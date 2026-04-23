import uuid
import random
import string
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.shortcuts import get_object_or_404

from ..models import Location, Question, SurveyResponse, Alert, IncentiveWin
from ..serializers import SurveyPublicSerializer, SurveyResponseSubmitSerializer


def _generate_win_code():
    """Generate a unique 8-character alphanumeric redemption code."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = ''.join(random.choices(chars, k=8))
        if not IncentiveWin.objects.filter(code=code).exists():
            return code
    raise RuntimeError('Could not generate a unique win code after 20 attempts.')


class PublicSurveyDetailView(APIView):
    """GET /api/survey/<location_uuid>/ — load survey for NFC tap."""
    permission_classes = [AllowAny]

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, id=location_uuid)

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
            'location_id': str(location.id),
            'location_name': location.name,
            'survey': serializer.data,
        })


class SurveyResponseView(APIView):
    """
    POST /api/survey/<location_uuid>/response/

    Accepts a full Survey submission:
    {
        "responses": [{"question_id": "<uuid>", "rating": 4}, ...],
        "comment": "optional",
        "email": "optional",
        "marketing_opt_in": false
    }

    Creates one SurveyResponse per question, all sharing a session_id.
    Fires alerts and incentive draws as needed.
    """
    permission_classes = [AllowAny]

    def post(self, request, location_uuid):
        location = get_object_or_404(Location, id=location_uuid)
        survey = location.survey

        if not survey:
            return Response(
                {'detail': 'No survey is configured for this location.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SurveyResponseSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated        = serializer.validated_data
        comment          = validated.get('comment', '')
        email            = validated.get('email', '')
        marketing_opt_in = validated.get('marketing_opt_in', False)

        # Build a lookup of valid question IDs in this survey
        valid_questions = {
            str(q.id): q
            for q in survey.questions.all()
        }

        # Validate all submitted question_ids belong to this survey
        for resp_data in validated['responses']:
            if str(resp_data['question_id']) not in valid_questions:
                return Response(
                    {'detail': f"Question {resp_data['question_id']} does not belong to this survey."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Incentive draw (once per session) ─────────────────────────────
        incentive_won     = False
        win_code          = None
        winning_prize     = None
        winning_incentive = None

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
                comment=comment if is_first else '',
                email=email if is_first else '',
                marketing_opt_in=marketing_opt_in if is_first else False,
                incentive_won=incentive_won and is_first,
            )
            created_responses.append(response_obj)

            if rating <= survey.alert_threshold:
                alert = Alert.objects.create(
                    survey_response=response_obj,
                    location=location,
                    rating=rating,
                )
                from ..tasks import send_alert
                send_alert.delay(str(alert.id))

        # ── Create IncentiveWin record ─────────────────────────────────────
        if incentive_won and winning_incentive and created_responses:
            first_response = created_responses[0]
            IncentiveWin.objects.create(
                incentive=winning_incentive,
                survey_response=first_response,
                code=win_code,
                email=email,
                marketing_opt_in=marketing_opt_in,
            )

        # ── Build response payload ─────────────────────────────────────────
        payload = {
            'success': True,
            'session_id': str(session_id),
            'incentive_won': incentive_won,
        }
        if incentive_won:
            payload['win_code']   = win_code
            payload['prize_text'] = winning_prize

        return Response(payload, status=status.HTTP_201_CREATED)