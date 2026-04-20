import uuid
import os
from django.utils.text import slugify
from django.utils import timezone
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Organization, Incentive, Survey, SurveySet, SurveyResponse, Location

User = get_user_model()


# ── Incentive ─────────────────────────────────────────────────────────────────

class IncentiveSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incentive
        fields = ['id', 'win_rate', 'prize_text']
        read_only_fields = ['id']


# ── Survey (individual question) ──────────────────────────────────────────────

class SurveySerializer(serializers.ModelSerializer):
    """Dashboard read serializer for a single question."""
    incentive = IncentiveSerializer(read_only=True)

    class Meta:
        model = Survey
        fields = ['id', 'question', 'scale_type', 'position', 'incentive', 'created_at']
        read_only_fields = ['id', 'created_at']


class SurveyWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Survey
        fields = ['question', 'scale_type', 'position']

    def validate_position(self, value):
        if value < 0:
            raise serializers.ValidationError('Position must be 0 or greater.')
        return value


# ── SurveySet ─────────────────────────────────────────────────────────────────

class SurveySetSerializer(serializers.ModelSerializer):
    """Dashboard read serializer — includes nested questions and location count."""
    surveys = SurveySerializer(many=True, read_only=True)
    location_count = serializers.IntegerField(source='locations.count', read_only=True)

    class Meta:
        model = SurveySet
        fields = [
            'id', 'name', 'comments_enabled', 'comments_prompt',
            'alert_threshold', 'surveys', 'location_count', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class SurveySetWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveySet
        fields = ['name', 'comments_enabled', 'comments_prompt', 'alert_threshold']

    def validate_alert_threshold(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError('Alert threshold must be between 1 and 5.')
        return value


# ── Public serializers (PWA) ──────────────────────────────────────────────────

class IncentivePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incentive
        fields = ['prize_text']


class SurveyPublicSerializer(serializers.ModelSerializer):
    """One question as seen by the PWA survey stepper."""
    incentive = IncentivePublicSerializer(read_only=True)

    class Meta:
        model = Survey
        fields = ['id', 'question', 'scale_type', 'position', 'incentive']


class SurveySetPublicSerializer(serializers.ModelSerializer):
    """Full survey set as returned by the public NFC tap endpoint."""
    surveys = SurveyPublicSerializer(many=True, read_only=True)
    brand_color = serializers.CharField(source='organization.brand_color', read_only=True)
    org_name = serializers.CharField(source='organization.name', read_only=True)
    location_name = serializers.SerializerMethodField()

    class Meta:
        model = SurveySet
        fields = [
            'id', 'name', 'comments_enabled', 'comments_prompt',
            'brand_color', 'org_name', 'location_name', 'surveys',
        ]

    def get_location_name(self, obj):
        location = self.context.get('location')
        return location.name if location else None


# ── Response serializer ───────────────────────────────────────────────────────

class SingleResponseSerializer(serializers.Serializer):
    """One rating within a multi-question submission."""
    survey_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5)


class SurveyResponseSubmitSerializer(serializers.Serializer):
    """Top-level submission payload for a full SurveySet tap."""
    responses = SingleResponseSerializer(many=True)
    comment = serializers.CharField(required=False, allow_blank=True, default='')
    email = serializers.EmailField(required=False, allow_blank=True, default='')

    def validate_responses(self, value):
        if not value:
            raise serializers.ValidationError('At least one response is required.')
        return value


# ── Auth / User ───────────────────────────────────────────────────────────────

class OrganizationSerializer(serializers.ModelSerializer):
    trial_days_remaining = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'slug', 'brand_color', 'logo_url',
            'plan', 'subscription_status', 'trial_ends_at',
            'trial_days_remaining',
        ]
        read_only_fields = ['id', 'slug', 'plan', 'subscription_status', 'trial_ends_at']


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    org_name = serializers.CharField(max_length=255)
    invite_code = serializers.CharField(write_only=True)

    def validate_invite_code(self, value):
        import os
        expected = os.environ.get('REGISTRATION_CODE')
        if not expected or value != expected:
            raise serializers.ValidationError("Invalid invite code.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value.lower()

    def create(self, validated_data):
        import os
        from datetime import timedelta

        trial_days = int(os.environ.get('TRIAL_DAYS', 30))

        base_slug = slugify(validated_data['org_name'])
        slug = base_slug or f"org-{uuid.uuid4().hex[:6]}"
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"

        org = Organization.objects.create(
            name=validated_data['org_name'],
            slug=slug,
            trial_ends_at=timezone.now() + timedelta(days=trial_days),
        )
        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            organization=org,
            role='owner',
        )
        return user


class UserSerializer(serializers.ModelSerializer):
    organization = OrganizationSerializer(read_only=True)

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'role', 'organization', 'is_staff']
        read_only_fields = fields


# ── Location ──────────────────────────────────────────────────────────────────

class LocationSerializer(serializers.ModelSerializer):
    nfc_url = serializers.SerializerMethodField()
    survey_set_name = serializers.CharField(source='survey_set.name', read_only=True)
    survey_set = serializers.PrimaryKeyRelatedField(
        queryset=SurveySet.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Location
        fields = ['id', 'name', 'survey_set', 'survey_set_name', 'nfc_url', 'qr_enabled', 'created_at']
        read_only_fields = ['id', 'nfc_url', 'created_at']

    def get_nfc_url(self, obj):
        request = self.context.get('request')
        frontend_base = os.environ.get('FRONTEND_URL', 'https://taprate.app')
        if request:
            frontend_base = request.META.get('HTTP_X_FRONTEND_URL', frontend_base)
        return f"{frontend_base}/s/{obj.id}"

    def validate_survey_set(self, value):
        if value is None:
            return value
        request = self.context.get('request')
        if value.organization != request.user.organization:
            raise serializers.ValidationError('Survey set not found.')
        return value