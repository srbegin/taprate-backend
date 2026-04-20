import io
import qrcode
from django.http import HttpResponse

from datetime import timedelta

from django.db.models import Avg, Count, FloatField
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Location, Survey, SurveySet, Incentive, SurveyResponse, Alert
from ..serializers import (
    LocationSerializer,
    SurveySerializer, SurveyWriteSerializer,
    SurveySetSerializer, SurveySetWriteSerializer,
    IncentiveSerializer,
)


# ── Locations ─────────────────────────────────────────────────────────────────

class LocationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        locations = Location.objects.filter(
            organization=request.user.organization
        ).select_related('survey_set').order_by('-created_at')
        serializer = LocationSerializer(locations, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = LocationSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save(organization=request.user.organization)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LocationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_location(self, request, pk):
        return get_object_or_404(Location, id=pk, organization=request.user.organization)

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
        self._get_location(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Survey Sets ───────────────────────────────────────────────────────────────

class SurveySetListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sets = SurveySet.objects.filter(
            organization=request.user.organization
        ).prefetch_related('surveys__incentive', 'locations').order_by('-created_at')
        return Response(SurveySetSerializer(sets, many=True).data)

    def post(self, request):
        questions_data = request.data.pop('questions', [])

        set_serializer = SurveySetWriteSerializer(data=request.data)
        if not set_serializer.is_valid():
            return Response(set_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        survey_set = set_serializer.save(organization=request.user.organization)

        for i, q_data in enumerate(questions_data):
            q_data.setdefault('position', i)
            q_ser = SurveyWriteSerializer(data=q_data)
            if q_ser.is_valid():
                q_ser.save(
                    survey_set=survey_set,
                    organization=request.user.organization,
                )

        survey_set.refresh_from_db()
        return Response(
            SurveySetSerializer(survey_set).data,
            status=status.HTTP_201_CREATED
        )


class SurveySetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_set(self, request, pk):
        return get_object_or_404(SurveySet, id=pk, organization=request.user.organization)

    def get(self, request, pk):
        return Response(SurveySetSerializer(self._get_set(request, pk)).data)

    def patch(self, request, pk):
        survey_set = self._get_set(request, pk)
        serializer = SurveySetWriteSerializer(survey_set, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        survey_set = serializer.save()
        return Response(SurveySetSerializer(survey_set).data)

    def delete(self, request, pk):
        self._get_set(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Questions (nested under SurveySet) ───────────────────────────────────────

class QuestionListView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_set(self, request, set_pk):
        return get_object_or_404(SurveySet, id=set_pk, organization=request.user.organization)

    def post(self, request, set_pk):
        survey_set = self._get_set(request, set_pk)
        serializer = SurveyWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        question = serializer.save(
            survey_set=survey_set,
            organization=request.user.organization,
        )
        return Response(SurveySerializer(question).data, status=status.HTTP_201_CREATED)


class QuestionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_question(self, request, set_pk, pk):
        return get_object_or_404(
            Survey,
            id=pk,
            survey_set_id=set_pk,
            organization=request.user.organization,
        )

    def patch(self, request, set_pk, pk):
        question = self._get_question(request, set_pk, pk)
        serializer = SurveyWriteSerializer(question, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(SurveySerializer(serializer.save()).data)

    def delete(self, request, set_pk, pk):
        self._get_question(request, set_pk, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Incentive (nested under individual question) ──────────────────────────────

class IncentiveView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_question(self, request, set_pk, survey_pk):
        return get_object_or_404(
            Survey,
            id=survey_pk,
            survey_set_id=set_pk,
            organization=request.user.organization,
        )

    def post(self, request, set_pk, survey_pk):
        survey = self._get_question(request, set_pk, survey_pk)
        if hasattr(survey, 'incentive'):
            return Response(
                {'detail': 'Incentive already exists. Use PATCH to update.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = IncentiveSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save(survey=survey)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def patch(self, request, set_pk, survey_pk):
        survey = self._get_question(request, set_pk, survey_pk)
        incentive = get_object_or_404(Incentive, survey=survey)
        serializer = IncentiveSerializer(incentive, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, set_pk, survey_pk):
        survey = self._get_question(request, set_pk, survey_pk)
        get_object_or_404(Incentive, survey=survey).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Alerts ────────────────────────────────────────────────────────────────────

class AlertListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_filter = request.query_params.get('status', 'pending')
        qs = Alert.objects.filter(
            location__organization=request.user.organization,
        ).select_related('location', 'survey_response').order_by('-created_at')
        if status_filter != 'all':
            qs = qs.filter(status=status_filter)
        return Response([self._serialize(a) for a in qs[:50]])

    @staticmethod
    def _serialize(alert):
        return {
            'id': str(alert.id),
            'location': alert.location.name,
            'location_id': str(alert.location.id),
            'rating': alert.rating,
            'comment': alert.survey_response.comment,
            'status': alert.status,
            'created_at': alert.created_at.isoformat(),
            'resolved_at': alert.resolved_at.isoformat() if alert.resolved_at else None,
        }


class AlertDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        alert = get_object_or_404(
            Alert, id=pk, location__organization=request.user.organization
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

class InsightsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.user.organization
        try:
            days = min(int(request.query_params.get('days', 30)), 90)
        except (ValueError, TypeError):
            days = 30

        location_id = request.query_params.get('location')
        now = timezone.now()
        period_start = now - timedelta(days=days)
        prev_start = period_start - timedelta(days=days)

        base_qs = SurveyResponse.objects.filter(location__organization=org)
        if location_id:
            base_qs = base_qs.filter(location_id=location_id)

        current_qs = base_qs.filter(created_at__gte=period_start)
        previous_qs = base_qs.filter(created_at__gte=prev_start, created_at__lt=period_start)

        current_agg = current_qs.aggregate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))
        previous_agg = previous_qs.aggregate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))

        current_avg = round(current_agg['avg'] or 0, 2)
        previous_avg = round(previous_agg['avg'] or 0, 2)

        daily = (
            current_qs
            .annotate(date=TruncDate('created_at'))
            .values('date')
            .annotate(avg=Avg('rating', output_field=FloatField()), count=Count('id'))
            .order_by('date')
        )
        daily_map = {
            row['date'].isoformat(): {'avg': round(row['avg'], 2), 'count': row['count']}
            for row in daily
        }
        daily_series = []
        for i in range(days):
            d = (period_start + timedelta(days=i)).date()
            key = d.isoformat()
            daily_series.append({
                'date': key,
                'avg': daily_map[key]['avg'] if key in daily_map else None,
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

        alert_qs = Alert.objects.filter(
            location__organization=org, status__in=['pending', 'owner_notified']
        ).select_related('location').order_by('-created_at')
        if location_id:
            alert_qs = alert_qs.filter(location_id=location_id)

        return Response({
            'days': days,
            'summary': {
                'avg_rating': current_avg,
                'avg_delta': round(current_avg - previous_avg, 2) if previous_avg else None,
                'total_responses': current_agg['count'],
                'count_delta': current_agg['count'] - previous_agg['count'],
            },
            'daily_series': daily_series,
            'by_location': [
                {'id': str(r['location__id']), 'name': r['location__name'],
                 'avg': round(r['avg'], 2), 'count': r['count']}
                for r in location_breakdown
            ],
            'distribution': distribution,
            'pending_alerts': [
                {'id': str(a.id), 'location': a.location.name,
                 'rating': a.rating, 'status': a.status, 'created_at': a.created_at.isoformat()}
                for a in alert_qs[:10]
            ],
        })


# ── Comment Feed ──────────────────────────────────────────────────────────────

class CommentFeedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.user.organization

        qs = (
            SurveyResponse.objects
            .filter(location__organization=org)
            .exclude(comment='')
            .select_related('location', 'survey_set')
            .order_by('-created_at')
        )

        location_id = request.query_params.get('location')
        if location_id:
            qs = qs.filter(location_id=location_id)

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = min(int(request.query_params.get('page_size', 20)), 100)
        except (ValueError, TypeError):
            page, page_size = 1, 20

        total = qs.count()
        offset = (page - 1) * page_size
        items = qs[offset:offset + page_size]

        return Response({
            'total': total,
            'page': page,
            'page_size': page_size,
            'results': [
                {
                    'id': str(r.id),
                    'comment': r.comment,
                    'rating': r.rating,
                    'created_at': r.created_at.isoformat(),
                    'location_id': str(r.location.id),
                    'location_name': r.location.name,
                    'survey_set_name': r.survey_set.name if r.survey_set else None,
                }
                for r in items
            ],
        })

class QRCodeView(APIView):
    permission_classes = [IsAuthenticated]
 
    def get(self, request, pk):
        location = get_object_or_404(Location, id=pk, organization=request.user.organization)
 
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
        qr.add_data(location.nfc_url)
        qr.make(fit=True)
 
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
 
        response = HttpResponse(buf, content_type='image/png')
        response['Content-Disposition'] = f'inline; filename="qr-{location.id}.png"'
        return response