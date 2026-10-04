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
}

# ── Plan base location allocations ────────────────────────────────────────────
# Single source of truth for included locations per plan.
# Trial/free orgs have no cap — access is gated by trial_ends_at only.
# Import this in dashboard_views.py for the location list meta.
PLAN_BASE_LOCATIONS = {
    'starter': 3,
    'growth':  10,
}

# ── Overage pricing ───────────────────────────────────────────────────────────
# Flat per-location monthly charge beyond the plan's base allocation.
OVERAGE_PRICE_PER_LOCATION = 10
OVERAGE_PRICE_ID = os.environ.get('STRIPE_PRICE_OVERAGE')  # $10/location Stripe price


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


def _sync_overage_quantity(org):
    """
    Update the overage line item quantity on the org's active Stripe subscription.

    Called after a location is created or deleted for paid subscribers.
    For trial/free orgs this is a no-op — they have no Stripe subscription yet.

    Overage = max(0, location_count - base_allocation).
    If overage is 0 the line item quantity is set to 0 (Stripe keeps the item
    but bills nothing), which avoids having to add/remove the item dynamically.
    """
    if not org.stripe_subscription_id or org.subscription_status != 'active':
        return

    base = PLAN_BASE_LOCATIONS.get(org.plan)
    if base is None:
        # Unknown plan — don't touch the subscription
        return

    from ..models import Location
    location_count = Location.objects.filter(organization=org).count()
    overage = max(0, location_count - base)

    if not OVERAGE_PRICE_ID:
        # Overage price not configured — skip silently (dev/staging env)
        return

    try:
        subscription = stripe.Subscription.retrieve(
            org.stripe_subscription_id,
            expand=['items'],
        )

        # Find existing overage item if any
        overage_item = next(
            (item for item in subscription['items']['data']
             if item['price']['id'] == OVERAGE_PRICE_ID),
            None,
        )

        if overage_item:
            if overage_item['quantity'] != overage:
                stripe.SubscriptionItem.modify(
                    overage_item['id'],
                    quantity=overage,
                    proration_behavior='always_invoice',
                )
        elif overage > 0:
            # Add the overage line item for the first time
            stripe.SubscriptionItem.create(
                subscription=org.stripe_subscription_id,
                price=OVERAGE_PRICE_ID,
                quantity=overage,
                proration_behavior='always_invoice',
            )
        # If overage == 0 and no item exists, nothing to do

    except stripe.error.StripeError:
        # Non-fatal — billing will reconcile on next cycle
        pass


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

        org = request.user.organization
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
        status_map = {
            'trialing':           'trialing',
            'active':             'active',
            'past_due':           'past_due',
            'canceled':           'canceled',
            'unpaid':             'unpaid',
            'incomplete':         'past_due',
            'incomplete_expired': 'canceled',
            'paused':             'canceled',
        }
        org.subscription_status = status_map.get(stripe_status, stripe_status)

        # Derive plan from the base price ID only (ignore overage item)
        items = subscription.get('items', {}).get('data', [])
        reverse = {v: k for k, v in PRICE_IDS.items()}
        for item in items:
            price_id = item.get('price', {}).get('id')
            if price_id in reverse:
                org.plan = reverse[price_id]
                break

        org.save(update_fields=['subscription_status', 'plan'])