import os
from datetime import timedelta

import stripe
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from ..products import CLEANPULSE, TAPRATE, DEFAULT_PRODUCT, PRODUCTS, get_brand, get_product

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

# ── Plans, per product ────────────────────────────────────────────────────────
# Cleanpulse and TapRate are separate purchases with separate prices.
# Cleanpulse pricing is not decided yet — its checkout is disabled until
# prices are configured here.
PRICE_IDS = {
    TAPRATE: {
        'starter': os.environ.get('STRIPE_PRICE_STARTER'),
        'growth':  os.environ.get('STRIPE_PRICE_GROWTH'),
    },
    CLEANPULSE: {},
}

# ── Plan base location allocations ────────────────────────────────────────────
# Single source of truth for included locations per plan, per product.
# Trialing orgs have no cap — access is gated by the subscription's
# trial_ends_at only. Never add a 'free' entry here.
PLAN_BASE_LOCATIONS = {
    TAPRATE: {
        'starter': 3,
        'growth':  10,
    },
    CLEANPULSE: {},
}

# ── Overage pricing ───────────────────────────────────────────────────────────
# Flat per-location monthly charge beyond the plan's base allocation.
OVERAGE_PRICE_PER_LOCATION = {
    TAPRATE: 10,
}
OVERAGE_PRICE_IDS = {
    TAPRATE: os.environ.get('STRIPE_PRICE_OVERAGE'),  # $10/location Stripe price
}

TRIAL_DAYS = int(os.environ.get('TRIAL_DAYS', 30))


def base_locations(product, plan):
    """Included locations for a plan, or None (no cap / unknown plan)."""
    return PLAN_BASE_LOCATIONS.get(product, {}).get(plan)


def location_usage(org, product):
    """(location_count, base_allocation or None, overage_count) for one product."""
    from ..models import Location
    sub   = org.get_subscription(product)
    count = Location.objects.filter(organization=org, product=product).count()
    base  = base_locations(product, sub.plan) if sub else None
    overage = max(0, count - base) if base is not None else 0
    return count, base, overage


def start_trial(org, product):
    """Create a trial subscription for a product the org doesn't have yet."""
    from ..models import Subscription
    sub, _ = Subscription.objects.get_or_create(
        organization=org,
        product=product,
        defaults={
            'status':        'trialing',
            'trial_ends_at': timezone.now() + timedelta(days=TRIAL_DAYS),
        },
    )
    return sub


def _get_or_create_customer(org, user):
    """Return existing Stripe customer ID or create a new one (shared by both products)."""
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


def _sync_overage_quantity(org, product):
    """
    Update the overage line item quantity on the org's active Stripe
    subscription for one product.

    Called after a location is created or deleted. For trialing orgs this is
    a no-op — they have no Stripe subscription yet.

    Overage = max(0, location_count - base_allocation).
    If overage is 0 the line item quantity is set to 0 (Stripe keeps the item
    but bills nothing), which avoids having to add/remove the item dynamically.
    """
    sub = org.get_subscription(product)
    if not sub or not sub.stripe_subscription_id or sub.status != 'active':
        return

    _, base, overage = location_usage(org, product)
    if base is None:
        # Unknown plan — don't touch the subscription
        return

    overage_price_id = OVERAGE_PRICE_IDS.get(product)
    if not overage_price_id:
        # Overage price not configured — skip silently (dev/staging env)
        return

    try:
        subscription = stripe.Subscription.retrieve(
            sub.stripe_subscription_id,
            expand=['items'],
        )

        # Find existing overage item if any
        overage_item = next(
            (item for item in subscription['items']['data']
             if item['price']['id'] == overage_price_id),
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
                subscription=sub.stripe_subscription_id,
                price=overage_price_id,
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
        product = get_product(request)
        brand   = get_brand(product)
        plans   = PRICE_IDS.get(product, {})

        if not plans:
            return Response(
                {'detail': f"Subscriptions for {brand.name} aren't available yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plan = request.data.get('plan', '').lower()
        if plan not in plans:
            return Response(
                {'detail': f"Invalid plan. Choose from: {', '.join(plans)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        price_id = plans[plan]
        if not price_id:
            return Response(
                {'detail': 'Plan price not configured.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        org = request.user.organization
        sub = org.get_subscription(product)
        if sub and sub.status == 'active':
            return Response(
                {'detail': f'You already have an active {brand.name} subscription. '
                           'Use the billing portal to change plans.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        metadata = {'org_id': str(org.id), 'plan': plan, 'product': product}

        try:
            customer_id = _get_or_create_customer(org, request.user)
            session = stripe.checkout.Session.create(
                customer=customer_id,
                payment_method_types=['card'],
                line_items=[{'price': price_id, 'quantity': 1}],
                mode='subscription',
                success_url=f"{brand.frontend_url}/dashboard/billing?success=1",
                cancel_url=f"{brand.frontend_url}/dashboard/billing?canceled=1",
                metadata=metadata,
                subscription_data={'metadata': metadata},
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

        brand = get_brand(get_product(request))

        try:
            session = stripe.billing_portal.Session.create(
                customer=org.stripe_customer_id,
                return_url=f"{brand.frontend_url}/dashboard/billing",
            )
        except stripe.error.StripeError as e:
            return Response({'detail': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'url': session.url})


class StartTrialView(APIView):
    """
    POST /api/billing/trial/ — start a trial of the request's product (X-Product)
    for an org that doesn't have that product yet, e.g. a TapRate customer
    opening Cleanpulse for the first time. Idempotent: returns the existing
    subscription if there is one.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        org = request.user.organization
        if not org:
            return Response({'detail': 'No organization found.'}, status=status.HTTP_404_NOT_FOUND)

        product = get_product(request)
        existing = org.get_subscription(product)
        sub = existing or start_trial(org, product)

        return Response(
            {
                'product':              sub.product,
                'status':               sub.status,
                'plan':                 sub.plan,
                'trial_ends_at':        sub.trial_ends_at,
                'trial_days_remaining': sub.trial_days_remaining,
            },
            status=status.HTTP_200_OK if existing else status.HTTP_201_CREATED,
        )


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

    @staticmethod
    def _metadata_product(metadata):
        # Subscriptions created before the product split carry no product → TapRate.
        product = (metadata or {}).get('product') or DEFAULT_PRODUCT
        return product if product in PRODUCTS else None

    def _handle(self, event):
        from ..models import Organization, Subscription

        etype = event['type']
        data = event._to_dict_recursive()['data']['object']

        # ── Checkout completed → subscription created ─────────────────────
        if etype == 'checkout.session.completed':
            metadata = data.get('metadata', {})
            org_id   = metadata.get('org_id')
            plan     = metadata.get('plan')
            product  = self._metadata_product(metadata)
            sub_id   = data.get('subscription')
            if not org_id or not product:
                return
            try:
                org = Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                return
            sub, _ = Subscription.objects.get_or_create(organization=org, product=product)
            sub.stripe_subscription_id = sub_id or ''
            sub.plan                   = plan or sub.plan
            sub.status                 = 'active'
            sub.save(update_fields=['stripe_subscription_id', 'plan', 'status'])

        # ── Subscription updated (plan change, renewal, past_due, etc.) ───
        elif etype == 'customer.subscription.updated':
            self._sync_subscription(data)

        # ── Subscription deleted (canceled) ───────────────────────────────
        elif etype == 'customer.subscription.deleted':
            self._sync_subscription(data)

    def _sync_subscription(self, subscription):
        from ..models import Organization, Subscription

        # Prefer matching on the Stripe subscription ID; fall back to metadata.
        sub = None
        stripe_sub_id = subscription.get('id')
        if stripe_sub_id:
            sub = Subscription.objects.filter(stripe_subscription_id=stripe_sub_id).first()

        if sub is None:
            metadata = subscription.get('metadata', {})
            org_id   = metadata.get('org_id')
            product  = self._metadata_product(metadata)
            if not org_id or not product:
                return
            try:
                org = Organization.objects.get(id=org_id)
            except Organization.DoesNotExist:
                return
            sub, _ = Subscription.objects.get_or_create(organization=org, product=product)
            if stripe_sub_id:
                sub.stripe_subscription_id = stripe_sub_id

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
        sub.status = status_map.get(stripe_status, stripe_status)

        # Derive plan from the base price ID only (ignore overage item)
        items = subscription.get('items', {}).get('data', [])
        reverse = {v: k for k, v in PRICE_IDS.get(sub.product, {}).items() if v}
        for item in items:
            price_id = item.get('price', {}).get('id')
            if price_id in reverse:
                sub.plan = reverse[price_id]
                break

        sub.save(update_fields=['stripe_subscription_id', 'status', 'plan'])
