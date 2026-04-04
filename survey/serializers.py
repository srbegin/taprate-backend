import uuid
import os
from django.utils.text import slugify
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Organization, Incentive, Survey, SurveyResponse, Location

User = get_user_model()


class IncentiveSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incentive
        fields = ['id', 'win_rate', 'prize_text']
        read_only_fields = ['id']


class SurveySerializer(serializers.ModelSerializer):
    incentive = IncentiveSerializer(read_only=True)
    location_count = serializers.IntegerField(
        source='locations.count', read_only=True
    )

    class Meta:
        model = Survey
        fields = [
            'id', 'question', 'scale_type', 'comments_enabled', 'comments_prompt',
            'alert_threshold', 'incentive', 'location_count', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class SurveyWriteSerializer(serializers.ModelSerializer):
    """Separate write serializer — excludes computed read-only fields."""

    class Meta:
        model = Survey
        fields = ['question', 'scale_type', 'comments_enabled', 'comments_prompt', 'alert_threshold']

    def validate_alert_threshold(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError('Alert threshold must be between 1 and 5.')
        return value


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'brand_color', 'logo_url', 'plan', 'alert_email']
        read_only_fields = ['id', 'slug', 'plan']


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    org_name = serializers.CharField(max_length=255)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value.lower()

    def create(self, validated_data):
        base_slug = slugify(validated_data['org_name'])
        slug = base_slug or f"org-{uuid.uuid4().hex[:6]}"
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"

        org = Organization.objects.create(
            name=validated_data['org_name'],
            slug=slug,
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
        fields = ['id', 'email', 'first_name', 'last_name', 'role', 'organization']
        read_only_fields = fields


class IncentivePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incentive
        fields = ['prize_text']


class SurveyPublicSerializer(serializers.ModelSerializer):
    incentive = IncentivePublicSerializer(read_only=True)
    brand_color = serializers.CharField(source='organization.brand_color', read_only=True)
    org_name = serializers.CharField(source='organization.name', read_only=True)
    location_name = serializers.SerializerMethodField()
    location_floor = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            'id', 'question', 'scale_type', 'comments_enabled', 'comments_prompt',
            'incentive', 'brand_color', 'org_name', 'location_name', 'location_floor',
        ]

    def get_location_name(self, obj):
        location = self.context.get('location')
        return location.name if location else None

    def get_location_floor(self, obj):
        location = self.context.get('location')
        return location.floor if location else None


class SurveyResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveyResponse
        fields = ['rating', 'comment', 'email']
        extra_kwargs = {
            'comment': {'required': False, 'allow_blank': True},
            'email': {'required': False, 'allow_blank': True},
        }

    def validate_rating(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError('Rating must be between 1 and 5.')
        return value


class LocationSerializer(serializers.ModelSerializer):
    nfc_url = serializers.SerializerMethodField()
    survey_name = serializers.CharField(source='survey.question', read_only=True)
    survey = serializers.PrimaryKeyRelatedField(
        queryset=Survey.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Location
        fields = ['id', 'name', 'survey', 'survey_name', 'nfc_url', 'created_at']
        read_only_fields = ['id', 'nfc_url', 'created_at']

    def get_nfc_url(self, obj):
        request = self.context.get('request')
        frontend_base = os.environ.get('FRONTEND_URL', 'https://taprate.app')
        if request:
            frontend_base = request.META.get('HTTP_X_FRONTEND_URL', frontend_base)
        return f"{frontend_base}/s/{obj.id}"

    def validate_survey(self, value):
        if value is None:
            return value
        request = self.context.get('request')
        if value.organization != request.user.organization:
            raise serializers.ValidationError('Survey not found.')
        return value