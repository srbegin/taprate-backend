import uuid
import hashlib
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    brand_color = models.CharField(max_length=7, default='#0c0c0e')
    logo_url = models.URLField(blank=True)
    plan = models.CharField(max_length=20, default='free')
    alert_email = models.EmailField(
        blank=True,
        help_text='Alert notification email. Falls back to owner account email.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


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


class Survey(models.Model):
    SCALE_CHOICES = [
        ('numbers', 'Numbers (1–5)'),
        ('stars', 'Stars (1–5)'),
        ('emoji', 'Emoji'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.CASCADE, related_name='surveys')
    name = models.CharField(max_length=200)
    question = models.CharField(max_length=500, default='How was your experience?')
    scale_type = models.CharField(max_length=20, choices=SCALE_CHOICES, default='numbers')
    comments_enabled = models.BooleanField(default=False)
    comments_prompt = models.CharField(max_length=200, default='Any additional feedback?', blank=True)
    active = models.BooleanField(default=True)
    alert_threshold = models.IntegerField(
        default=2,
        help_text='Create an alert when a rating is at or below this value (1–5).',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        org = self.organization.name if self.organization else 'No Org'
        return f"{org} — {self.name}"


class Incentive(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.OneToOneField(Survey, on_delete=models.CASCADE, related_name='incentive')
    active = models.BooleanField(default=True)
    win_rate = models.IntegerField(default=10, help_text='1 in X chance of winning')
    prize_text = models.CharField(max_length=200)
    email_subject = models.CharField(max_length=200, default='You won a prize!')
    email_body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.survey.name} — {self.prize_text}"


class Location(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.CASCADE, related_name='locations')
    survey = models.ForeignKey(Survey, null=True, blank=True, on_delete=models.SET_NULL, related_name='locations')
    name = models.CharField(max_length=200)
    floor = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
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
    id = models.UUIDField(primary_key=True, editable=False)  # set externally, not auto
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
    RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='responses')
    survey = models.ForeignKey(Survey, null=True, blank=True, on_delete=models.SET_NULL, related_name='responses')
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
            models.Index(fields=['ip_hash', 'created_at']),
        ]

    def __str__(self):
        return f"{self.location} — {self.rating}★ at {self.created_at:%Y-%m-%d %H:%M}"


class Alert(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('sent', 'Sent'), ('resolved', 'Resolved')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey_response = models.OneToOneField(SurveyResponse, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='alerts')
    rating = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Alert: {self.location} — {self.rating}★ [{self.status}]"