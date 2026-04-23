from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from ..models import Incentive, IncentiveWin, Survey
from ..serializers import IncentiveSerializer, IncentiveWinSerializer, RedeemSerializer


# ── Incentive CRUD ─────────────────────────────────────────────────────────────

class IncentiveListCreateView(generics.ListCreateAPIView):
    serializer_class   = IncentiveSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Incentive.objects.filter(
            organization=self.request.user.organization
        ).select_related('survey').order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.organization)


class IncentiveDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = IncentiveSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Incentive.objects.filter(organization=self.request.user.organization)


class IncentiveAssignView(APIView):
    """
    PATCH /api/incentives/<id>/assign/

    Body: { "survey": "<uuid>" | null }

    Assigns or unassigns an incentive to a survey.
    Enforces single active incentive per survey by detaching any
    previously assigned incentive on the target survey.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            incentive = Incentive.objects.get(
                id=pk, organization=request.user.organization
            )
        except Incentive.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        survey_id = request.data.get('survey')

        if survey_id is None:
            incentive.survey = None
            incentive.save(update_fields=['survey'])
            return Response(IncentiveSerializer(incentive, context={'request': request}).data)

        try:
            survey = Survey.objects.get(
                id=survey_id, organization=request.user.organization
            )
        except Survey.DoesNotExist:
            return Response({'detail': 'Survey not found.'}, status=status.HTTP_400_BAD_REQUEST)

        # Detach any other incentive currently assigned to this survey
        Incentive.objects.filter(
            survey=survey,
            organization=request.user.organization,
        ).exclude(id=incentive.id).update(survey=None)

        incentive.survey = survey
        incentive.save(update_fields=['survey'])

        return Response(IncentiveSerializer(incentive, context={'request': request}).data)


# ── Redeem ─────────────────────────────────────────────────────────────────────

class RedeemValidateView(APIView):
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
            win.redeemed_at = timezone.now()
            win.redeemed_by = request.user
            win.save(update_fields=['redeemed_at', 'redeemed_by'])

        return Response({
            **IncentiveWinSerializer(win).data,
            'already_redeemed': True,
        })