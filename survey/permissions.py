from rest_framework.permissions import BasePermission


class HasActiveAccess(BasePermission):
    """
    Allows access only if the user's organization has an active subscription
    or an unexpired trial. Returns 403 with code='subscription_required' when
    access is blocked so the frontend can redirect to /dashboard/billing.

    Applied to all dashboard, incentive, and owner-facing tag views. Auth,
    billing, admin, and public survey endpoints are intentionally excluded.
    """
    message = {
        'detail': 'Your trial has ended. Subscribe to continue using TapRate.',
        'code':   'subscription_required',
    }

    def has_permission(self, request, view):
        org = getattr(request.user, 'organization', None)
        if not org:
            return False
        return org.is_access_allowed()