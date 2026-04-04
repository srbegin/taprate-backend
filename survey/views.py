import random
import hashlib
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication
from .models import Location, SurveyResponse, Alert
from .serializers import (
    LocationSerializer,
    SurveyPublicSerializer,
    SurveyResponseSerializer,
)

ALERT_THRESHOLD = 2
RATE_LIMIT_SECONDS = 10  # set back to 300 for production


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return  # skip CSRF check for public endpoints


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:32]


def get_client_ip(request) -> str:
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


# ── GET /api/survey/<location_uuid>/
@api_view(['GET'])
@permission_classes([AllowAny])
def public_survey_detail(request, location_uuid):
    """Returns survey config + branding for the public PWA."""
    try:
        location = Location.objects.select_related(
            'survey__organization',
            'survey__incentive',
        ).get(id=location_uuid, active=True)
    except Location.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    if not location.survey:
        return Response({'error': 'No survey assigned to this location'}, status=status.HTTP_404_NOT_FOUND)

    serializer = SurveyPublicSerializer(
        location.survey,
        context={'request': request, 'location': location}
    )
    return Response(serializer.data)


# ── POST /api/survey/<location_uuid>/response/
@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([AllowAny])
def submit_survey_response(request, location_uuid):
    """Rate-limited survey submission with optional incentive handling."""
    try:
        location = Location.objects.select_related(
            'survey__incentive'
        ).get(id=location_uuid, active=True)
    except Location.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    serializer = SurveyResponseSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    ip = get_client_ip(request)
    ip_hash = hash_ip(ip)
    device_hash = request.data.get('device_hash', '')

    # ── Rate limit per IP
    rate_key = f"survey:ratelimit:{ip_hash}:{location.id}"
    if cache.get(rate_key):
        return Response(
            {'error': 'Too many submissions. Please wait before rating again.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS
        )
    cache.set(rate_key, 1, timeout=RATE_LIMIT_SECONDS)

    # ── Rate limit per device fingerprint
    if device_hash:
        device_key = f"survey:device:{device_hash}:{location.id}"
        if cache.get(device_key):
            return Response(
                {'error': 'Already submitted recently.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        cache.set(device_key, 1, timeout=RATE_LIMIT_SECONDS)

    # ── Determine incentive win
    incentive_won = False
    incentive = getattr(location.survey, 'incentive', None)
    if incentive and incentive.active:
        incentive_won = (random.randint(1, incentive.win_rate) == 1)

    # ── Save response
    survey_response = SurveyResponse.objects.create(
        location=location,
        survey=location.survey,
        rating=serializer.validated_data['rating'],
        comment=serializer.validated_data.get('comment', ''),
        email=serializer.validated_data.get('email', ''),
        incentive_won=incentive_won,
        ip_hash=ip_hash,
        device_hash=device_hash,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
    )

    # ── Send incentive email if won and email provided
    if incentive_won and survey_response.email:
        from .tasks import send_incentive_email
        send_incentive_email.delay(survey_response.id)

    # ── Trigger low-rating alert
    if survey_response.rating <= ALERT_THRESHOLD:
        alert = Alert.objects.create(
            survey_response=survey_response,
            location=location,
            rating=survey_response.rating,
        )
        from .tasks import send_alert
        send_alert.delay(alert.id)

    return Response({
        'status': 'ok',
        'id': str(survey_response.id),
        'incentive_won': incentive_won,
    }, status=status.HTTP_201_CREATED)