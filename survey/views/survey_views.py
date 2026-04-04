import random
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.shortcuts import get_object_or_404

from ..models import Location, SurveyResponse, Alert
from ..serializers import SurveyPublicSerializer, SurveyResponseSerializer


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
    """POST /api/survey/<location_uuid>/response/ — submit a rating."""
    permission_classes = [AllowAny]

    def post(self, request, location_uuid):
        location = get_object_or_404(Location, id=location_uuid)
        survey = location.survey

        if not survey:
            return Response(
                {'detail': 'No survey is configured for this location.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SurveyResponseSerializer(
            data=request.data,
            context={'survey': survey},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        incentive_won = self._run_incentive_draw(survey, serializer.validated_data)

        response_obj = SurveyResponse.objects.create(
            survey=survey,
            location=location,
            rating=serializer.validated_data['rating'],
            comment=serializer.validated_data.get('comment', ''),
            email=serializer.validated_data.get('email', ''),
            incentive_won=incentive_won,
        )

        self._maybe_create_alert(survey, response_obj)

        if incentive_won and response_obj.email:
            from ..tasks import send_incentive_email
            send_incentive_email.delay(str(response_obj.id))

        payload = {'success': True, 'incentive_won': incentive_won}
        if incentive_won:
            payload['prize_text'] = survey.incentive.prize_text

        return Response(payload, status=status.HTTP_201_CREATED)

    def _run_incentive_draw(self, survey, data):
        if not hasattr(survey, 'incentive'):
            return False
        if not data.get('email'):
            return False
        return random.randint(1, survey.incentive.win_rate) == 1

    def _maybe_create_alert(self, survey, response_obj):
        """Create an Alert and dispatch the notification task if rating is low."""
        if response_obj.rating <= survey.alert_threshold:
            alert = Alert.objects.create(
                survey_response=response_obj,
                location=response_obj.location,
                rating=response_obj.rating,
            )
            from ..tasks import send_alert
            send_alert.delay(str(alert.id))