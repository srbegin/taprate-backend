import io
import qrcode
from django.http import HttpResponse
from django.core.cache import cache
import uuid as uuid_lib
import json

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Avg, Count, FloatField
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..permissions import HasActiveAccess
from ..models import Location, Survey, Question, SurveyResponse, Alert, ResponseIssue
from ..serializers import (
    LocationSerializer, OrganizationSerializer,
    QuestionSerializer, QuestionWriteSerializer,
    SurveySerializer, SurveyWriteSerializer,
)
from ..utils.responses import list_response
from ..products import get_brand, get_product
from .billing_views import OVERAGE_PRICE_PER_LOCATION, _sync_overage_quantity, location_usage



# ── Locations ─────────────────────────────────────────────────────────────────

class LocationListView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request):
        org     = request.user.organization
        product = get_product(request)
        locations = Location.objects.filter(
            organization=org, product=product,
        ).select_related('survey').order_by('-created_at')
        serializer = LocationSerializer(locations, many=True, context={'request': request})

        _, base, overage_count = location_usage(org, product)  # base None for trial — no cap
        overage_cost = overage_count * OVERAGE_PRICE_PER_LOCATION.get(product, 0)

        return Response(list_response(
            serializer.data,
            base_locations=base,
            overage_count=overage_count,
            overage_monthly_cost=overage_cost,
            # at_limit kept for ClaimTagClient — only true on paid plans at their base
            at_limit=False,
        ))

    def post(self, request):
        org     = request.user.organization
        product = get_product(request)

        serializer = LocationSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save(organization=org, product=product)

        # Sync overage quantity to Stripe for active paid subscribers.
        # No-op for trial orgs — they have no Stripe subscription yet.
        _sync_overage_quantity(org, product)

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LocationDetailView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def _get_location(self, request, pk):
        return get_object_or_404(
            Location, id=pk, organization=request.user.organization, product=get_product(request),
        )

    def get(self, request, pk):
        location = self._get_location(request, pk)
        return Response(LocationSerializer(location, context={'request': request}).data)

    def patch(self, request, pk):
        location = self._get_location(request, pk)
        serializer = LocationSerializer(
            location, data=request.data, partial=True, context={'request': request}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        location = self._get_location(request, pk)
        location.delete()
        _sync_overage_quantity(request.user.organization, location.product)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LocationPreviewView(APIView):
    """POST /api/dashboard/locations/<pk>/preview/ — mint a session for dashboard preview."""
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def post(self, request, pk):
        location = get_object_or_404(
            Location, id=pk, organization=request.user.organization, product=get_product(request),
        )
        if not location.survey:
            return Response(
                {'detail': 'No survey assigned to this location.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        token = str(uuid_lib.uuid4())
        # tag_id=None is the signal that marks this as a preview session.
        # survey_views._resolve_session reads this and sets is_test=True on the response.
        cache.set(
            f'survey_session:{token}',
            json.dumps({'location_id': str(location.id), 'tag_id': None, 'source': 'preview'}),
            60 * 30,
        )
        return Response({'token': token})


# ── Surveys ───────────────────────────────────────────────────────────────────

class SurveyListView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request):
        surveys = Survey.objects.filter(
            organization=request.user.organization, product=get_product(request),
        ).prefetch_related('questions__options', 'incentives', 'locations').order_by('-created_at')
        data = SurveySerializer(surveys, many=True).data
        return Response(list_response(data))

    def post(self, request):
        questions_data = request.data.pop('questions', [])

        serializer = SurveyWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        survey = serializer.save(
            organization=request.user.organization, product=get_product(request),
        )

        for i, q_data in enumerate(questions_data):
            q_data.setdefault('position', i)
            q_ser = QuestionWriteSerializer(data=q_data)
            if q_ser.is_valid():
                q_ser.save(
                    survey=survey,
                    organization=request.user.organization,
                )

        survey.refresh_from_db()
        return Response(SurveySerializer(survey).data, status=status.HTTP_201_CREATED)


class SurveyDetailView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def _get_survey(self, request, pk):
        return get_object_or_404(
            Survey, id=pk, organization=request.user.organization, product=get_product(request),
        )

    def get(self, request, pk):
        return Response(SurveySerializer(self._get_survey(request, pk)).data)

    def patch(self, request, pk):
        survey = self._get_survey(request, pk)
        serializer = SurveyWriteSerializer(survey, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        survey = serializer.save()
        return Response(SurveySerializer(survey).data)

    def delete(self, request, pk):
        self._get_survey(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Questions (nested under Survey) ──────────────────────────────────────────

class QuestionListView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def _get_survey(self, request, survey_pk):
        return get_object_or_404(
            Survey, id=survey_pk, organization=request.user.organization, product=get_product(request),
        )

    def get(self, request, survey_pk):
        survey = self._get_survey(request, survey_pk)
        questions = survey.questions.order_by('position')
        return Response(list_response(QuestionSerializer(questions, many=True).data))

    def post(self, request, survey_pk):
        survey = self._get_survey(request, survey_pk)
        serializer = QuestionWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        question = serializer.save(
            survey=survey,
            organization=request.user.organization,
        )
        return Response(QuestionSerializer(question).data, status=status.HTTP_201_CREATED)


class QuestionDetailView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def _get_question(self, request, survey_pk, pk):
        return get_object_or_404(
            Question,
            id=pk,
            survey_id=survey_pk,
            survey__product=get_product(request),
            organization=request.user.organization,
        )

    def get(self, request, survey_pk, pk):
        question = self._get_question(request, survey_pk, pk)
        return Response(QuestionSerializer(question).data)

    def patch(self, request, survey_pk, pk):
        question = self._get_question(request, survey_pk, pk)
        serializer = QuestionWriteSerializer(question, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(QuestionSerializer(serializer.save()).data)

    def delete(self, request, survey_pk, pk):
        self._get_question(request, survey_pk, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Alerts ────────────────────────────────────────────────────────────────────

class AlertListView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request):
        status_filter = request.query_params.get('status', 'pending')
        qs = Alert.objects.filter(
            location__organization=request.user.organization,
            location__product=get_product(request),
            survey_response__is_test=False,
        ).select_related('location', 'survey_response').order_by('-created_at')
        if status_filter != 'all':
            qs = qs.filter(status=status_filter)
        data = [self._serialize(a) for a in qs[:50]]
        return Response(list_response(data))

    @staticmethod
    def _serialize(alert):
        return {
            'id':               str(alert.id),
            'kind':             alert.kind,             # 'low_rating' | 'issue'
            'location':         alert.location.name,
            'location_id':      str(alert.location.id),
            'rating':           alert.rating,           # low_rating only
            'issue_label':      alert.issue_label,      # issue only
            'report_count':     alert.report_count,     # issue only — repeat reports while open
            'last_reported_at': alert.last_reported_at.isoformat() if alert.last_reported_at else None,
            'comment':          alert.survey_response.comment,
            'status':           alert.status,
            'created_at':       alert.created_at.isoformat(),
            'resolved_at':      alert.resolved_at.isoformat() if alert.resolved_at else None,
        }


class AlertDetailView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def patch(self, request, pk):
        alert = get_object_or_404(
            Alert, id=pk,
            location__organization=request.user.organization,
            location__product=get_product(request),
        )
        new_status = request.data.get('status')
        if new_status not in ('pending', 'owner_notified', 'resolved'):
            return Response(
                {'detail': 'status must be "pending", "owner_notified", or "resolved".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        alert.status = new_status
        alert.resolved_at = timezone.now() if new_status == 'resolved' else None
        alert.save(update_fields=['status', 'resolved_at'])
        return Response(AlertListView._serialize(alert))


# ── Insights ──────────────────────────────────────────────────────────────────

def _get_org_tz(org):
    """Return a ZoneInfo object for the org's configured timezone, falling back to UTC."""
    tz_string = (org.timezone or 'UTC') if org else 'UTC'
    try:
        return ZoneInfo(tz_string)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo('UTC')


class InsightsView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request):
        org     = request.user.organization
        product = get_product(request)
        try:
            days = min(int(request.query_params.get('days', 30)), 90)
        except (ValueError, TypeError):
            days = 30

        local_tz = _get_org_tz(org)

        location_id  = request.query_params.get('location')
        now          = timezone.now()
        period_start = now - timedelta(days=days)
        prev_start   = period_start - timedelta(days=days)

        # All real answers (rating + issues questions)
        answers_qs = SurveyResponse.objects.filter(
            location__organization=org,
            location__product=product,
            is_test=False,
        )
        if location_id:
            answers_qs = answers_qs.filter(location_id=location_id)

        # Rating metrics use rated answers only — issue answers have no rating.
        base_qs = answers_qs.filter(rating__isnull=False)

        current_qs  = base_qs.filter(created_at__gte=period_start)
        previous_qs = base_qs.filter(created_at__gte=prev_start, created_at__lt=period_start)

        current_agg  = current_qs.aggregate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))
        previous_agg = previous_qs.aggregate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))

        current_avg  = round(current_agg['avg'] or 0, 2)
        previous_avg = round(previous_agg['avg'] or 0, 2)

        # Count test responses in the same period so the frontend can surface a notice
        test_qs = SurveyResponse.objects.filter(
            location__organization=org,
            location__product=product,
            is_test=True,
            created_at__gte=period_start,
        )
        if location_id:
            test_qs = test_qs.filter(location_id=location_id)
        test_response_count = test_qs.count()

        daily = (
            current_qs
            .annotate(date=TruncDate('created_at', tzinfo=local_tz))
            .values('date')
            .annotate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))
            .order_by('date')
        )
        daily_map = {
            row['date'].isoformat(): {'avg': round(row['avg'], 2), 'count': row['count']}
            for row in daily
        }

        local_period_start = now.astimezone(local_tz) - timedelta(days=days)
        daily_series = []
        for i in range(days):
            d   = (local_period_start + timedelta(days=i)).date()
            key = d.isoformat()
            daily_series.append({
                'date':  key,
                'avg':   daily_map[key]['avg']   if key in daily_map else None,
                'count': daily_map[key]['count'] if key in daily_map else 0,
            })

        location_breakdown = (
            current_qs
            .values('location__id', 'location__name')
            .annotate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))
            .order_by('-count')
        )

        distribution_qs = current_qs.values('rating').annotate(count=Count('id'))
        distribution = {str(i): 0 for i in range(1, 6)}
        for row in distribution_qs:
            distribution[str(row['rating'])] = row['count']

        # Issues: answers to issues questions (rating is null), how many
        # reported something, and the most-reported issues.
        issue_answers = answers_qs.filter(rating__isnull=True, created_at__gte=period_start)
        top_issues = (
            ResponseIssue.objects
            .filter(survey_response__in=issue_answers)
            .values('label')
            .annotate(count=Count('id'))
            .order_by('-count', 'label')[:10]
        )

        alert_qs = Alert.objects.filter(
            location__organization=org,
            location__product=product,
            status__in=Alert.OPEN_STATUSES,
            survey_response__is_test=False,
        ).select_related('location', 'survey_response').order_by('-created_at')
        if location_id:
            alert_qs = alert_qs.filter(location_id=location_id)

        return Response({
            'days':     days,
            'timezone': str(local_tz),
            'summary': {
                'avg_rating':          current_avg,
                'avg_delta':           round(current_avg - previous_avg, 2) if previous_avg else None,
                'total_responses':     current_agg['count'],
                'count_delta':         current_agg['count'] - previous_agg['count'],
                'test_response_count': test_response_count,
            },
            'daily_series': daily_series,
            'by_location': [
                {
                    'id':    str(r['location__id']),
                    'name':  r['location__name'],
                    'avg':   round(r['avg'], 2),
                    'count': r['count'],
                }
                for r in location_breakdown
            ],
            'distribution':   distribution,
            'issues': {
                'answered':    issue_answers.count(),
                'with_issues': issue_answers.filter(issues__isnull=False).distinct().count(),
                'top':         [{'label': r['label'], 'count': r['count']} for r in top_issues],
            },
            'pending_alerts': [AlertListView._serialize(a) for a in alert_qs[:10]],
        })


# ── Comment Feed ──────────────────────────────────────────────────────────────

class CommentFeedView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request):
        org = request.user.organization

        show_test = request.query_params.get('is_test', 'false').lower() == 'true'

        qs = (
            SurveyResponse.objects
            .filter(
                location__organization=org,
                location__product=get_product(request),
                is_test=show_test,
            )
            .exclude(comment='')
            .select_related('location', 'survey')
            .order_by('-created_at')
        )

        location_id = request.query_params.get('location')
        if location_id:
            qs = qs.filter(location_id=location_id)

        date_from = request.query_params.get('date_from')
        date_to   = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        try:
            page      = max(int(request.query_params.get('page', 1)), 1)
            page_size = min(int(request.query_params.get('page_size', 20)), 100)
        except (ValueError, TypeError):
            page, page_size = 1, 20

        total  = qs.count()
        offset = (page - 1) * page_size
        items  = qs[offset:offset + page_size]

        return Response(list_response(
            [
                {
                    'id':            str(r.id),
                    'comment':       r.comment,
                    'rating':        r.rating,
                    'is_test':       r.is_test,
                    'created_at':    r.created_at.isoformat(),
                    'location_id':   str(r.location.id) if r.location else None,
                    'location_name': r.location.name if r.location else None,
                    'survey_name':   r.survey.name if r.survey else None,
                }
                for r in items
            ],
            # count here is the total across all pages, not just this page
            count=total,
            page=page,
            page_size=page_size,
            is_test=show_test,
        ))


# ── Organization ──────────────────────────────────────────────────────────────

_ORG_WRITABLE_FIELDS = {
    'name', 'brand_color', 'logo_url',
    'alert_email', 'alerts_enabled',
    'default_alert_threshold', 'default_review_url',
    'default_comments_enabled', 'default_comments_prompt',
    'timezone',
    'test_mode',
}


class OrganizationView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request):
        return Response(
            OrganizationSerializer(request.user.organization, context={'request': request}).data
        )

    def patch(self, request):
        org = request.user.organization

        if not org:
            return Response({'detail': 'No organization found.'}, status=status.HTTP_404_NOT_FOUND)

        # if 'test_mode' in request.data and not request.user.is_staff:
        #     return Response({'detail': 'Not permitted.'}, status=status.HTTP_403_FORBIDDEN)

        allowed = {k: v for k, v in request.data.items() if k in _ORG_WRITABLE_FIELDS}

        serializer = OrganizationSerializer(
            org, data=allowed, partial=True, context={'request': request},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)


class QRCodeView(APIView):
    permission_classes = [IsAuthenticated, HasActiveAccess]

    def get(self, request, pk):
        location = get_object_or_404(
            Location, id=pk, organization=request.user.organization, product=get_product(request),
        )

        if not location.qr_enabled:
            return Response(
                {'detail': 'QR code is not enabled for this location.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(f"{get_brand(location.product).frontend_url}/qr/{location.id}")
        qr.make(fit=True)

        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        response = HttpResponse(buf, content_type='image/png')
        response['Content-Disposition'] = f'inline; filename="qr-{location.id}.png"'
        return response