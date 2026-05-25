import uuid
import json
import random
import string
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.shortcuts import get_object_or_404
from django.core.cache import cache

from ..models import Location, Question, SurveyResponse, Alert, IncentiveWin
from ..serializers import SurveyPublicSerializer, SurveyResponseSubmitSerializer

# Session token TTL in seconds (30 minutes)
SESSION_TTL = 60 * 30  # 30 minutes


def _generate_win_code():
    """Generate a unique 8-character alphanumeric redemption code."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = ''.join(random.choices(chars, k=8))
        if not IncentiveWin.objects.filter(code=code).exists():
            return code
    raise RuntimeError('Could not generate a unique win code after 20 attempts.')


def _resolve_session(session_token):
    """
    Look up a session token in Redis.

    Returns (location, session_data) if valid, or (None, None) if
    expired/missing. session_data includes 'tag_id' which is None for
    dashboard previews and a UUID string for real NFC taps.

    Does NOT delete the token — that happens on submit only.
    """
    raw = cache.get(f'survey_session:{session_token}')
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
        location = (
            Location.objects
            .select_related('organization')
            .get(id=data['location_id'])
        )
        return location, data
    except (Location.DoesNotExist, KeyError, json.JSONDecodeError):
        return None, None


class PublicSurveyDetailView(APIView):
    """GET /api/survey/<session_token>/ — load survey for NFC tap."""
    permission_classes = [AllowAny]

    def get(self, request, session_token):
        location, _ = _resolve_session(session_token)
        if location is None:
            return Response(
                {'detail': 'Survey session has expired or is invalid. Please tap again.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not location.survey:
            return Response({
                'location_id': str(location.id),
                'location_name': location.name,
                'survey': None,
            })

        serializer = SurveyPublicSerializer(
            location.survey,
            context={'request': request, 'location': location},
        )
        return Response({
            'location_id':   str(location.id),
            'location_name': location.name,
            'survey':        serializer.data,
        })


class SurveyResponseView(APIView):
    """
    POST /api/survey/<session_token>/response/

    Accepts a full Survey submission:
    {
        "responses": [{"question_id": "<uuid>", "rating": 4}, ...],
        "comment": "optional",
        "email": "optional",
        "marketing_opt_in": false,
        "recovery_comment": "optional — only present when recovery step was shown",
        "recovery_email":   "optional — only present when recovery step was shown"
    }

    Validates and deletes the session token (single-use).
    Creates one SurveyResponse per question, all sharing a session_id.
    Recovery fields are stored only on the first response.

    Test detection (is_test=True):
      - Dashboard preview: session tag_id is None (minted by LocationPreviewView)
      - Physical tap while org test_mode is enabled
    Test responses skip alert creation and incentive draws entirely.
    """
    permission_classes = [AllowAny]

    def post(self, request, session_token):
        location, session_data = _resolve_session(session_token)
        if location is None:
            return Response(
                {'detail': 'Survey session has expired or is invalid. Please tap again.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        survey = location.survey
        if not survey:
            return Response(
                {'detail': 'No survey is configured for this location.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SurveyResponseSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # ── Consume the token before any writes ────────────────────────────
        cache.delete(f'survey_session:{session_token}')

        # ── Determine if this is a test submission ─────────────────────────
        # Preview sessions are minted with tag_id=None by LocationPreviewView.
        # Physical taps while org test_mode is on are also test responses.
        is_preview = session_data.get('source') == 'preview'
        # 'preview' is set by LocationPreviewView (dashboard).
        # QR scans set source='qr' — not a test response.
        # NFC taps have no source field — not a test response.
        is_test    = is_preview or location.organization.test_mode

        validated        = serializer.validated_data
        comment          = validated.get('comment', '')
        email            = validated.get('email', '')
        marketing_opt_in = validated.get('marketing_opt_in', False)
        recovery_comment = validated.get('recovery_comment', '')
        recovery_email   = validated.get('recovery_email', '')

        # ── Determine recovery_triggered server-side ───────────────────────
        recovery_triggered = (
            survey.recovery_enabled
            and any(
                r['rating'] <= survey.recovery_threshold
                for r in validated['responses']
            )
        )

        # Build a lookup of valid question IDs in this survey
        valid_questions = {
            str(q.id): q
            for q in survey.questions.all()
        }

        for resp_data in validated['responses']:
            if str(resp_data['question_id']) not in valid_questions:
                return Response(
                    {'detail': f"Question {resp_data['question_id']} does not belong to this survey."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Incentive draw — skipped for test responses ────────────────────
        incentive_won     = False
        win_code          = None
        winning_prize     = None
        winning_incentive = None

        if not is_test:
            active_incentive = survey.incentives.filter(active=True).first()
            if active_incentive:
                won = random.randint(1, 100) <= active_incentive.win_rate
                if won:
                    incentive_won     = True
                    winning_prize     = active_incentive.prize_text
                    winning_incentive = active_incentive
                    win_code          = _generate_win_code()

        # ── Create responses ───────────────────────────────────────────────
        session_id        = uuid.uuid4()
        created_responses = []

        for resp_data in validated['responses']:
            question = valid_questions[str(resp_data['question_id'])]
            rating   = resp_data['rating']
            is_first = not created_responses

            response_obj = SurveyResponse.objects.create(
                session_id=session_id,
                location=location,
                survey=survey,
                question=question,
                rating=rating,
                is_test=is_test,
                # Standard feedback fields — only on first response
                comment=comment if is_first else '',
                email=email if is_first else '',
                marketing_opt_in=marketing_opt_in if is_first else False,
                incentive_won=incentive_won and is_first,
                # Recovery fields — only on first response, only when triggered
                recovery_triggered=recovery_triggered and is_first,
                recovery_comment=recovery_comment if (is_first and recovery_triggered) else '',
                recovery_email=recovery_email if (is_first and recovery_triggered) else '',
            )
            created_responses.append(response_obj)

            # ── Alerts — skipped for test responses ───────────────────────
            if not is_test and rating <= survey.alert_threshold:
                alert = Alert.objects.create(
                    survey_response=response_obj,
                    location=location,
                    rating=rating,
                )
                from ..tasks import send_alert
                send_alert.delay(str(alert.id))

        # ── IncentiveWin record — skipped for test responses ──────────────
        if not is_test and incentive_won and winning_incentive and created_responses:
            first_response = created_responses[0]
            IncentiveWin.objects.create(
                incentive=winning_incentive,
                survey_response=first_response,
                code=win_code,
                email=email,
                marketing_opt_in=marketing_opt_in,
            )

        payload = {
            'success':       True,
            'session_id':    str(session_id),
            'incentive_won': incentive_won,
            'is_test':       is_test,
        }
        if incentive_won:
            payload['win_code']   = win_code
            payload['prize_text'] = winning_prize

        return Response(payload, status=status.HTTP_201_CREATED)

class QrSessionView(APIView):
    """
    POST /api/survey/location/<location_id>/session/
 
    Public endpoint called when a customer scans a location's QR code.
    Mints a short-lived session token the same way TagSessionView does for
    NFC taps. No rate limiting — QR scans are manual and can't be spammed
    the way a tap-loop could be.
 
    Session source is 'qr' so SurveyResponseView does not mark the
    response as is_test (unlike 'preview' sessions from the dashboard).
    """
    permission_classes = [AllowAny]
 
    def post(self, request, location_id):
        location = get_object_or_404(Location, id=location_id, active=True)
 
        if not location.survey:
            return Response(
                {'detail': 'No survey is configured for this location.'},
                status=status.HTTP_404_NOT_FOUND,
            )
 
        token = str(uuid.uuid4())
        session_data = json.dumps({
            'location_id': str(location_id),
            'tag_id':      None,
            'source':      'qr',
        })
        cache.set(f'survey_session:{token}', session_data, SESSION_TTL)
        return Response({'token': token}, status=status.HTTP_201_CREATED)
 