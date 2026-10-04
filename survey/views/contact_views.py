"""
contact_views.py

Handles:
  - ContactSubmissionView   POST /api/contact/
  - DemoSessionView         POST /api/demo/session/ (redirects to /s/<location_id>)
  - AdminContactListView    GET  /api/admin/contacts/
  - AdminContactDetailView  GET/PATCH /api/admin/contacts/<uuid>/
"""
import os
import uuid
import json
import logging

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import ContactSubmission, Location
from ..products import TAPRATE, get_brand, get_product

logger = logging.getLogger(__name__)

SESSION_TTL = 60 * 30  # 30 minutes — matches survey_views.py


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


def _send_notification(submission):
    """
    Internal new-lead notification. Always sent from the TapRate (verified)
    domain to the ops inbox (CONTACT_NOTIFY_EMAIL), whichever brand the lead
    came from — so Cleanpulse leads arrive before cleanpulse.app is verified.
    """
    try:
        import resend
        resend.api_key = os.environ.get('RESEND_API_KEY', '')

        ops        = get_brand(TAPRATE)
        lead_brand = get_brand(submission.product)
        notify_to  = os.environ.get('CONTACT_NOTIFY_EMAIL', ops.hello_email)

        body = f"""
<p>New contact form submission on {lead_brand.name}:</p>
<table style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Name</td><td>{submission.name}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Business</td><td>{submission.business_name}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Email</td><td>{submission.email}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Phone</td><td>{submission.phone or '—'}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Locations</td><td>{submission.location_count}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Message</td><td>{submission.message or '—'}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">IP</td><td>{submission.ip_address or '—'}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;">Submitted</td><td>{submission.submitted_at.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
</table>
""".strip()

        resend.Emails.send({
            'from': f'{ops.name} <{ops.hello_email}>',
            'to': [notify_to],
            'subject': f'[{lead_brand.name}] New contact: {submission.business_name}',
            'html': body,
        })
    except Exception:
        logger.exception('Failed to send contact notification email for %s', submission.id)


def _send_autoreply(submission):
    """Send auto-reply confirmation to the prospect, from the lead's brand."""
    brand = get_brand(submission.product)
    if not brand.email_enabled:
        logger.info('Email disabled for %s — skipping contact auto-reply to %s', brand.name, submission.email)
        return
    try:
        import resend
        resend.api_key = os.environ.get('RESEND_API_KEY', '')

        body = f"""
<p>Hi {submission.name},</p>
<p>Thanks for reaching out about {brand.name}! We received your message and will be in touch within 1 business day.</p>
<p>If you have any urgent questions in the meantime, you can reply directly to this email.</p>
<p>— The {brand.name} Team</p>
""".strip()

        resend.Emails.send({
            'from': f'{brand.name} <{brand.hello_email}>',
            'to': [submission.email],
            'reply_to': brand.hello_email,
            'subject': f'Thanks for reaching out to {brand.name}',
            'html': body,
        })
    except Exception:
        logger.exception('Failed to send contact auto-reply to %s', submission.email)


# ── Contact form submission ───────────────────────────────────────────────────

class ContactSubmissionView(APIView):
    """POST /api/contact/ — public contact form intake."""
    permission_classes = [AllowAny]

    REQUIRED_FIELDS = ['name', 'business_name', 'email']
    VALID_LOCATION_COUNTS = ['1', '2-5', '6-20', '20+']
    RATE_LIMIT_MAX = 5
    RATE_LIMIT_TTL = 3600  # 1 hour

    def post(self, request):
        ip = _get_client_ip(request)

        # ── Honeypot check — bot filled hidden field, silently succeed
        if request.data.get('website'):
            return Response({'success': True}, status=status.HTTP_200_OK)

        # ── IP rate limit
        rate_key = f'contact_submit:{ip}'
        count = cache.get(rate_key, 0)
        if count >= self.RATE_LIMIT_MAX:
            return Response(
                {'detail': 'Too many submissions. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # ── Validate
        data = request.data
        errors = {}

        for field in self.REQUIRED_FIELDS:
            if not str(data.get(field, '')).strip():
                errors[field] = 'This field is required.'

        email = str(data.get('email', '')).strip()
        if email and '@' not in email:
            errors['email'] = 'Enter a valid email address.'

        location_count = str(data.get('location_count', '')).strip()
        if location_count and location_count not in self.VALID_LOCATION_COUNTS:
            errors['location_count'] = 'Invalid value.'

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        # ── Persist
        submission = ContactSubmission.objects.create(
            product=get_product(request),
            name=str(data.get('name', '')).strip(),
            business_name=str(data.get('business_name', '')).strip(),
            email=email,
            phone=str(data.get('phone', '')).strip(),
            location_count=location_count,
            message=str(data.get('message', '')).strip(),
            ip_address=ip or None,
        )

        # ── Increment rate limit counter
        if count == 0:
            cache.set(rate_key, 1, timeout=self.RATE_LIMIT_TTL)
        else:
            cache.incr(rate_key)

        # ── Fire emails (non-blocking — failures are logged, not raised)
        _send_notification(submission)
        _send_autoreply(submission)

        return Response({'success': True}, status=status.HTTP_201_CREATED)


# ── Demo session ──────────────────────────────────────────────────────────────

class DemoSessionView(APIView):
    """
    POST /api/demo/session/
    Mints a real session token against the demo location (same mechanism as
    QrSessionView) and returns { token } for the frontend to redirect to
    /s/{token}. Source is 'demo' so responses are not marked is_test.
    No rate limit — demo is the point.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        demo_location_id = os.environ.get('DEMO_LOCATION_ID')
        if not demo_location_id:
            return Response(
                {'detail': 'Demo not configured.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            location = Location.objects.get(id=demo_location_id)
        except Location.DoesNotExist:
            return Response(
                {'detail': 'Demo location not found.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not location.survey:
            return Response(
                {'detail': 'No survey is configured for the demo location.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        token = str(uuid.uuid4())
        session_data = json.dumps({
            'location_id': str(location.id),
            'tag_id':      None,
            'source':      'demo',
        })
        cache.set(f'survey_session:{token}', session_data, SESSION_TTL)

        return Response({'token': token}, status=status.HTTP_201_CREATED)


# ── Admin contact views ───────────────────────────────────────────────────────

def _serialize_submission(s):
    return {
        'id':             str(s.id),
        'product':        s.product,
        'name':           s.name,
        'business_name':  s.business_name,
        'email':          s.email,
        'phone':          s.phone,
        'location_count': s.location_count,
        'message':        s.message,
        'status':         s.status,
        'notes':          s.notes,
        'submitted_at':   s.submitted_at.isoformat(),
        'contacted_at':   s.contacted_at.isoformat() if s.contacted_at else None,
        'ip_address':     str(s.ip_address) if s.ip_address else None,
    }


class AdminContactListView(APIView):
    """GET /api/admin/contacts/ — paginated, filterable by status."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = ContactSubmission.objects.all()

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = min(int(request.query_params.get('page_size', 25)), 100)
        except (ValueError, TypeError):
            page, page_size = 1, 25

        total = qs.count()
        offset = (page - 1) * page_size
        items = qs[offset:offset + page_size]

        return Response({
            'total':     total,
            'page':      page,
            'page_size': page_size,
            'results':   [_serialize_submission(s) for s in items],
        })


class AdminContactDetailView(APIView):
    """GET/PATCH /api/admin/contacts/<uuid>/"""
    permission_classes = [IsAdminUser]

    def _get(self, pk):
        return get_object_or_404(ContactSubmission, id=pk)

    def get(self, request, pk):
        return Response(_serialize_submission(self._get(pk)))

    def patch(self, request, pk):
        submission = self._get(pk)
        new_status = request.data.get('status')
        new_notes = request.data.get('notes')

        valid_statuses = ['new', 'contacted', 'converted', 'declined', 'spam']

        if new_status is not None:
            if new_status not in valid_statuses:
                return Response(
                    {'detail': f'Invalid status. Choose from: {", ".join(valid_statuses)}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Stamp contacted_at on first transition to 'contacted'
            if new_status == 'contacted' and submission.status != 'contacted':
                submission.contacted_at = timezone.now()
            submission.status = new_status

        if new_notes is not None:
            submission.notes = new_notes

        submission.save()
        return Response(_serialize_submission(submission))