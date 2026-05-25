import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class Organization(models.Model):
    SUBSCRIPTION_STATUS_CHOICES = [
        ('trialing',         'Trialing'),
        ('active',           'Active'),
        ('past_due',         'Past Due'),
        ('canceled',         'Canceled'),
        ('unpaid',           'Unpaid'),
    ]

    id                     = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name                   = models.CharField(max_length=200)
    slug                   = models.SlugField(unique=True)
    brand_color            = models.CharField(max_length=7, default='#0c0c0e')
    logo_url               = models.URLField(blank=True)
    plan                   = models.CharField(max_length=20, default='free')
    # ── Billing ──────────────────────────────────────────────────────────────
    stripe_customer_id     = models.CharField(max_length=100, blank=True)
    stripe_subscription_id = models.CharField(max_length=100, blank=True)
    subscription_status    = models.CharField(
                                 max_length=20,
                                 choices=SUBSCRIPTION_STATUS_CHOICES,
                                 blank=True,
                             )
    trial_ends_at          = models.DateTimeField(null=True, blank=True)
    # ── Notifications ─────────────────────────────────────────────────────────
    alert_email            = models.EmailField(
                                 blank=True,
                                 help_text='Alert notification email. Falls back to owner account email if blank.',
                             )
    alerts_enabled         = models.BooleanField(
                                 default=True,
                                 help_text='Send email alerts when a low rating is received.',
                             )
    # ── Survey defaults (prefill new surveys, overridable per survey) ─────────
    default_alert_threshold  = models.IntegerField(
                                   default=2,
                                   help_text='Default alert threshold for new surveys (1–5).',
                               )
    default_review_url       = models.URLField(
                                   blank=True,
                                   help_text='Default review redirect URL for new surveys.',
                               )
    default_comments_enabled = models.BooleanField(default=False)
    default_comments_prompt  = models.CharField(
                                   max_length=200,
                                   default='Any additional feedback?',
                                   blank=True,
                               )
    timezone                 = models.CharField(
                                   max_length=50,
                                   default='UTC',
                                   blank=True,
                                   help_text='IANA timezone string, e.g. America/New_York.',
                               )
    # ── Testing ───────────────────────────────────────────────────────────────
    test_mode              = models.BooleanField(
                                 default=False,
                                 help_text=(
                                     'When enabled, all survey submissions are marked as test '
                                     'responses and excluded from analytics, insights, and alert emails.'
                                 ),
                             )
    created_at             = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def is_access_allowed(self):
        if self.subscription_status == 'active':
            return True
        if self.trial_ends_at and timezone.now() < self.trial_ends_at:
            return True
        return False

    def trial_days_remaining(self):
        if not self.trial_ends_at:
            return 0
        delta = self.trial_ends_at - timezone.now()
        return max(0, delta.days)


# ── NOTE: Organization deletion ───────────────────────────────────────────────
# Deleting an Organization cascades to Location (intended — org owns its
# locations). With SurveyResponse.location = SET_NULL, responses survive as
# org-less orphans rather than being wiped. This is acceptable for data
# integrity, but org deletion should be performed via a management command
# that handles cleanup explicitly rather than from the Django admin or shell.


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        # SET_NULL: users survive org deletion (they just lose their org context).
        Organization, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='members'
    )
    role = models.CharField(
        max_length=20,
        choices=[('owner', 'Owner'), ('member', 'Member')],
        default='owner'
    )

    def __str__(self):
        return self.email


class Survey(models.Model):
    """
    A named collection of ordered questions assigned to a Location.
    Formerly SurveySet.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.CASCADE, related_name='surveys'
    )
    name = models.CharField(max_length=200)
    comments_enabled = models.BooleanField(default=False)
    comments_prompt = models.CharField(
        max_length=200, default='Any additional feedback?', blank=True
    )
    alert_threshold = models.IntegerField(
        default=2,
        help_text='Create an alert when any rating is at or below this value (1–5).',
    )
    review_redirect_enabled = models.BooleanField(default=False)
    review_redirect_url     = models.URLField(blank=True)
    # ── Recovery flow ─────────────────────────────────────────────────────────
    recovery_enabled   = models.BooleanField(
                             default=False,
                             help_text='Show a recovery prompt when a low rating is submitted.',
                         )
    recovery_threshold = models.IntegerField(
                             default=3,
                             help_text='Trigger recovery prompt when rating is at or below this value (1–5).',
                         )
    recovery_message   = models.TextField(
                             default="We're sorry your experience fell short. Tell us what happened and we'll make it right.",
                             blank=True,
                             help_text='Message shown to the customer on the recovery step.',
                         )
    recovery_coupon_text = models.CharField(
                               max_length=200,
                               blank=True,
                               help_text='Coupon or offer shown as the incentive to share an email (e.g. "10% off your next visit").',
                           )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        org = self.organization.name if self.organization else 'No Org'
        return f"{org} — {self.name}"


class Question(models.Model):
    """
    A single question within a Survey.
    Formerly Survey.
    """
    SCALE_CHOICES = [
        ('numbers', 'Numbers (1–5)'),
        ('stars', 'Stars (1–5)'),
        ('emoji', 'Emoji'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.CASCADE, related_name='questions'
    )
    survey = models.ForeignKey(
        # CASCADE: questions are structural parts of a survey, not independent
        # records. Deleting a survey deletes its questions.
        Survey, null=True, blank=True,
        on_delete=models.CASCADE, related_name='questions'
    )
    position = models.IntegerField(default=0)
    question = models.CharField(max_length=500, default='How was your experience?')
    scale_type = models.CharField(max_length=20, choices=SCALE_CHOICES, default='numbers')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']

    def __str__(self):
        org = self.organization.name if self.organization else 'No Org'
        return f"{org} — {self.question[:60]}"


class Incentive(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='incentives'
    )
    survey = models.ForeignKey(
        # SET_NULL: detach from survey rather than delete.
        Survey, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='incentives'
    )
    name       = models.CharField(max_length=200)
    active     = models.BooleanField(default=True)
    win_rate   = models.IntegerField(default=10, help_text='Percentage chance of winning (1–100)')
    prize_text = models.CharField(max_length=200)
    email_subject = models.CharField(max_length=200, default='You won a prize!')
    email_body    = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.organization.name} — {self.name}"


class IncentiveWin(models.Model):
    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incentive        = models.ForeignKey(
                           # SET_NULL: win records are permanent audit entries.
                           Incentive, null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='wins'
                       )
    survey_response  = models.ForeignKey(
                           'SurveyResponse', on_delete=models.CASCADE, related_name='wins'
                       )
    redeemed_by      = models.ForeignKey(
                           'User', null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='redemptions'
                       )
    code             = models.CharField(max_length=8, unique=True, db_index=True)
    email            = models.EmailField(blank=True)
    marketing_opt_in = models.BooleanField(default=False)
    redeemed_at      = models.DateTimeField(null=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} — {self.incentive.name if self.incentive else 'deleted incentive'}"


class NfcTag(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
                       # SET_NULL: tags are physical hardware; survive org deletion.
                       Organization, null=True, blank=True,
                       on_delete=models.SET_NULL, related_name='nfc_tags'
                   )
    location     = models.OneToOneField(
                       # SET_NULL: tag survives location deletion, becomes unclaimed.
                       'Location', null=True, blank=True,
                       on_delete=models.SET_NULL, related_name='nfc_tag'
                   )
    claimed_at   = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.id)


class Location(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        # CASCADE: locations are owned by the org.
        Organization, on_delete=models.CASCADE, related_name='locations'
    )
    survey = models.ForeignKey(
        # SET_NULL: location survives survey deletion, just loses its survey.
        Survey, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='locations'
    )
    name = models.CharField(max_length=200)
    floor = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    qr_enabled = models.BooleanField(default=False)
    nfc_url = models.CharField(max_length=500, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['active'])]

    def save(self, *args, **kwargs):
        if not self.nfc_url:
            import os
            base = os.environ.get('FRONTEND_URL', 'https://taprate.app')
            self.nfc_url = f"{base}/s/{self.id}"
        super().save(*args, **kwargs)

    def __str__(self):
        org = self.organization.name if self.organization else 'No Org'
        return f"{org} — {self.name}"

    def average_rating(self, days=7):
        since = timezone.now() - timezone.timedelta(days=days)
        return self.responses.filter(created_at__gte=since).aggregate(
            avg=models.Avg('rating'),
            count=models.Count('id')
        )


class SurveyResponse(models.Model):
    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_id       = models.UUIDField(null=True, blank=True, db_index=True)
    location         = models.ForeignKey(
                           # SET_NULL: responses are permanent audit records and must survive
                           # location deletion.
                           Location, null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='responses'
                       )
    survey           = models.ForeignKey(
                           Survey, null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='responses'
                       )
    question         = models.ForeignKey(
                           Question, null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='responses'
                       )
    rating           = models.IntegerField(choices=[(i, str(i)) for i in range(1, 6)])
    comment          = models.TextField(blank=True)
    email            = models.EmailField(blank=True)
    marketing_opt_in = models.BooleanField(default=False)
    incentive_won    = models.BooleanField(default=False)
    incentive_claimed = models.BooleanField(default=False)
    # ── Recovery flow ─────────────────────────────────────────────────────────
    recovery_triggered = models.BooleanField(default=False)
    recovery_comment   = models.TextField(blank=True)
    recovery_email     = models.EmailField(blank=True)
    # ── Testing ───────────────────────────────────────────────────────────────
    is_test          = models.BooleanField(
                           default=False,
                           db_index=True,
                           help_text=(
                               'True when submitted via dashboard preview or while org '
                               'test_mode is enabled. Excluded from analytics and alert emails.'
                           ),
                       )
    # ── Meta ──────────────────────────────────────────────────────────────────
    ip_hash          = models.CharField(max_length=64, blank=True)
    device_hash      = models.CharField(max_length=64, blank=True)
    user_agent       = models.CharField(max_length=500, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        marker = ' [TEST]' if self.is_test else ''
        return f"Response {self.id} — {self.rating}★{marker}"


class Alert(models.Model):
    STATUS_CHOICES = [
        ('pending',        'Pending'),
        ('owner_notified', 'Owner Notified'),
        ('sent',           'Sent'),
        ('resolved',       'Resolved'),
    ]

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey_response = models.ForeignKey(
                          # CASCADE: alerts are operational items. If the response is gone,
                          # the alert has no context.
                          SurveyResponse, on_delete=models.CASCADE, related_name='alerts'
                      )
    location        = models.ForeignKey(
                          # CASCADE: an alert without a location is unactionable.
                          Location, on_delete=models.CASCADE, related_name='alerts'
                      )
    rating          = models.IntegerField()
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at      = models.DateTimeField(auto_now_add=True)
    resolved_at     = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Alert {self.id} — {self.rating}★"