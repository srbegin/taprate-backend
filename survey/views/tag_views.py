from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone

from ..models import NfcTag, Location


class NfcTagView(APIView):

    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request, tag_id):
        """Public — check if tag is claimed."""
        tag, _ = NfcTag.objects.get_or_create(id=tag_id)
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

        tag, _ = NfcTag.objects.get_or_create(id=tag_id)

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