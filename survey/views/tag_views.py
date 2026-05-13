import os
import uuid
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.core.cache import cache

from ..models import NfcTag, Location

# Session token TTL in seconds (30 minutes)
SESSION_TTL = 60 * 30

# Rate limit: one session mint per tag per IP per window.
# TTL-based — presence of key means window is active.
# 5 minutes matches the frontend localStorage cooldown.
RATE_LIMIT_WINDOW = 60 * 5  # 5 minutes


def _get_client_ip(request):
    """Extract client IP, respecting X-Forwarded-For from Fly.io proxy."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


class NfcTagView(APIView):

    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request, tag_id):
        """Public — check if tag is claimed."""
        tag = get_object_or_404(NfcTag, id=tag_id)
        if tag.location:
            return Response({
                'claimed': True,
                'location_id': str(tag.location.id),
            })
        return Response({'claimed': False})

    def post(self, request, tag_id):
        """Authenticated — claim or reassign tag to a location."""
        location_id = request.data.get('location_id')
        if not location_id:
            return Response(
                {'detail': 'location_id is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        location = get_object_or_404(
            Location, id=location_id, organization=request.user.organization
        )

        tag = get_object_or_404(NfcTag, id=tag_id)

        # Block if already owned by a different org
        if tag.organization and tag.organization != request.user.organization:
            return Response(
                {'detail': 'This tag is registered to another organization.'},
                status=status.HTTP_403_FORBIDDEN
            )

        tag.organization = request.user.organization
        tag.location = location
        tag.claimed_at = timezone.now()
        tag.save()

        return Response({
            'claimed': True,
            'location_id': str(location.id),
            'location_name': location.name,
        })


class TagSessionView(APIView):
    """
    POST /api/tags/<tag_id>/session/

    Public endpoint called when a customer taps an NFC tag.
    - Validates the tag is claimed and has a location with an active survey.
    - Enforces IP-based rate limit (1 mint / tag / IP / 5 min window).
    - Mints a short-lived single-use session token in Redis (30 min TTL).
    - Returns { token } for the frontend to redirect to /s/{token}.
    """
    permission_classes = [AllowAny]

    def post(self, request, tag_id):
        tag = get_object_or_404(NfcTag, id=tag_id)

        if not tag.location:
            return Response(
                {'detail': 'This tag has not been claimed yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        location = tag.location

        if not location.survey:
            return Response(
                {'detail': 'No survey is configured for this location.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── IP rate limit ──────────────────────────────────────────────────
        # TTL-based: one mint allowed per tag per IP per RATE_LIMIT_WINDOW.
        # Key presence means the window is active; expiry clears it automatically.
        ip = _get_client_ip(request)

        if not os.environ.get('DISABLE_RATE_LIMIT') == 'true':
            rate_key = f'tag_session_rate:{tag_id}:{ip}'

            if cache.get(rate_key):
                return Response(
                    {'detail': 'Too many survey sessions. Please try again later.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            cache.set(rate_key, 1, RATE_LIMIT_WINDOW)

        # ── Mint session token ─────────────────────────────────────────────
        token = str(uuid.uuid4())
        session_data = json.dumps({
            'location_id': str(location.id),
            'tag_id':      str(tag_id),
        })
        cache.set(f'survey_session:{token}', session_data, SESSION_TTL)

        return Response({'token': token}, status=status.HTTP_201_CREATED)