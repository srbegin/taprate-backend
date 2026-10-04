"""
Management command: seed_demo

Creates a realistic demo organization with locations, surveys, questions, and
30 days of response data so the insights dashboard has something to display.

The "Main Counter" location gets a stable deterministic UUID so it can be wired
to the DEMO_LOCATION_ID env var for the public /demo page.

Usage:
    python manage.py seed_demo
    python manage.py seed_demo --email you@example.com   # attach to your account
    python manage.py seed_demo --flush                   # wipe and re-seed
"""

import uuid
import random
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone

from survey.models import (
    Organization, User,
    Survey, Question,
    Location, SurveyResponse, Alert,
)

DEMO_ORG_SLUG = 'demo-coffee-co'

# Stable deterministic UUID for the primary demo location.
# Derived via uuid5 from a fixed name — always the same, always a valid UUID.
# Wire this value to DEMO_LOCATION_ID on Fly.io.
DEMO_MAIN_LOCATION_ID = uuid.uuid5(uuid.NAMESPACE_DNS, 'demo-main-counter.taprate.app')

LOCATION_NAMES = ['Main Counter', 'Drive-Through', 'Patio', 'Restrooms']

# Survey shown to prospects via /demo — covers all three scale types.
# Kept separate from analytics surveys so the demo experience is always clean.
DEMO_SURVEY_DEF = {
    'name': 'Quick Feedback',
    'comments_enabled': True,
    'comments_prompt': 'Anything else to share?',
    'alert_threshold': 2,
    'questions': [
        {'question': 'How was your visit today?',     'scale_type': 'stars',   'position': 0},
        {'question': 'How friendly was our team?',    'scale_type': 'emoji',   'position': 1},
        {'question': 'How would you rate the value?', 'scale_type': 'numbers', 'position': 2},
    ],
}

# Analytics surveys — used to generate 30 days of realistic response data.
ANALYTICS_SURVEY_DEFS = [
    {
        'name': 'General Experience',
        'comments_enabled': True,
        'comments_prompt': 'Tell us more…',
        'alert_threshold': 2,
        'questions': [
            {'question': 'How was your visit today?',  'scale_type': 'stars', 'position': 0},
            {'question': 'How friendly was our team?', 'scale_type': 'emoji', 'position': 1},
        ],
    },
    {
        'name': 'Quick Check-in',
        'comments_enabled': False,
        'comments_prompt': '',
        'alert_threshold': 2,
        'questions': [
            {'question': 'How would you rate your experience?', 'scale_type': 'numbers', 'position': 0},
        ],
    },
]

# Rating personality per location: (mean, std_dev)
LOCATION_BIAS = {
    'Main Counter':  (4.2, 0.8),
    'Drive-Through': (3.6, 1.1),
    'Patio':         (4.5, 0.6),
    'Restrooms':     (2.9, 1.2),
}

COMMENTS = [
    'Really great service, will be back!',
    'A bit slow today but the coffee was excellent.',
    'Friendly staff, made my morning.',
    'The seating area was a bit messy.',
    'Perfect as always.',
    'Waited too long, expected better.',
    'Loved the atmosphere.',
    "My order wasn't quite right but they fixed it quickly.",
    'Best espresso in town.',
    'The music was too loud.',
    '', '', '', '',   # most responses have no comment
]

DAYS = 30
RESPONSES_PER_DAY = (8, 25)


def clamp(val, lo, hi):
    return max(lo, min(hi, round(val)))


def _make_survey(org, defn):
    """Get-or-create a Survey and its Questions from a definition dict."""
    survey, _ = Survey.objects.get_or_create(
        organization=org,
        name=defn['name'],
        defaults={
            'comments_enabled': defn['comments_enabled'],
            'comments_prompt':  defn['comments_prompt'],
            'alert_threshold':  defn['alert_threshold'],
        },
    )
    for q in defn['questions']:
        Question.objects.get_or_create(
            survey=survey,
            question=q['question'],
            defaults={
                'organization': org,
                'scale_type':   q['scale_type'],
                'position':     q['position'],
            },
        )
    return survey


class Command(BaseCommand):
    help = 'Seed demo org with 30 days of realistic response data'

    def add_arguments(self, parser):
        parser.add_argument('--email', type=str, help='Attach demo org to this user')
        parser.add_argument('--flush', action='store_true', help='Delete and re-seed')

    def handle(self, *args, **options):
        if options['flush']:
            Organization.objects.filter(slug=DEMO_ORG_SLUG).delete()
            self.stdout.write(self.style.WARNING('Flushed demo org.'))

        # ── Org ───────────────────────────────────────────────────────────────
        org, created = Organization.objects.get_or_create(
            slug=DEMO_ORG_SLUG,
            defaults={
                'name':        'Demo Coffee Co.',
                'brand_color': '#c8975a',
                'is_test':     True,
            },
        )
        if not org.is_test:
            org.is_test = True
            org.save(update_fields=['is_test'])

        self.stdout.write(f'{"Created" if created else "Using existing"} org: {org.name}')

        # ── Attach to user ────────────────────────────────────────────────────
        if options['email']:
            try:
                user = User.objects.get(email=options['email'])
                user.organization = org
                user.save(update_fields=['organization'])
                self.stdout.write(f'Attached to user: {user.email}')
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'User {options["email"]} not found.'))
                return

        # ── Demo survey (used by /demo page) ──────────────────────────────────
        demo_survey = _make_survey(org, DEMO_SURVEY_DEF)
        self.stdout.write(f'Demo survey ready: {demo_survey.name}')

        # ── Stable demo location ──────────────────────────────────────────────
        main_location, _ = Location.objects.get_or_create(
            id=DEMO_MAIN_LOCATION_ID,
            defaults={
                'organization': org,
                'name':         'Main Counter',
                'survey':       demo_survey,
            },
        )

        # ── Analytics surveys ─────────────────────────────────────────────────
        analytics_surveys = [_make_survey(org, defn) for defn in ANALYTICS_SURVEY_DEFS]
        self.stdout.write(f'Analytics surveys ready: {len(analytics_surveys)}')

        # ── Other locations — assigned to the primary analytics survey ────────
        primary_survey = analytics_surveys[0]
        locations = [main_location]
        for loc_name in LOCATION_NAMES[1:]:   # Main Counter already created above
            loc, _ = Location.objects.get_or_create(
                organization=org,
                name=loc_name,
                defaults={'survey': primary_survey},
            )
            locations.append(loc)

        self.stdout.write(f'Locations ready: {len(locations)}')

        # ── Response data ─────────────────────────────────────────────────────
        now = timezone.now()
        session_count  = 0
        response_count = 0

        for days_ago in range(DAYS, 0, -1):
            base_day = (now - timedelta(days=days_ago)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for _ in range(random.randint(*RESPONSES_PER_DAY)):
                loc     = random.choice(locations)
                survey  = random.choice(analytics_surveys)
                qs      = list(survey.questions.order_by('position'))
                if not qs:
                    continue

                mean, std  = LOCATION_BIAS.get(loc.name, (3.8, 1.0))
                session_id = uuid.uuid4()
                created_at = base_day + timedelta(
                    hours=random.randint(7, 21),
                    minutes=random.randint(0, 59),
                )

                for i, question in enumerate(qs):
                    rating   = clamp(random.gauss(mean, std) + random.gauss(0, 0.3), 1, 5)
                    is_first = (i == 0)

                    resp = SurveyResponse.objects.create(
                        session_id = session_id,
                        location   = loc,
                        survey     = survey,
                        question   = question,
                        rating     = rating,
                        comment    = random.choice(COMMENTS) if is_first else '',
                        email      = '',
                    )
                    SurveyResponse.objects.filter(pk=resp.pk).update(created_at=created_at)

                    if rating <= survey.alert_threshold:
                        alert, _ = Alert.objects.get_or_create(
                            survey_response=resp,
                            defaults={
                                'location': loc,
                                'rating':   rating,
                                'status':   random.choice(['pending', 'resolved']),
                            },
                        )
                        Alert.objects.filter(pk=alert.pk).update(created_at=created_at)

                    response_count += 1
                session_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done — {session_count} sessions, {response_count} responses '
            f'across {DAYS} days and {len(locations)} locations.'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'\nDemo location ID — set this as DEMO_LOCATION_ID on Fly.io:\n'
            f'  {main_location.id}\n'
        ))
        if not options['email']:
            self.stdout.write(self.style.WARNING(
                'Tip: run with --email your@email.com to attach this org to your account.'
            ))