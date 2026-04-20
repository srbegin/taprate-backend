from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Location, NfcTag, Organization, SurveyResponse

User = get_user_model()


class AdminOverviewView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return Response({
            'orgs':              Organization.objects.count(),
            'locations':         Location.objects.count(),
            'responses_total':   SurveyResponse.objects.count(),
            'responses_30d':     SurveyResponse.objects.filter(
                                     created_at__gte=thirty_days_ago
                                 ).count(),
            'tags_total':        NfcTag.objects.count(),
            'tags_claimed':      NfcTag.objects.filter(location__isnull=False).count(),
            'tags_unclaimed':    NfcTag.objects.filter(location__isnull=True).count(),
        })


class AdminOrganizationListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        orgs = (
            Organization.objects
            .annotate(
                location_count=Count('locations', distinct=True),
                response_count=Count('locations__responses', distinct=True),
                user_count=Count('members', distinct=True),
            )
            .order_by('-created_at')
        )
        data = [
            {
                'id':             str(org.id),
                'name':           org.name,
                'slug':           org.slug,
                'plan':           org.plan,
                'location_count': org.location_count,
                'response_count': org.response_count,
                'user_count':     org.user_count,
                'created_at':     org.created_at,
            }
            for org in orgs
        ]
        return Response(data)


class AdminTagListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        tags = (
            NfcTag.objects
            .select_related('organization', 'location')
            .order_by('-created_at')
        )
        data = [
            {
                'id':            str(tag.id),
                'claimed':       tag.location is not None,
                'org_name':      tag.organization.name if tag.organization else None,
                'location_name': tag.location.name if tag.location else None,
                'claimed_at':    tag.claimed_at,
                'created_at':    tag.created_at,
            }
            for tag in tags
        ]
        return Response(data)


class AdminRecentSignupsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        recent_orgs = Organization.objects.order_by('-created_at')[:15]
        recent_users = (
            User.objects
            .select_related('organization')
            .order_by('-date_joined')[:15]
        )
        return Response({
            'recent_orgs': [
                {
                    'id':         str(org.id),
                    'name':       org.name,
                    'plan':       org.plan,
                    'created_at': org.created_at,
                }
                for org in recent_orgs
            ],
            'recent_users': [
                {
                    'id':       str(u.id),
                    'email':    u.email,
                    'name':     f"{u.first_name} {u.last_name}".strip(),
                    'org_name': u.organization.name if u.organization else '—',
                    'role':     u.role,
                    'joined':   u.date_joined,
                }
                for u in recent_users
            ],
        })