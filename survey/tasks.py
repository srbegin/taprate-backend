"""
Celery tasks for TapRate.

Required environment variables:
    RESEND_API_KEY     — Resend API key
    ALERTS_FROM_EMAIL  — verified sender address (default: alerts@taprate.app)
    FRONTEND_URL       — used to build dashboard deep-link in emails
"""

import os
import logging
from celery import shared_task

logger = logging.getLogger(__name__)


def _send_email(*, to: str, subject: str, html_body: str, text_body: str) -> bool:
    """
    Send a transactional email via Resend.
    Returns True on success, False on failure.
    """
    import resend

    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        logger.error('RESEND_API_KEY not set — skipping email send')
        return False

    resend.api_key = api_key
    from_email = os.environ.get('ALERTS_FROM_EMAIL', 'alerts@taprate.app')

    try:
        response = resend.Emails.send({
            'from': f'TapRate <{from_email}>',
            'to': [to],
            'subject': subject,
            'html': html_body,
            'text': text_body,
        })
        # Resend raises on error; if we get here it succeeded.
        # response is a dict with an 'id' key on success.
        if not response.get('id'):
            logger.error(f'Resend returned unexpected response: {response}')
            return False
        return True
    except Exception as e:
        logger.error(f'Resend exception: {e}')
        return False


# ── Alert email ────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_alert(self, alert_id):
    """
    Triggered when a SurveyResponse rating falls at or below the survey set's
    alert_threshold. Sends an email to the organization's alert_email address,
    falling back to the owner account email if none is set.
    Sets alert.status = 'owner_notified' on successful send.
    """
    from .models import Alert, User

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

    # Recipient: org.alert_email if set, otherwise the owner's email
    recipient = org.alert_email
    if not recipient:
        owner = User.objects.filter(organization=org, role='owner').first()
        if not owner:
            logger.warning(f'send_alert: No recipient found for org {org.id}')
            return
        recipient = owner.email

    location_name = alert.location.name
    rating = alert.rating
    stars = '★' * rating + '☆' * (5 - rating)
    dashboard_url = os.environ.get('FRONTEND_URL', 'https://taprate.app') + '/dashboard/insights'
    comment = alert.survey_response.comment or ''

    subject = f"Low rating alert — {location_name} ({rating}/5)"

    text_body = (
        f"A low rating was submitted at {location_name}.\n\n"
        f"Rating: {rating}/5\n"
        f"Location: {location_name}\n"
        + (f"Comment: {comment}\n" if comment else "")
        + f"\nView your dashboard: {dashboard_url}"
    )

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 32px 24px; color: #111;">
      <p style="font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
                color: #888; margin: 0 0 24px;">TapRate Alert</p>

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
        {f'<p style="margin: 12px 0 0; font-size: 14px; color: #555; font-style: italic;">"{comment}"</p>' if comment else ''}
      </div>

      <a href="{dashboard_url}"
         style="display: inline-block; background: #111; color: #fff; text-decoration: none;
                font-size: 13px; font-weight: 500; padding: 10px 20px; border-radius: 8px;">
        View dashboard →
      </a>

      <p style="font-size: 12px; color: #bbb; margin: 32px 0 0;">
        You're receiving this because your TapRate account has alert notifications enabled.
      </p>
    </div>
    """

    success = _send_email(
        to=recipient,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )

    if success:
        alert.status = 'owner_notified'
        alert.save(update_fields=['status'])
        logger.info(f'Alert email sent for alert {alert_id} to {recipient}')
    else:
        try:
            raise self.retry()
        except self.MaxRetriesExceededError:
            logger.error(f'send_alert: max retries exceeded for alert {alert_id}')


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