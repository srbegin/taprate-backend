"""
Celery tasks for Cleanpulse and TapRate.

Required environment variables:
    RESEND_API_KEY     — Resend API key

Sender addresses, site URLs and per-brand email on/off switches come from the
brand config in products.py.
"""

import os
import logging
from html import escape
from celery import shared_task
from django.core.management import call_command

from .products import DEFAULT_PRODUCT, get_brand

logger = logging.getLogger(__name__)


def _send_email(*, to: str, subject: str, html_body: str, text_body: str,
                product: str = DEFAULT_PRODUCT):
    """
    Send a transactional email via Resend, from the product's brand.
    Returns True on success, False on failure (caller retries), or None when
    email is switched off for that brand (not an error — don't retry).
    """
    brand = get_brand(product)
    if not brand.email_enabled:
        logger.info(f'Email disabled for {brand.name} — not sending "{subject}" to {to}')
        return None

    import resend

    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        logger.error('RESEND_API_KEY not set — skipping email send')
        return False

    resend.api_key = api_key

    try:
        response = resend.Emails.send({
            'from': f'{brand.name} <{brand.alerts_from_email}>',
            'to': [to],
            'subject': subject,
            'html': html_body,
            'text': text_body,
        })
        if not response.get('id'):
            logger.error(f'Resend returned unexpected response: {response}')
            return False
        return True
    except Exception as e:
        logger.error(f'Resend exception: {e}')
        return False


# ── Welcome email ──────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_welcome_email(self, user_id, product=DEFAULT_PRODUCT):
    """
    Fired immediately after a new account is created via RegisterView.
    Sends a branded welcome email with quick-start steps.
    """
    from .models import User

    try:
        user = User.objects.select_related('organization').get(id=user_id)
    except User.DoesNotExist:
        logger.warning(f'send_welcome_email: User {user_id} not found')
        return

    first_name   = user.first_name or 'there'
    org_name     = user.organization.name if user.organization else 'your organization'
    brand        = get_brand(product)
    dashboard_url = f"{brand.frontend_url}/dashboard"

    subject = f"Welcome to {brand.name}, {first_name}!"

    text_body = (
        f"Hi {first_name},\n\n"
        f"Your {brand.name} account for {org_name} is ready.\n\n"
        f"Here's how to get started:\n"
        f"1. Create a location — a spot where you'll collect feedback\n"
        f"2. Set up a survey — star ratings, comments, review redirects\n"
        f"3. Claim your NFC tag or download a QR code\n"
        f"4. Place it at your location and start collecting responses\n\n"
        f"Head to your dashboard to get started:\n{dashboard_url}\n\n"
        f"Questions? Reply to this email — we're happy to help.\n\n"
        f"— The {brand.name} team"
    )

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">

      <p style="font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
                color: #888; margin: 0 0 24px;">{brand.name}</p>

      <h1 style="font-size: 24px; font-weight: 600; margin: 0 0 8px; color: #111;">
        Welcome, {first_name}! 👋
      </h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 28px; line-height: 1.6;">
        Your {brand.name} account for <strong>{org_name}</strong> is ready.
        Here's how to collect your first piece of feedback.
      </p>

      <!-- Steps -->
      <div style="margin-bottom: 28px;">

        <div style="display: flex; align-items: flex-start; margin-bottom: 16px;">
          <div style="min-width: 28px; height: 28px; border-radius: 50%; background: #111;
                      color: #fff; font-size: 12px; font-weight: 600; display: flex;
                      align-items: center; justify-content: center; margin-right: 12px;
                      margin-top: 1px; flex-shrink: 0; text-align: center; line-height: 28px;">
            1
          </div>
          <div>
            <p style="margin: 0 0 2px; font-size: 14px; font-weight: 600; color: #111;">Create a location</p>
            <p style="margin: 0; font-size: 13px; color: #777;">A location is any physical spot where you want to collect feedback — a room, desk, or entry point.</p>
          </div>
        </div>

        <div style="display: flex; align-items: flex-start; margin-bottom: 16px;">
          <div style="min-width: 28px; height: 28px; border-radius: 50%; background: #111;
                      color: #fff; font-size: 12px; font-weight: 600; display: flex;
                      align-items: center; justify-content: center; margin-right: 12px;
                      margin-top: 1px; flex-shrink: 0; text-align: center; line-height: 28px;">
            2
          </div>
          <div>
            <p style="margin: 0 0 2px; font-size: 14px; font-weight: 600; color: #111;">Set up a survey</p>
            <p style="margin: 0; font-size: 13px; color: #777;">Choose a rating scale, enable comments, and optionally redirect happy customers to leave a public review.</p>
          </div>
        </div>

        <div style="display: flex; align-items: flex-start; margin-bottom: 16px;">
          <div style="min-width: 28px; height: 28px; border-radius: 50%; background: #111;
                      color: #fff; font-size: 12px; font-weight: 600; display: flex;
                      align-items: center; justify-content: center; margin-right: 12px;
                      margin-top: 1px; flex-shrink: 0; text-align: center; line-height: 28px;">
            3
          </div>
          <div>
            <p style="margin: 0 0 2px; font-size: 14px; font-weight: 600; color: #111;">Claim your NFC tag or download a QR code</p>
            <p style="margin: 0; font-size: 13px; color: #777;">Tap the tag with your phone to claim it, or download a print-ready QR code from your location settings.</p>
          </div>
        </div>

        <div style="display: flex; align-items: flex-start;">
          <div style="min-width: 28px; height: 28px; border-radius: 50%; background: #7c3aed;
                      color: #fff; font-size: 12px; font-weight: 600; display: flex;
                      align-items: center; justify-content: center; margin-right: 12px;
                      margin-top: 1px; flex-shrink: 0; text-align: center; line-height: 28px;">
            4
          </div>
          <div>
            <p style="margin: 0 0 2px; font-size: 14px; font-weight: 600; color: #111;">Place it and go live</p>
            <p style="margin: 0; font-size: 13px; color: #777;">Put the tag or QR code at your location. Responses and alerts start flowing into your dashboard immediately.</p>
          </div>
        </div>

      </div>

      <a href="{dashboard_url}"
         style="display: inline-block; background: #7c3aed; color: #fff; text-decoration: none;
                font-size: 13px; font-weight: 500; padding: 12px 24px; border-radius: 8px;">
        Go to your dashboard →
      </a>

      <p style="font-size: 13px; color: #777; margin: 28px 0 0; line-height: 1.6;">
        Questions? Just reply to this email — we read every one.
      </p>

      <p style="font-size: 12px; color: #bbb; margin: 24px 0 0;">
        — The {brand.name} team
      </p>
    </div>
    """

    success = _send_email(
        to=user.email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        product=product,
    )

    if success is False:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            logger.error(f'send_welcome_email: max retries exceeded for user {user_id}')


# ── Password reset email ───────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_password_reset_email(self, user_id, reset_url, product=DEFAULT_PRODUCT):
    """
    Fired by PasswordResetRequestView after generating a signed reset token.
    The reset_url contains the uid + token and is valid for PASSWORD_RESET_TIMEOUT
    seconds (Django default: 3 days). The token is single-use — it's invalidated
    the moment the password is changed.
    """
    from .models import User

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning(f'send_password_reset_email: User {user_id} not found')
        return

    brand      = get_brand(product)
    first_name = user.first_name or 'there'
    subject    = f'Reset your {brand.name} password'

    text_body = (
        f"Hi {first_name},\n\n"
        f"We received a request to reset the password for your {brand.name} account.\n\n"
        f"Click the link below to set a new password. This link expires in 3 days "
        f"and can only be used once.\n\n"
        f"{reset_url}\n\n"
        f"If you didn't request this, you can safely ignore this email — "
        f"your password won't be changed.\n\n"
        f"— The {brand.name} team"
    )

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">

      <p style="font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
                color: #888; margin: 0 0 24px;">{brand.name}</p>

      <h1 style="font-size: 22px; font-weight: 600; margin: 0 0 8px; color: #111;">
        Reset your password
      </h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 24px; line-height: 1.6;">
        Hi {first_name} — we received a request to reset the password on your {brand.name} account.
        Click the button below to choose a new one.
      </p>

      <a href="{reset_url}"
         style="display: inline-block; background: #7c3aed; color: #fff; text-decoration: none;
                font-size: 13px; font-weight: 500; padding: 12px 24px; border-radius: 8px;
                margin-bottom: 24px;">
        Reset my password →
      </a>

      <div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px;
                  padding: 14px 16px; margin-bottom: 24px;">
        <p style="margin: 0; font-size: 12px; color: #888; line-height: 1.6;">
          This link expires in <strong style="color: #555;">3 days</strong> and can only be used once.
          If the button doesn't work, copy and paste this URL into your browser:
        </p>
        <p style="margin: 8px 0 0; font-size: 11px; color: #aaa; word-break: break-all;">
          {reset_url}
        </p>
      </div>

      <p style="font-size: 13px; color: #999; margin: 0; line-height: 1.6;">
        If you didn't request a password reset, you can safely ignore this email.
        Your password will not be changed.
      </p>

      <p style="font-size: 12px; color: #bbb; margin: 24px 0 0;">
        — The {brand.name} team
      </p>
    </div>
    """

    success = _send_email(
        to=user.email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        product=product,
    )

    if success is False:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            logger.error(f'send_password_reset_email: max retries exceeded for user {user_id}')


# ── Alert email ────────────────────────────────────────────────────────────────

def _alert_recipient(org):
    """org.alert_email if set, otherwise the owner's email (None if neither)."""
    from .models import User
    if org.alert_email:
        return org.alert_email
    owner = User.objects.filter(organization=org, role='owner').first()
    return owner.email if owner else None


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_alert(self, alert_id):
    """
    Triggered when a SurveyResponse rating falls at or below the survey's
    alert_threshold. Sends an email to the organization's alert_email address,
    falling back to the owner account email if none is set.

    If the response also triggered the recovery flow, the alert email includes
    a recovery block showing the customer's comment and email so the owner
    can follow up directly.

    Sets alert.status = 'owner_notified' on successful send.
    """
    from .models import Alert

    try:
        alert = Alert.objects.select_related(
            'location__organization',
            'survey_response',
        ).get(id=alert_id)
    except Alert.DoesNotExist:
        logger.warning(f'send_alert: Alert {alert_id} not found')
        return

    org = alert.location.organization
    if not org:
        logger.warning(f'send_alert: Alert {alert_id} has no org — skipping')
        return

    # Respect the org-level alerts toggle
    if not org.alerts_enabled:
        logger.info(f'send_alert: alerts disabled for org {org.id} — skipping')
        return

    recipient = _alert_recipient(org)
    if not recipient:
        logger.warning(f'send_alert: No recipient found for org {org.id}')
        return

    survey_response   = alert.survey_response
    location_name     = alert.location.name
    rating            = alert.rating
    stars             = '★' * rating + '☆' * (5 - rating)
    brand             = get_brand(alert.location.product)
    dashboard_url     = brand.frontend_url + brand.alerts_dashboard_path
    comment           = survey_response.comment or ''

    # Recovery fields
    recovery_triggered  = survey_response.recovery_triggered
    recovery_comment    = survey_response.recovery_comment or ''
    recovery_email_addr = survey_response.recovery_email or ''

    subject = f"Low rating alert — {location_name} ({rating}/5)"

    # ── Plain-text body ────────────────────────────────────────────────────
    text_lines = [
        f"A low rating was submitted at {location_name}.",
        '',
        f"Rating: {rating}/5",
        f"Location: {location_name}",
    ]
    if comment:
        text_lines.append(f"Comment: {comment}")
    if recovery_triggered:
        text_lines += [
            '',
            '── Customer recovery response ──',
        ]
        if recovery_comment:
            text_lines.append(f"What went wrong: {recovery_comment}")
        if recovery_email_addr:
            text_lines.append(f"Customer email: {recovery_email_addr}")
        else:
            text_lines.append("Customer did not provide an email.")
    text_lines += ['', f"View your dashboard: {dashboard_url}"]
    text_body = '\n'.join(text_lines)

    # ── Recovery block HTML (conditionally included) ───────────────────────
    if recovery_triggered:
        recovery_comment_html = (
            f'<p style="margin: 10px 0 0; font-size: 14px; color: #374151;">'
            f'<strong>What went wrong:</strong> {recovery_comment}</p>'
            if recovery_comment else ''
        )
        recovery_email_html = (
            f'<p style="margin: 8px 0 0; font-size: 14px; color: #374151;">'
            f'<strong>Customer email:</strong> '
            f'<a href="mailto:{recovery_email_addr}" style="color: #2563eb;">{recovery_email_addr}</a></p>'
            if recovery_email_addr
            else '<p style="margin: 8px 0 0; font-size: 13px; color: #9ca3af;">Customer did not provide an email.</p>'
        )
        recovery_block_html = f"""
      <div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 12px;
                  padding: 16px 20px; margin-bottom: 24px;">
        <p style="margin: 0 0 6px; font-size: 11px; letter-spacing: 0.08em;
                  text-transform: uppercase; color: #92400e; font-weight: 600;">
          Customer requested follow-up
        </p>
        <p style="margin: 0; font-size: 13px; color: #78350f; line-height: 1.5;">
          This customer used the recovery prompt. Reply to their email with the coupon offer
          to turn this experience around.
        </p>
        {recovery_comment_html}
        {recovery_email_html}
      </div>
        """
    else:
        recovery_block_html = ''

    # ── HTML body ──────────────────────────────────────────────────────────
    comment_html = (
        f'<p style="margin: 12px 0 0; font-size: 14px; color: #555; font-style: italic;">"{comment}"</p>'
        if comment else ''
    )

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">
      <p style="font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
                color: #888; margin: 0 0 24px;">{brand.name} Alert</p>

      <h1 style="font-size: 22px; font-weight: 600; margin: 0 0 8px;">
        Low rating received
      </h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 24px;">
        A customer rated their experience at <strong>{location_name}</strong>.
      </p>

      <div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 12px;
                  padding: 20px 24px; margin-bottom: 24px;">
        <p style="margin: 0 0 4px; font-size: 28px; letter-spacing: 2px; color: #111;">
          {stars}
        </p>
        <p style="margin: 0; font-size: 13px; color: #888;">{rating} out of 5</p>
        {comment_html}
      </div>

      {recovery_block_html}

      <a href="{dashboard_url}"
         style="display: inline-block; background: #111; color: #fff; text-decoration: none;
                font-size: 13px; font-weight: 500; padding: 10px 20px; border-radius: 8px;">
        View dashboard →
      </a>

      <p style="font-size: 12px; color: #bbb; margin: 32px 0 0;">
        You're receiving this because your {brand.name} account has alert notifications enabled.
      </p>
    </div>
    """

    success = _send_email(
        to=recipient,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        product=brand.product,
    )

    if success:
        alert.status = 'owner_notified'
        alert.save(update_fields=['status'])
        logger.info(f'Alert email sent for alert {alert_id} to {recipient}')
    elif success is False:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            logger.error(f'send_alert: max retries exceeded for alert {alert_id}')


# ── Issue alert email ──────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_issue_alerts(self, alert_ids):
    """
    One email for the issues newly reported in a single submission
    (e.g. "Restroom 2: Out of soap, Trash is full"). Repeat reports of an
    issue that already has an open alert never get here — they only bump
    the alert's report_count (see survey_views._record_issue_alerts).

    Sets each alert's status = 'owner_notified' on successful send.
    """
    from .models import Alert

    alerts = list(
        Alert.objects.select_related('location__organization', 'survey_response')
        .filter(id__in=alert_ids, kind='issue')
        .order_by('created_at')
    )
    if not alerts:
        logger.warning(f'send_issue_alerts: none of {alert_ids} found')
        return

    location = alerts[0].location
    org = location.organization
    if not org or not org.alerts_enabled:
        logger.info(f'send_issue_alerts: alerts disabled or no org for location {location.id} — skipping')
        return

    recipient = _alert_recipient(org)
    if not recipient:
        logger.warning(f'send_issue_alerts: No recipient found for org {org.id}')
        return

    brand         = get_brand(location.product)
    dashboard_url = brand.frontend_url + brand.alerts_dashboard_path
    labels        = [a.issue_label for a in alerts]
    comment       = alerts[0].survey_response.comment or ''

    subject = f"Issue reported — {location.name}: {', '.join(labels)}"

    text_lines = [f"A customer reported {'an issue' if len(labels) == 1 else 'issues'} at {location.name}:", '']
    text_lines += [f"  • {label}" for label in labels]
    if comment:
        text_lines += ['', f"Comment: {comment}"]
    text_lines += [
        '',
        "Repeat reports won't email you again until you resolve the alert.",
        f"View your dashboard: {dashboard_url}",
    ]
    text_body = '\n'.join(text_lines)

    # Labels come from the business and the comment from a customer — escape both.
    issues_html = ''.join(
        f'<li style="margin: 0 0 6px; font-size: 16px; color: #111;">{escape(label)}</li>' for label in labels
    )
    comment_html = (
        f'<p style="margin: 16px 0 0; font-size: 14px; color: #555; font-style: italic;">&ldquo;{escape(comment)}&rdquo;</p>'
        if comment else ''
    )
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">
      <p style="font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
                color: #888; margin: 0 0 24px;">{brand.name} Alert</p>

      <h1 style="font-size: 22px; font-weight: 600; margin: 0 0 8px;">
        {'Issue' if len(labels) == 1 else 'Issues'} reported
      </h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 24px;">
        A customer reported {'an issue' if len(labels) == 1 else 'issues'} at <strong>{escape(location.name)}</strong>.
      </p>

      <div style="background: #fff7ed; border: 1px solid #fed7aa; border-radius: 12px;
                  padding: 20px 24px; margin-bottom: 24px;">
        <ul style="margin: 0; padding-left: 18px;">{issues_html}</ul>
        {comment_html}
      </div>

      <a href="{dashboard_url}"
         style="display: inline-block; background: #111; color: #fff; text-decoration: none;
                font-size: 13px; font-weight: 500; padding: 10px 20px; border-radius: 8px;">
        View dashboard →
      </a>

      <p style="font-size: 12px; color: #bbb; margin: 32px 0 0;">
        Repeat reports of the same issue won't email you again until you resolve it.
        You're receiving this because your {brand.name} account has alert notifications enabled.
      </p>
    </div>
    """

    success = _send_email(
        to=recipient,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        product=brand.product,
    )

    if success:
        Alert.objects.filter(id__in=[a.id for a in alerts], status='pending').update(status='owner_notified')
        logger.info(f'Issue alert email sent for {len(alerts)} alert(s) at {location.id} to {recipient}')
    elif success is False:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            logger.error(f'send_issue_alerts: max retries exceeded for {alert_ids}')


# ── Incentive email ────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_incentive_email(self, survey_response_id):
    """
    Triggered when a customer wins an incentive and provides their email.
    """
    from .models import SurveyResponse

    try:
        response = SurveyResponse.objects.select_related(
            'survey__incentive',
            'survey__organization',
            'location',
        ).get(id=survey_response_id)
    except SurveyResponse.DoesNotExist:
        logger.warning(f'send_incentive_email: SurveyResponse {survey_response_id} not found')
        return

    if not response.email:
        return

    incentive = getattr(response.survey, 'incentive', None)
    if not incentive:
        return

    org = response.survey.organization
    org_name = org.name if org else 'TapRate'
    prize_text = incentive.prize_text
    location_name = response.location.name

    subject = f"You won at {org_name}! 🎉"

    text_body = (
        f"Congratulations! You won a prize at {org_name} ({location_name}).\n\n"
        f"Your prize: {prize_text}\n\n"
        f"Show this email to redeem."
    )

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">
      <p style="font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
                color: #888; margin: 0 0 24px;">{org_name}</p>

      <h1 style="font-size: 28px; font-weight: 600; margin: 0 0 8px;">
        You won! 🎉
      </h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 24px;">
        Thanks for your feedback at <strong>{location_name}</strong>.
        You've won a prize — here's what you get:
      </p>

      <div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 12px;
                  padding: 20px 24px; margin-bottom: 24px; text-align: center;">
        <p style="margin: 0; font-size: 20px; font-weight: 600; color: #92400e;">
          {prize_text}
        </p>
      </div>

      <p style="font-size: 14px; color: #555;">
        Show this email to a staff member to redeem your prize. Valid at {location_name}.
      </p>

      <p style="font-size: 12px; color: #bbb; margin: 32px 0 0;">
        Powered by TapRate
      </p>
    </div>
    """

    success = _send_email(
        to=response.email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )

    if success:
        response.incentive_claimed = True
        response.save(update_fields=['incentive_claimed'])
        logger.info(f'Incentive email sent to {response.email} for response {survey_response_id}')
    else:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            logger.error(f'send_incentive_email: max retries exceeded for {survey_response_id}')

@shared_task
def cleanup_expired_tokens():
    """Flush expired entries from the simplejwt token_blacklist table."""
    call_command('flushexpiredtokens')
