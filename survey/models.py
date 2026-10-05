import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone

from .products import PRODUCT_CHOICES, TAPRATE


SUBSCRIPTION_STATUS_CHOICES = [
    ('trialing',         'Trialing'),
    ('active',           'Active'),
    ('past_due',         'Past Due'),
    ('canceled',         'Canceled'),
    ('unpaid',           'Unpaid'),
]


def _product_field(**kwargs):
    """Which product (Cleanpulse / TapRate) a record belongs to."""
    kwargs.setdefault('default', TAPRATE)
    return models.CharField(max_length=20, choices=PRODUCT_CHOICES, db_index=True, **kwargs)


class Organization(models.Model):
    id                     = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name                   = models.CharField(max_length=200)
    slug                   = models.SlugField(unique=True)
    brand_color            = models.CharField(max_length=7, default='#0c0c0e')
    logo_url               = models.URLField(blank=True)
    # ── Billing ──────────────────────────────────────────────────────────────
    # One Stripe customer per org; per-product plan/status live on Subscription.
    stripe_customer_id     = models.CharField(max_length=100, blank=True)
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
    is_test = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    def get_subscription(self, product):
        return self.subscriptions.filter(product=product).first()

    def has_access(self, product):
        sub = self.get_subscription(product)
        return bool(sub and sub.is_access_allowed())


class Subscription(models.Model):
    """
    An organization's subscription to one product. Cleanpulse and TapRate are
    bought separately, so an org has at most one row per product. All billing
    state lives here; the Stripe customer stays on Organization (one customer,
    up to two Stripe subscriptions).
    """
    id                     = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization           = models.ForeignKey(
                                 Organization, on_delete=models.CASCADE, related_name='subscriptions'
                             )
    product                = models.CharField(max_length=20, choices=PRODUCT_CHOICES)
    # '' while trialing. Never 'free' — plan names must match billing PRICE_IDS.
    plan                   = models.CharField(max_length=20, blank=True)
    status                 = models.CharField(
                                 max_length=20, choices=SUBSCRIPTION_STATUS_CHOICES, blank=True
                             )
    stripe_subscription_id = models.CharField(max_length=100, blank=True, db_index=True)
    trial_ends_at          = models.DateTimeField(null=True, blank=True)
    created_at             = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'product'], name='unique_subscription_per_product'
            ),
        ]

    def __str__(self):
        return f"{self.organization.name} — {self.product} ({self.status or 'none'})"

    def is_access_allowed(self):
        if self.status == 'active':
            return True
        if self.trial_ends_at and timezone.now() < self.trial_ends_at:
            return True
        return False

    @property
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
    product = _product_field()
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
    TYPE_CHOICES = [
        ('rating', 'Rating (1–5)'),
        ('issues', 'Issues (pick any that apply)'),
    ]
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
    question_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='rating')
    # Rating questions only.
    scale_type = models.CharField(max_length=20, choices=SCALE_CHOICES, default='numbers')
    # Inactive questions are kept (with their options) but hidden from the
    # survey and rejected on submit — e.g. a business switching its issues step off.
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']

    def __str__(self):
        org = self.organization.name if self.organization else 'No Org'
        return f"{org} — {self.question[:60]}"


class IssueOption(models.Model):
    """
    One pickable issue on an 'issues' question (e.g. "Out of soap").
    Businesses add these from presets or type their own.
    """
    id       = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(
                   # CASCADE: options are part of the question. Reports keep a
                   # label snapshot (ResponseIssue / Alert), so history survives.
                   Question, on_delete=models.CASCADE, related_name='options'
               )
    label    = models.CharField(max_length=80)
    position = models.IntegerField(default=0)
    alerts   = models.BooleanField(
                   default=True,
                   help_text='Email the business when a customer reports this issue.',
               )

    class Meta:
        ordering = ['position']

    def __str__(self):
        return self.label


class Incentive(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='incentives'
    )
    product = _product_field()
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
    # '' = unallocated stock. Set when allocated/claimed; must match its location's product.
    product      = _product_field(default='', blank=True)
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
    product = _product_field()
    name = models.CharField(max_length=200)
    floor = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    qr_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['active'])]

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
    # Null for answers to 'issues' questions — see ResponseIssue.
    rating           = models.IntegerField(choices=[(i, str(i)) for i in range(1, 6)], null=True, blank=True)
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
        answer = f"{self.rating}★" if self.rating is not None else 'issues'
        return f"Response {self.id} — {answer}{marker}"


class ResponseIssue(models.Model):
    """One issue a customer selected in an answer to an 'issues' question."""
    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey_response = models.ForeignKey(
                          SurveyResponse, on_delete=models.CASCADE, related_name='issues'
                      )
    option          = models.ForeignKey(
                          # SET_NULL + label snapshot: reports outlive renamed/deleted options.
                          IssueOption, null=True, blank=True,
                          on_delete=models.SET_NULL, related_name='reports'
                      )
    label           = models.CharField(max_length=80)

    def __str__(self):
        return self.label


class Alert(models.Model):
    STATUS_CHOICES = [
        ('pending',        'Pending'),
        ('owner_notified', 'Owner Notified'),
        ('sent',           'Sent'),
        ('resolved',       'Resolved'),
    ]
    KIND_CHOICES = [
        ('low_rating', 'Low rating'),
        ('issue',      'Issue reported'),
    ]
    OPEN_STATUSES = ('pending', 'owner_notified')

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
    kind            = models.CharField(max_length=20, choices=KIND_CHOICES, default='low_rating')
    rating          = models.IntegerField(null=True, blank=True)   # low_rating alerts only
    # ── Issue alerts ──────────────────────────────────────────────────────────
    # At most one open (pending/owner_notified) alert per location + issue:
    # repeat reports bump report_count instead of re-alerting. Resolving re-arms.
    issue_option     = models.ForeignKey(
                           IssueOption, null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='issue_alerts'
                       )
    issue_label      = models.CharField(max_length=80, blank=True)
    report_count     = models.IntegerField(default=1)
    last_reported_at = models.DateTimeField(null=True, blank=True)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at      = models.DateTimeField(auto_now_add=True)
    resolved_at     = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        if self.kind == 'issue':
            return f"Alert {self.id} — {self.issue_label} ×{self.report_count}"
        return f"Alert {self.id} — {self.rating}★"

class ContactSubmission(models.Model):
    STATUS_CHOICES = [
        ('new',       'New'),
        ('contacted', 'Contacted'),
        ('converted', 'Converted'),
        ('declined',  'Declined'),
        ('spam',      'Spam'),
    ]
 
    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product        = _product_field()   # which brand's site the lead came from
    name           = models.CharField(max_length=120)
    business_name  = models.CharField(max_length=200)
    email          = models.EmailField()
    phone          = models.CharField(max_length=40, blank=True)
    location_count = models.CharField(max_length=10)   # '1' | '2-5' | '6-20' | '20+'
    message        = models.TextField(blank=True)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    notes          = models.TextField(blank=True)       # internal admin notes
    submitted_at   = models.DateTimeField(auto_now_add=True)
    contacted_at   = models.DateTimeField(null=True, blank=True)
    ip_address     = models.GenericIPAddressField(null=True, blank=True)
 
    class Meta:
        ordering = ['-submitted_at']
 
    def __str__(self):
        return f'{self.business_name} — {self.email} [{self.status}]'
 