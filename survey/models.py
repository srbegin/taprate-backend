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
    created_at             = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def is_access_allowed(self):
        """True if the org has an active subscription or a live trial."""
        if self.subscription_status == 'active':
            return True
        if self.trial_ends_at and timezone.now() < self.trial_ends_at:
            return True
        return False

    def trial_days_remaining(self):
        """Returns days left in trial, or 0 if expired/not set."""
        if not self.trial_ends_at:
            return 0
        delta = self.trial_ends_at - timezone.now()
        return max(0, delta.days)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
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


class SurveySet(models.Model):
    """
    A named collection of ordered survey questions assigned to a Location.
    Replaces the single Survey → Location relationship.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.CASCADE, related_name='survey_sets'
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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        org = self.organization.name if self.organization else 'No Org'
        return f"{org} — {self.name}"


class Survey(models.Model):
    """
    A single question within a SurveySet.
    """
    SCALE_CHOICES = [
        ('numbers', 'Numbers (1–5)'),
        ('stars', 'Stars (1–5)'),
        ('emoji', 'Emoji'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.CASCADE, related_name='surveys'
    )
    survey_set = models.ForeignKey(
        SurveySet, null=True, blank=True,
        on_delete=models.CASCADE, related_name='surveys'
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
    survey = models.OneToOneField(Survey, on_delete=models.CASCADE, related_name='incentive')
    active = models.BooleanField(default=True)
    win_rate = models.IntegerField(default=10, help_text='1 in X chance of winning')
    prize_text = models.CharField(max_length=200)
    email_subject = models.CharField(max_length=200, default='You won a prize!')
    email_body = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.survey.question[:40]} — {self.prize_text}"


class Location(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.CASCADE, related_name='locations'
    )
    survey_set = models.ForeignKey(
        SurveySet, null=True, blank=True,
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


class NfcTag(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    organization = models.ForeignKey(
        Organization, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='nfc_tags'
    )
    location = models.ForeignKey(
        Location, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='nfc_tags'
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.location:
            return f"Tag → {self.location.name}"
        return f"Unclaimed tag {self.id}"


class SurveyResponse(models.Model):
    """
    One rating per question per NFC tap.
    Responses from the same tap share a session_id UUID.
    """
    RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_id = models.UUIDField(null=True, blank=True, db_index=True)
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='responses')
    survey_set = models.ForeignKey(
        SurveySet, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='responses'
    )
    survey = models.ForeignKey(
        Survey, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='responses'
    )
    rating = models.IntegerField(choices=RATING_CHOICES)
    comment = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    incentive_won = models.BooleanField(default=False)
    incentive_claimed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    device_hash = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['location', 'created_at']),
            models.Index(fields=['created_at']),
            models.Index(fields=['rating']),
            models.Index(fields=['session_id']),
        ]

    def __str__(self):
        return f"{self.location} — {self.rating}★ at {self.created_at:%Y-%m-%d %H:%M}"


class Alert(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('owner_notified', 'Owner Notified'),
        ('sent', 'Sent'),
        ('resolved', 'Resolved')
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey_response = models.OneToOneField(SurveyResponse, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='alerts')
    rating = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Alert: {self.location} — {self.rating}★ [{self.status}]"