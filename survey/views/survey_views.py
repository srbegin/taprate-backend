import uuid
import random
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.shortcuts import get_object_or_404

from ..models import Location, Survey, SurveyResponse, Alert
from ..serializers import SurveySetPublicSerializer, SurveyResponseSubmitSerializer


class PublicSurveyDetailView(APIView):
    """GET /api/survey/<location_uuid>/ — load survey set for NFC tap."""
    permission_classes = [AllowAny]

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, id=location_uuid)

        if not location.survey_set:
            return Response({
                'location_id': str(location.id),
                'location_name': location.name,
                'survey_set': None,
            })

        serializer = SurveySetPublicSerializer(
            location.survey_set,
            context={'request': request, 'location': location},
        )
        return Response({
            'location_id': str(location.id),
            'location_name': location.name,
            'survey_set': serializer.data,
        })


class SurveyResponseView(APIView):
    """
    POST /api/survey/<location_uuid>/response/

    Accepts a full SurveySet submission:
    {
        "responses": [{"survey_id": "<uuid>", "rating": 4}, ...],
        "comment": "optional",
        "email": "optional"
    }

    Creates one SurveyResponse per question, all sharing a session_id.
    Fires alerts and incentive draws as needed.
    """
    permission_classes = [AllowAny]

    def post(self, request, location_uuid):
        location = get_object_or_404(Location, id=location_uuid)
        survey_set = location.survey_set

        if not survey_set:
            return Response(
                {'detail': 'No survey is configured for this location.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SurveyResponseSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated = serializer.validated_data
        comment = validated.get('comment', '')
        email = validated.get('email', '')

        # Build a lookup of the valid survey IDs in this set
        valid_surveys = {
            str(s.id): s
            for s in survey_set.surveys.select_related('incentive').all()
        }

        # Validate all submitted survey_ids belong to this set
        for resp_data in validated['responses']:
            if str(resp_data['survey_id']) not in valid_surveys:
                return Response(
                    {'detail': f"Survey {resp_data['survey_id']} does not belong to this location."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # One UUID groups all responses from this tap
        session_id = uuid.uuid4()

        created_responses = []
        incentive_won = False
        winning_prize = None

        for resp_data in validated['responses']:
            survey = valid_surveys[str(resp_data['survey_id'])]
            rating = resp_data['rating']

            # Incentive draw — only once per session, first win takes it
            if not incentive_won and email:
                won, prize = self._run_incentive_draw(survey, email)
                if won:
                    incentive_won = True
                    winning_prize = prize

            response_obj = SurveyResponse.objects.create(
                session_id=session_id,
                location=location,
                survey_set=survey_set,
                survey=survey,
                rating=rating,
                # Only attach comment + email to the first response to avoid duplication
                comment=comment if not created_responses else '',
                email=email if not created_responses else '',
                incentive_won=(incentive_won and not created_responses),
            )
            created_responses.append(response_obj)

            # Alert if this individual rating is below the set's threshold
            if rating <= survey_set.alert_threshold:
                alert = Alert.objects.create(
                    survey_response=response_obj,
                    location=location,
                    rating=rating,
                )
                # Uncomment once SendGrid is configured:
                print("Trying to send alert")
                from ..tasks import send_alert
                send_alert.delay(str(alert.id))

        # Send incentive email for the winning response
        if incentive_won and email and created_responses:
            winning_response = next(r for r in created_responses if r.incentive_won)
            # Uncomment once SendGrid is configured:
            # from ..tasks import send_incentive_email
            # send_incentive_email.delay(str(winning_response.id))

        payload = {
            'success': True,
            'session_id': str(session_id),
            'incentive_won': incentive_won,
        }
        if incentive_won and winning_prize:
            payload['prize_text'] = winning_prize

        return Response(payload, status=status.HTTP_201_CREATED)

    @staticmethod
    def _run_incentive_draw(survey, email):
        """Returns (won: bool, prize_text: str|None)."""
        if not hasattr(survey, 'incentive') or not survey.incentive.active:
            return False, None
        won = random.randint(1, survey.incentive.win_rate) == 1
        return won, survey.incentive.prize_text if won else None