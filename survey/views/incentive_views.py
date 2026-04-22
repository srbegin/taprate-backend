from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from ..models import Incentive, IncentiveWin, SurveySet
from ..serializers import IncentiveSerializer, IncentiveWinSerializer, RedeemSerializer


# ── Incentive CRUD ─────────────────────────────────────────────────────────────

class IncentiveListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/incentives/        — list all incentives for the org
    POST /api/incentives/        — create a new incentive
    """
    serializer_class   = IncentiveSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Incentive.objects.filter(
            organization=self.request.user.organization
        ).select_related('survey_set').order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.organization)


class IncentiveDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/incentives/<id>/  — retrieve
    PATCH  /api/incentives/<id>/  — update
    DELETE /api/incentives/<id>/  — delete
    """
    serializer_class   = IncentiveSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Incentive.objects.filter(organization=self.request.user.organization)


class IncentiveAssignView(APIView):
    """
    PATCH /api/incentives/<id>/assign/

    Body: { "survey_set": "<uuid>" | null }

    Assigns or unassigns an incentive to a survey set.
    Enforces single active incentive per survey set by deactivating any
    previously assigned incentive on the target set.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            incentive = Incentive.objects.get(
                id=pk, organization=request.user.organization
            )
        except Incentive.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        survey_set_id = request.data.get('survey_set')

        if survey_set_id is None:
            # Unassign
            incentive.survey_set = None
            incentive.save(update_fields=['survey_set'])
            return Response(IncentiveSerializer(incentive, context={'request': request}).data)

        try:
            survey_set = SurveySet.objects.get(
                id=survey_set_id, organization=request.user.organization
            )
        except SurveySet.DoesNotExist:
            return Response({'detail': 'Survey set not found.'}, status=status.HTTP_400_BAD_REQUEST)

        # Detach any other active incentive currently assigned to this set
        Incentive.objects.filter(
            survey_set=survey_set,
            organization=request.user.organization,
        ).exclude(id=incentive.id).update(survey_set=None)

        incentive.survey_set = survey_set
        incentive.save(update_fields=['survey_set'])

        return Response(IncentiveSerializer(incentive, context={'request': request}).data)


# ── Redeem ─────────────────────────────────────────────────────────────────────

class RedeemValidateView(APIView):
    """
    POST /api/redeem/

    Body: { "code": "ABCD1234" }

    Validates a win code belongs to the requesting user's org.
    Returns win details without marking it redeemed.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RedeemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.validated_data['code']

        try:
            win = IncentiveWin.objects.select_related(
                'incentive', 'survey_response__location', 'redeemed_by'
            ).get(
                code=code,
                incentive__organization=request.user.organization,
            )
        except IncentiveWin.DoesNotExist:
            return Response(
                {'detail': 'Code not found or does not belong to your organisation.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            **IncentiveWinSerializer(win).data,
            'already_redeemed': win.redeemed_at is not None,
        })


class RedeemUseView(APIView):
    """
    POST /api/redeem/<code>/use/

    Marks a win code as redeemed. Idempotent — calling again returns the
    existing redemption data rather than an error.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, code):
        code = code.upper().strip()

        try:
            win = IncentiveWin.objects.select_related(
                'incentive', 'survey_response__location', 'redeemed_by'
            ).get(
                code=code,
                incentive__organization=request.user.organization,
            )
        except IncentiveWin.DoesNotExist:
            return Response(
                {'detail': 'Code not found or does not belong to your organisation.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not win.redeemed_at:
            win.redeemed_at  = timezone.now()
            win.redeemed_by  = request.user
            win.save(update_fields=['redeemed_at', 'redeemed_by'])

        return Response({
            **IncentiveWinSerializer(win).data,
            'already_redeemed': True,
        })