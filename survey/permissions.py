from rest_framework.permissions import BasePermission

from .products import get_brand, get_product


class HasActiveAccess(BasePermission):
    """
    Allows access only if the user's organization has an active subscription
    or an unexpired trial for the product this request is for (X-Product
    header — see products.py). Returns 403 with code='subscription_required'
    when blocked so the frontend can redirect to /dashboard/billing.

    Applied to all dashboard, incentive, and owner-facing tag views. Auth,
    billing, admin, and public survey endpoints are intentionally excluded.
    """
    message = {
        'detail': 'Your trial has ended. Subscribe to continue.',
        'code':   'subscription_required',
    }

    def has_permission(self, request, view):
        org = getattr(request.user, 'organization', None)
        if not org:
            return False
        product = get_product(request)
        if org.has_access(product):
            return True
        self.message = {
            'detail': f'Your trial has ended. Subscribe to continue using {get_brand(product).name}.',
            'code':   'subscription_required',
        }
        return False
