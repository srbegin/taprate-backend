import uuid
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils.text import slugify
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Organization, Incentive, IncentiveWin, IssueOption, Survey, Question, SurveyResponse, Location
from .products import DEFAULT_PRODUCT, get_brand, get_product as request_product

User = get_user_model()


# ── Incentive ─────────────────────────────────────────────────────────────────

class IncentiveSerializer(serializers.ModelSerializer):
    """Full CRUD serializer for /dashboard/incentives."""
    survey_name = serializers.CharField(source='survey.name', read_only=True)

    class Meta:
        model = Incentive
        fields = [
            'id', 'name', 'active', 'win_rate', 'prize_text',
            'email_subject', 'email_body',
            'survey', 'survey_name',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'survey_name']

    def validate_win_rate(self, value):
        if not (1 <= value <= 100):
            raise serializers.ValidationError('Win rate must be between 1 and 100.')
        return value

    def validate_survey(self, value):
        if value is None:
            return value
        request = self.context.get('request')
        if value.organization != request.user.organization or value.product != request_product(request):
            raise serializers.ValidationError('Survey not found.')
        return value


class IncentivePublicSerializer(serializers.ModelSerializer):
    """Minimal read for the survey PWA — never expose win_rate."""
    class Meta:
        model = Incentive
        fields = ['prize_text']


class IncentiveWinSerializer(serializers.ModelSerializer):
    incentive_name = serializers.CharField(source='incentive.name', read_only=True)
    prize_text     = serializers.CharField(source='incentive.prize_text', read_only=True)
    location_name  = serializers.CharField(
        source='survey_response.location.name', read_only=True
    )

    class Meta:
        model = IncentiveWin
        fields = [
            'id', 'code', 'incentive_name', 'prize_text',
            'email', 'marketing_opt_in',
            'redeemed_at', 'redeemed_by',
            'location_name', 'created_at',
        ]
        read_only_fields = fields


class RedeemSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=8, min_length=8)

    def validate_code(self, value):
        return value.upper().strip()


# ── Question (individual question within a Survey) ────────────────────────────

MAX_ISSUE_OPTIONS = 20


class IssueOptionSerializer(serializers.ModelSerializer):
    """An issue option on an 'issues' question. Send `id` to update an existing one."""
    id = serializers.UUIDField(required=False)

    class Meta:
        model = IssueOption
        fields = ['id', 'label', 'alerts', 'position']
        extra_kwargs = {'position': {'required': False}, 'alerts': {'required': False}}

    def validate_label(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Issue label is required.')
        return value


class QuestionSerializer(serializers.ModelSerializer):
    """Dashboard read serializer for a single question."""
    options = IssueOptionSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = ['id', 'question', 'question_type', 'scale_type', 'active', 'position', 'options', 'created_at']
        read_only_fields = ['id', 'created_at']


class QuestionWriteSerializer(serializers.ModelSerializer):
    """
    Create/update a question. For 'issues' questions, `options` is the full
    list: existing ids are updated, new entries created, missing ones deleted.
    """
    options = IssueOptionSerializer(many=True, required=False)

    class Meta:
        model = Question
        fields = ['question', 'question_type', 'scale_type', 'active', 'position', 'options']

    def validate_position(self, value):
        if value < 0:
            raise serializers.ValidationError('Position must be 0 or greater.')
        return value

    def validate_options(self, value):
        if len(value) > MAX_ISSUE_OPTIONS:
            raise serializers.ValidationError(f'At most {MAX_ISSUE_OPTIONS} issues per question.')
        labels = [o['label'].lower() for o in value]
        if len(labels) != len(set(labels)):
            raise serializers.ValidationError('Issue labels must be unique.')
        return value

    def validate(self, attrs):
        question_type = attrs.get('question_type') or getattr(self.instance, 'question_type', 'rating')
        if question_type == 'issues':
            options = attrs.get('options')
            has_existing = self.instance is not None and self.instance.options.exists()
            if (options is not None and not options) or (options is None and not has_existing):
                raise serializers.ValidationError({'options': 'Add at least one issue.'})
        return attrs

    def create(self, validated_data):
        options = validated_data.pop('options', [])
        question = super().create(validated_data)
        self._save_options(question, options)
        return question

    def update(self, instance, validated_data):
        options = validated_data.pop('options', None)
        question = super().update(instance, validated_data)
        if options is not None:
            self._save_options(question, options)
        return question

    @staticmethod
    def _save_options(question, options):
        existing = {str(o.id): o for o in question.options.all()}
        keep = set()
        for i, data in enumerate(options):
            fields = {
                'label':    data['label'],
                'alerts':   data.get('alerts', True),
                'position': data.get('position', i),
            }
            option = existing.get(str(data['id'])) if data.get('id') else None
            if option:
                for key, value in fields.items():
                    setattr(option, key, value)
                option.save()
            else:
                option = IssueOption.objects.create(question=question, **fields)
            keep.add(str(option.id))
        for option_id, option in existing.items():
            if option_id not in keep:
                option.delete()


# ── Survey (collection of questions) ─────────────────────────────────────────

class SurveySerializer(serializers.ModelSerializer):
    """Dashboard read serializer — includes nested questions, location count, active incentive."""
    questions        = QuestionSerializer(many=True, read_only=True)
    location_count   = serializers.IntegerField(source='locations.count', read_only=True)
    active_incentive = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            'id', 'name', 'comments_enabled', 'comments_prompt',
            'alert_threshold',
            'review_redirect_url', 'review_redirect_enabled',
            # recovery
            'recovery_enabled', 'recovery_threshold',
            'recovery_message', 'recovery_coupon_text',
            'questions', 'location_count', 'active_incentive',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def get_active_incentive(self, obj):
        inc = obj.incentives.filter(active=True).first()
        if not inc:
            return None
        return {'id': str(inc.id), 'name': inc.name, 'prize_text': inc.prize_text}


class SurveyWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Survey
        fields = [
            'name', 'comments_enabled', 'comments_prompt', 'alert_threshold',
            'review_redirect_url', 'review_redirect_enabled',
            # recovery
            'recovery_enabled', 'recovery_threshold',
            'recovery_message', 'recovery_coupon_text',
        ]

    def validate_alert_threshold(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError('Alert threshold must be between 1 and 5.')
        return value

    def validate_recovery_threshold(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError('Recovery threshold must be between 1 and 5.')
        return value


# ── Public serializers (PWA) ──────────────────────────────────────────────────

class IssueOptionPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssueOption
        fields = ['id', 'label']


class QuestionPublicSerializer(serializers.ModelSerializer):
    """One question as seen by the PWA survey stepper."""
    options = IssueOptionPublicSerializer(many=True, read_only=True)

    class Meta:
        model = Question
        fields = ['id', 'question', 'question_type', 'scale_type', 'position', 'options']


class SurveyPublicSerializer(serializers.ModelSerializer):
    """Full survey as returned by the public NFC tap endpoint."""
    questions     = serializers.SerializerMethodField()   # active questions only
    brand_color   = serializers.CharField(source='organization.brand_color', read_only=True)
    org_name      = serializers.CharField(source='organization.name', read_only=True)
    logo_url      = serializers.CharField(source='organization.logo_url', read_only=True)
    location_name = serializers.SerializerMethodField()
    incentive     = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            'id', 'name', 'comments_enabled', 'comments_prompt',
            'brand_color', 'org_name', 'logo_url', 'location_name',
            'review_redirect_url', 'review_redirect_enabled',
            'incentive',
            # recovery
            'recovery_enabled', 'recovery_threshold',
            'recovery_message', 'recovery_coupon_text',
            'questions',
        ]

    def get_questions(self, obj):
        questions = obj.questions.filter(active=True).prefetch_related('options')
        return QuestionPublicSerializer(questions, many=True).data

    def get_location_name(self, obj):
        location = self.context.get('location')
        return location.name if location else None

    def get_incentive(self, obj):
        inc = obj.incentives.filter(active=True).first()
        if not inc:
            return None
        return IncentivePublicSerializer(inc).data


# ── Response serializer ───────────────────────────────────────────────────────

class SingleResponseSerializer(serializers.Serializer):
    """
    One answer within a multi-question submission: `rating` for rating
    questions, `issue_ids` for issues questions (empty list = "all good").
    SurveyResponseView checks which one applies to each question.
    """
    question_id = serializers.UUIDField()
    rating      = serializers.IntegerField(min_value=1, max_value=5, required=False, allow_null=True)
    issue_ids   = serializers.ListField(
                      child=serializers.UUIDField(), required=False, max_length=MAX_ISSUE_OPTIONS,
                  )


class SurveyResponseSubmitSerializer(serializers.Serializer):
    """Top-level submission payload for a full Survey tap."""
    responses        = SingleResponseSerializer(many=True)
    comment          = serializers.CharField(required=False, allow_blank=True, default='')
    email            = serializers.EmailField(required=False, allow_blank=True, default='')
    marketing_opt_in = serializers.BooleanField(required=False, default=False)
    # Recovery flow fields — only populated when the recovery step is shown
    recovery_comment = serializers.CharField(required=False, allow_blank=True, default='')
    recovery_email   = serializers.EmailField(required=False, allow_blank=True, default='')

    def validate_responses(self, value):
        if not value:
            raise serializers.ValidationError('At least one response is required.')
        return value


# ── Auth / User ───────────────────────────────────────────────────────────────

class OrganizationSerializer(serializers.ModelSerializer):
    """
    Billing fields describe the product this request is for (X-Product header,
    default TapRate). They keep their pre-split keys so existing frontends work
    unchanged; `subscriptions` lists every product the org has.
    """
    product              = serializers.SerializerMethodField()
    plan                 = serializers.SerializerMethodField()
    subscription_status  = serializers.SerializerMethodField()
    trial_ends_at        = serializers.SerializerMethodField()
    trial_days_remaining = serializers.SerializerMethodField()
    location_count       = serializers.SerializerMethodField()
    overage_count        = serializers.SerializerMethodField()
    subscriptions        = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = [
            # identity
            'id', 'name', 'slug',
            # branding
            'brand_color', 'logo_url',
            # billing (read-only, for the request's product)
            'product',
            'plan', 'subscription_status', 'trial_ends_at', 'trial_days_remaining',
            'location_count',
            'overage_count',
            'subscriptions',
            # notifications
            'alert_email', 'alerts_enabled',
            # survey defaults
            'default_alert_threshold', 'default_review_url',
            'default_comments_enabled', 'default_comments_prompt',
            'timezone',
            'test_mode',
        ]
        read_only_fields = ['id', 'slug']

    def _product(self):
        request = self.context.get('request')
        return request_product(request) if request else DEFAULT_PRODUCT

    def _subscription(self, obj):
        return obj.get_subscription(self._product())

    def _usage(self, obj):
        from .views.billing_views import location_usage
        if not hasattr(self, '_usage_cache'):
            self._usage_cache = {}
        key = (obj.pk, self._product())
        if key not in self._usage_cache:
            self._usage_cache[key] = location_usage(obj, self._product())
        return self._usage_cache[key]

    def get_product(self, obj):
        return self._product()

    def get_plan(self, obj):
        sub = self._subscription(obj)
        # Legacy display value: trials report 'free' as before the split.
        # Limit logic never reads this — it uses Subscription.plan.
        return (sub.plan if sub else '') or 'free'

    def get_subscription_status(self, obj):
        sub = self._subscription(obj)
        return sub.status if sub else ''

    def get_trial_ends_at(self, obj):
        sub = self._subscription(obj)
        if not sub or not sub.trial_ends_at:
            return None
        return serializers.DateTimeField().to_representation(sub.trial_ends_at)

    def get_trial_days_remaining(self, obj):
        sub = self._subscription(obj)
        return sub.trial_days_remaining if sub else 0

    def get_location_count(self, obj):
        return self._usage(obj)[0]

    def get_overage_count(self, obj):
        return self._usage(obj)[2]

    def get_subscriptions(self, obj):
        return [
            {
                'product':              sub.product,
                'plan':                 sub.plan,
                'status':               sub.status,
                'trial_ends_at':        serializers.DateTimeField().to_representation(sub.trial_ends_at)
                                        if sub.trial_ends_at else None,
                'trial_days_remaining': sub.trial_days_remaining,
            }
            for sub in obj.subscriptions.order_by('product')
        ]

    def validate_default_alert_threshold(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError('Must be between 1 and 5.')
        return value

    def validate_logo_url(self, value):
        if not value:
            return value  # blank/null allowed
        if len(value) > 500:
            raise serializers.ValidationError('Logo URL must be 500 characters or fewer.')
        if not value.startswith('https://'):
            raise serializers.ValidationError('Logo URL must use HTTPS.')
        try:
            URLValidator(schemes=['https'])(value)
        except DjangoValidationError:
            raise serializers.ValidationError('Logo URL is not a valid URL.')
        return value


class RegisterSerializer(serializers.Serializer):
    email       = serializers.EmailField()
    password    = serializers.CharField(write_only=True, min_length=8)
    first_name  = serializers.CharField(max_length=150)
    last_name   = serializers.CharField(max_length=150)
    org_name    = serializers.CharField(max_length=255)
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
        from .views.billing_views import start_trial
        product = validated_data.pop('product', DEFAULT_PRODUCT)

        base_slug = slugify(validated_data['org_name'])
        slug = base_slug or f"org-{uuid.uuid4().hex[:6]}"
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"

        org = Organization.objects.create(
            name=validated_data['org_name'],
            slug=slug,
        )
        start_trial(org, product)
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
    nfc_url     = serializers.SerializerMethodField()
    survey_name = serializers.CharField(source='survey.name', read_only=True)
    survey      = serializers.PrimaryKeyRelatedField(
        queryset=Survey.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Location
        fields = ['id', 'name', 'survey', 'survey_name', 'nfc_url', 'qr_enabled', 'created_at']
        read_only_fields = ['id', 'nfc_url', 'created_at']

    def get_nfc_url(self, obj):
        # Shareable public link for this location (key kept for frontend compat).
        # Opens the QR entry route, which mints a survey session.
        return f"{get_brand(obj.product).frontend_url}/qr/{obj.id}"

    def validate_survey(self, value):
        if value is None:
            return value
        request = self.context.get('request')
        if value.organization != request.user.organization or value.product != request_product(request):
            raise serializers.ValidationError('Survey not found.')
        return value