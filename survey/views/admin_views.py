from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Exists, OuterRef
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Location, NfcTag, Organization, SurveyResponse
from ..products import TAPRATE
from ..utils.responses import list_response

User = get_user_model()



def _subscriptions(org):
    """Per-product subscriptions for admin display."""
    return [
        {
            'product':                sub.product,
            'plan':                   sub.plan,
            'status':                 sub.status,
            'trial_ends_at':          sub.trial_ends_at,
            'stripe_subscription_id': sub.stripe_subscription_id,
        }
        for sub in org.subscriptions.all()
    ]


def _legacy_sub(org):
    """TapRate subscription, backing the pre-split plan/status keys the admin UI reads."""
    return next((sub for sub in org.subscriptions.all() if sub.product == TAPRATE), None)


def _legacy_plan(org):
    sub = _legacy_sub(org)
    return (sub.plan if sub else '') or 'free'

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
            .prefetch_related('subscriptions')
            .order_by('-created_at')
        )
        data = [
            {
                'id':             str(org.id),
                'name':           org.name,
                'slug':           org.slug,
                'plan':           _legacy_plan(org),
                'subscriptions':  _subscriptions(org),
                'location_count': org.location_count,
                'response_count': org.response_count,
                'user_count':     org.user_count,
                'created_at':     org.created_at,
            }
            for org in orgs
        ]
        return Response(list_response(data))


class AdminTagListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        tags = (
            NfcTag.objects
            .select_related('organization', 'location')
            .order_by('-created_at')
        )

        # Optional filter: ?status=claimed | unclaimed
        status_filter = request.query_params.get('status')
        if status_filter == 'unclaimed':
            tags = tags.filter(organization__isnull=True)
        elif status_filter == 'claimed':
            tags = tags.filter(organization__isnull=False)

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
        return Response(list_response(data))


class AdminRecentSignupsView(APIView):
    permission_classes = [IsAdminUser]

    # Not converted — compound object with two named lists, not a single list endpoint.
    def get(self, request):
        recent_orgs = Organization.objects.prefetch_related('subscriptions').order_by('-created_at')[:15]
        recent_users = (
            User.objects
            .select_related('organization')
            .order_by('-date_joined')[:15]
        )
        return Response({
            'recent_orgs': [
                {
                    'id':         str(org.id),
                    'name':          org.name,
                    'plan':          _legacy_plan(org),
                    'subscriptions': _subscriptions(org),
                    'created_at':    org.created_at,
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


class AdminTagDetailView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, tag_id):
        """Release a claimed tag back to fully unclaimed state (no org, no location)."""
        tag = get_object_or_404(NfcTag, id=tag_id)
        tag.organization = None
        tag.location = None
        tag.claimed_at = None
        tag.save()
        return Response({'id': str(tag.id), 'claimed': False})


# ── Org detail ────────────────────────────────────────────────────────────────

class AdminOrgDetailView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, org_id):
        """Full org record for the admin detail page."""
        org = get_object_or_404(Organization, id=org_id)
        legacy = _legacy_sub(org)
        return Response({
            'id':                     str(org.id),
            'name':                   org.name,
            'slug':                   org.slug,
            'plan':                   _legacy_plan(org),
            'subscription_status':    legacy.status if legacy else '',
            'trial_ends_at':          legacy.trial_ends_at if legacy else None,
            'stripe_customer_id':     org.stripe_customer_id,
            'stripe_subscription_id': legacy.stripe_subscription_id if legacy else '',
            'subscriptions':          _subscriptions(org),
            'alert_email':            org.alert_email,
            'alerts_enabled':         org.alerts_enabled,
            'timezone':               org.timezone,
            'created_at':             org.created_at,
            'stats': {
                'locations': org.locations.count(),
                'users':     org.members.count(),
                'tags':      org.nfc_tags.count(),
                'responses': SurveyResponse.objects.filter(
                                 location__organization=org
                             ).count(),
            },
        })


class AdminOrgLocationsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, org_id):
        org = get_object_or_404(Organization, id=org_id)
        locations = (
            Location.objects
            .filter(organization=org)
            .annotate(has_tag=Exists(NfcTag.objects.filter(location_id=OuterRef('pk'))))
            .order_by('name')
        )
        data = [
            {
                'id':      str(loc.id),
                'name':    loc.name,
                'has_tag': loc.has_tag,
            }
            for loc in locations
        ]
        return Response(list_response(data))


class AdminOrgTagsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, org_id):
        """List tags currently owned by this org."""
        org = get_object_or_404(Organization, id=org_id)
        tags = (
            NfcTag.objects
            .filter(organization=org)
            .select_related('location')
            .order_by('-claimed_at')
        )
        data = [
            {
                'id':            str(tag.id),
                'location_id':   str(tag.location.id) if tag.location else None,
                'location_name': tag.location.name if tag.location else None,
                'claimed_at':    tag.claimed_at,
                'created_at':    tag.created_at,
            }
            for tag in tags
        ]
        return Response(list_response(data))

    def post(self, request, org_id):
        """
        Assign an unowned tag directly to one of this org's locations in one step.

        Body: { "tag_id": "<uuid>", "location_id": "<uuid>" }

        Returns 409 if the tag already belongs to any org, or if the location
        already has a tag assigned.
        """
        org = get_object_or_404(Organization, id=org_id)

        tag_id      = request.data.get('tag_id')
        location_id = request.data.get('location_id')

        if not tag_id:
            return Response(
                {'detail': 'tag_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not location_id:
            return Response(
                {'detail': 'location_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tag      = get_object_or_404(NfcTag, id=tag_id)
        location = get_object_or_404(Location, id=location_id, organization=org)

        if tag.organization is not None:
            return Response(
                {'detail': 'Tag is already assigned to an organization.'},
                status=status.HTTP_409_CONFLICT,
            )

        if NfcTag.objects.filter(location=location).exists():
            return Response(
                {'detail': 'This location already has a tag assigned.'},
                status=status.HTTP_409_CONFLICT,
            )

        tag.organization = org
        tag.location     = location
        tag.claimed_at   = timezone.now()
        tag.save()

        return Response(
            {
                'id':            str(tag.id),
                'location_id':   str(location.id),
                'location_name': location.name,
                'claimed_at':    tag.claimed_at,
                'created_at':    tag.created_at,
            },
            status=status.HTTP_201_CREATED,
        )