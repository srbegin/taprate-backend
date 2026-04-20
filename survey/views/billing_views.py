import os
import stripe
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

PRICE_IDS = {
    'starter': os.environ.get('STRIPE_PRICE_STARTER'),
    'growth':  os.environ.get('STRIPE_PRICE_GROWTH'),
    'pro':     os.environ.get('STRIPE_PRICE_PRO'),
}

PLAN_LOCATION_LIMITS = {
    'starter': 3,
    'growth':  10,
    'pro':     None,  # unlimited
}


def _get_or_create_customer(org, user):
    """Return existing Stripe customer ID or create a new one."""
    if org.stripe_customer_id:
        return org.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email,
        name=org.name,
        metadata={'org_id': str(org.id)},
    )
    org.stripe_customer_id = customer.id
    org.save(update_fields=['stripe_customer_id'])
    return customer.id


class CheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        plan = request.data.get('plan', '').lower()
        if plan not in PRICE_IDS:
            return Response(
                {'detail': f"Invalid plan. Choose from: {', '.join(PRICE_IDS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        price_id = PRICE_IDS[plan]
        if not price_id:
            return Response(
                {'detail': 'Plan price not configured.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        org  = request.user.organization
        frontend_url = os.environ.get('FRONTEND_URL', 'https://taprate.app')

        try:
            customer_id = _get_or_create_customer(org, request.user)
            session = stripe.checkout.Session.create(
                customer=customer_id,
                payment_method_types=['card'],
                line_items=[{'price': price_id, 'quantity': 1}],
                mode='subscription',
                success_url=f"{frontend_url}/dashboard/billing?success=1",
                cancel_url=f"{frontend_url}/dashboard/billing?canceled=1",
                metadata={'org_id': str(org.id), 'plan': plan},
                subscription_data={
                    'metadata': {'org_id': str(org.id), 'plan': plan},
                },
            )
        except stripe.error.StripeError as e:
            return Response({'detail': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'url': session.url})


class PortalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        org = request.user.organization
        if not org.stripe_customer_id:
            return Response(
                {'detail': 'No billing account found.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        frontend_url = os.environ.get('FRONTEND_URL', 'https://taprate.app')

        try:
            session = stripe.billing_portal.Session.create(
                customer=org.stripe_customer_id,
                return_url=f"{frontend_url}/dashboard/billing",
            )
        except stripe.error.StripeError as e:
            return Response({'detail': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'url': session.url})


@method_decorator(csrf_exempt, name='dispatch')
class WebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')

        try:
            event = stripe.Webhook.construct_event(
                request.body, sig_header, webhook_secret
            )
        except (ValueError, stripe.error.SignatureVerificationError):
            return Response(status=status.HTTP_400_BAD_REQUEST)

        self._handle(event)
        return Response({'status': 'ok'})

    def _handle(self, event):
        from ..models import Organization

        etype = event['type']
        data = event._to_dict_recursive()['data']['object']

        # ── Checkout completed → subscription created ─────────────────────
        if etype == 'checkout.session.completed':
            org_id = data.get('metadata', {}).get('org_id')
            plan   = data.get('metadata', {}).get('plan')
            sub_id = data.get('subscription')
            if not org_id:
                return
            try:
                org = Organization.objects.get(id=org_id)
                org.stripe_subscription_id = sub_id or ''
                org.plan                   = plan or org.plan
                org.subscription_status    = 'active'
                org.save(update_fields=[
                    'stripe_subscription_id', 'plan', 'subscription_status'
                ])
            except Organization.DoesNotExist:
                pass

        # ── Subscription updated (plan change, renewal, past_due, etc.) ───
        elif etype == 'customer.subscription.updated':
            self._sync_subscription(data)

        # ── Subscription deleted (canceled) ───────────────────────────────
        elif etype == 'customer.subscription.deleted':
            self._sync_subscription(data)

    def _sync_subscription(self, subscription):
        from ..models import Organization

        org_id = subscription.get('metadata', {}).get('org_id')
        if not org_id:
            return

        try:
            org = Organization.objects.get(id=org_id)
        except Organization.DoesNotExist:
            return

        stripe_status = subscription.get('status', '')
        # Map Stripe status → our subscription_status choices
        status_map = {
            'trialing':          'trialing',
            'active':            'active',
            'past_due':          'past_due',
            'canceled':          'canceled',
            'unpaid':            'unpaid',
            'incomplete':        'past_due',
            'incomplete_expired':'canceled',
            'paused':            'canceled',
        }
        org.subscription_status = status_map.get(stripe_status, stripe_status)

        # Derive plan from price ID
        items = subscription.get('items', {}).get('data', [])
        if items:
            price_id = items[0].get('price', {}).get('id')
            reverse  = {v: k for k, v in PRICE_IDS.items()}
            if price_id in reverse:
                org.plan = reverse[price_id]

        org.save(update_fields=['subscription_status', 'plan'])