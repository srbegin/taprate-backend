from datetime import timedelta

from django.db.models import Avg, Count, FloatField
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Location, Survey, Incentive, SurveyResponse, Alert
from ..serializers import (
    LocationSerializer,
    SurveySerializer,
    SurveyWriteSerializer,
    IncentiveSerializer,
)


# ── Locations ────────────────────────────────────────────────────────────────

class LocationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        locations = Location.objects.filter(
            organization=request.user.organization
        ).select_related('survey').order_by('-created_at')
        serializer = LocationSerializer(
            locations, many=True, context={'request': request}
        )
        return Response(serializer.data)

    def post(self, request):
        serializer = LocationSerializer(
            data=request.data, context={'request': request}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save(organization=request.user.organization)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class LocationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_location(self, request, pk):
        return get_object_or_404(
            Location, id=pk, organization=request.user.organization
        )

    def patch(self, request, pk):
        location = self._get_location(request, pk)
        serializer = LocationSerializer(
            location, data=request.data,
            partial=True, context={'request': request}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        location = self._get_location(request, pk)
        location.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Surveys ──────────────────────────────────────────────────────────────────

class SurveyListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        surveys = Survey.objects.filter(
            organization=request.user.organization
        ).prefetch_related('locations').order_by('-created_at')
        serializer = SurveySerializer(surveys, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = SurveyWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        survey = serializer.save(organization=request.user.organization)
        return Response(SurveySerializer(survey).data, status=status.HTTP_201_CREATED)


class SurveyDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_survey(self, request, pk):
        return get_object_or_404(
            Survey, id=pk, organization=request.user.organization
        )

    def get(self, request, pk):
        survey = self._get_survey(request, pk)
        return Response(SurveySerializer(survey).data)

    def patch(self, request, pk):
        survey = self._get_survey(request, pk)
        serializer = SurveyWriteSerializer(survey, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        survey = serializer.save()
        return Response(SurveySerializer(survey).data)

    def delete(self, request, pk):
        survey = self._get_survey(request, pk)
        survey.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Incentive ─────────────────────────────────────────────────────────────────

class IncentiveView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_survey(self, request, survey_pk):
        return get_object_or_404(
            Survey, id=survey_pk, organization=request.user.organization
        )

    def post(self, request, survey_pk):
        survey = self._get_survey(request, survey_pk)
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

    def patch(self, request, survey_pk):
        survey = self._get_survey(request, survey_pk)
        incentive = get_object_or_404(Incentive, survey=survey)
        serializer = IncentiveSerializer(incentive, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, survey_pk):
        survey = self._get_survey(request, survey_pk)
        incentive = get_object_or_404(Incentive, survey=survey)
        incentive.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Alerts ────────────────────────────────────────────────────────────────────

class AlertListView(APIView):
    """
    GET /api/dashboard/alerts/
    Returns all pending alerts for the org, newest first.
    Optional ?status=pending|sent|resolved filter.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_filter = request.query_params.get('status', 'pending')
        qs = Alert.objects.filter(
            location__organization=request.user.organization,
        ).select_related('location', 'survey_response').order_by('-created_at')

        if status_filter != 'all':
            qs = qs.filter(status=status_filter)

        return Response([
            self._serialize(a) for a in qs[:50]
        ])

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
    """
    PATCH /api/dashboard/alerts/<pk>/
    Accepts { "status": "resolved" } to resolve an alert.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        alert = get_object_or_404(
            Alert,
            id=pk,
            location__organization=request.user.organization,
        )
        new_status = request.data.get('status')
        if new_status not in ('pending', 'resolved'):
            return Response(
                {'detail': 'status must be "pending" or "resolved".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        alert.status = new_status
        if new_status == 'resolved':
            alert.resolved_at = timezone.now()
        else:
            alert.resolved_at = None
        alert.save(update_fields=['status', 'resolved_at'])
        return Response(AlertListView._serialize(alert))


# ── Insights ──────────────────────────────────────────────────────────────────

class InsightsView(APIView):
    """
    GET /api/dashboard/insights/?days=30&location=<uuid>
    """
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

        current_agg = current_qs.aggregate(
            avg=Avg('rating', output_field=FloatField()),
            count=Count('id'),
        )
        previous_agg = previous_qs.aggregate(
            avg=Avg('rating', output_field=FloatField()),
            count=Count('id'),
        )

        current_avg = round(current_agg['avg'] or 0, 2)
        previous_avg = round(previous_agg['avg'] or 0, 2)
        avg_delta = round(current_avg - previous_avg, 2) if previous_avg else None
        count_delta = current_agg['count'] - previous_agg['count']

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
            location__organization=org,
            status='pending',
        ).select_related('location').order_by('-created_at')
        if location_id:
            alert_qs = alert_qs.filter(location_id=location_id)

        return Response({
            'days': days,
            'summary': {
                'avg_rating': current_avg,
                'avg_delta': avg_delta,
                'total_responses': current_agg['count'],
                'count_delta': count_delta,
            },
            'daily_series': daily_series,
            'by_location': [
                {
                    'id': str(row['location__id']),
                    'name': row['location__name'],
                    'avg': round(row['avg'], 2),
                    'count': row['count'],
                }
                for row in location_breakdown
            ],
            'distribution': distribution,
            'pending_alerts': [
                {
                    'id': str(a.id),
                    'location': a.location.name,
                    'rating': a.rating,
                    'created_at': a.created_at.isoformat(),
                }
                for a in alert_qs[:10]
            ],
        })