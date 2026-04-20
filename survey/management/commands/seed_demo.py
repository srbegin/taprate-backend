"""
Management command: seed_demo

Creates a realistic demo organization with locations, survey sets, and 30 days
of response data so the insights dashboard has something to display.

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
    Organization, User, SurveySet, Survey,
    Location, SurveyResponse, Alert
)

DEMO_ORG_SLUG = 'demo-coffee-co'

LOCATIONS = ['Main Counter', 'Drive-Through', 'Patio', 'Restrooms']

SURVEY_SETS = [
    {
        'name': 'General Experience',
        'comments_enabled': True,
        'comments_prompt': 'Tell us more…',
        'alert_threshold': 2,
        'questions': [
            {'question': 'How was your visit today?',   'scale_type': 'stars',   'position': 0},
            {'question': 'How friendly was our team?',  'scale_type': 'emoji',   'position': 1},
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

# Each location has its own (mean, std_dev) rating personality
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
    '', '', '', '',  # blanks — most responses have no comment
]

DAYS = 30
RESPONSES_PER_DAY = (8, 25)


def clamp(val, lo, hi):
    return max(lo, min(hi, round(val)))


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
            },
        )
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

        # ── Survey sets + questions ───────────────────────────────────────────
        survey_sets = []
        for set_data in SURVEY_SETS:
            survey_set, _ = SurveySet.objects.get_or_create(
                organization=org,
                name=set_data['name'],
                defaults={
                    'comments_enabled': set_data['comments_enabled'],
                    'comments_prompt':  set_data['comments_prompt'],
                    'alert_threshold':  set_data['alert_threshold'],
                },
            )
            for q_data in set_data['questions']:
                Survey.objects.get_or_create(
                    survey_set=survey_set,
                    question=q_data['question'],
                    defaults={
                        'organization': org,
                        'scale_type':   q_data['scale_type'],
                        'position':     q_data['position'],
                    },
                )
            survey_sets.append(survey_set)

        self.stdout.write(f'Survey sets ready: {len(survey_sets)}')

        # ── Locations — all assigned to the primary survey set ────────────────
        primary_set = survey_sets[0]
        locations = []
        for loc_name in LOCATIONS:
            loc, _ = Location.objects.get_or_create(
                organization=org,
                name=loc_name,
                defaults={'survey_set': primary_set},
            )
            locations.append(loc)

        self.stdout.write(f'Locations ready: {len(locations)}')

        # ── Responses ─────────────────────────────────────────────────────────
        now = timezone.now()
        session_count  = 0
        response_count = 0

        for days_ago in range(DAYS, 0, -1):
            base_day = (now - timedelta(days=days_ago)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for _ in range(random.randint(*RESPONSES_PER_DAY)):
                loc        = random.choice(locations)
                survey_set = random.choice(survey_sets)
                questions  = list(survey_set.surveys.order_by('position'))
                if not questions:
                    continue

                mean, std  = LOCATION_BIAS.get(loc.name, (3.8, 1.0))
                session_id = uuid.uuid4()
                created_at = base_day + timedelta(
                    hours=random.randint(7, 21),
                    minutes=random.randint(0, 59),
                )

                for i, question in enumerate(questions):
                    rating   = clamp(random.gauss(mean, std) + random.gauss(0, 0.3), 1, 5)
                    is_first = (i == 0)

                    resp = SurveyResponse.objects.create(
                        session_id = session_id,
                        location   = loc,
                        survey_set = survey_set,
                        survey     = question,
                        rating     = rating,
                        comment    = random.choice(COMMENTS) if is_first else '',
                        email      = '',
                    )
                    # Backdate the response
                    SurveyResponse.objects.filter(pk=resp.pk).update(created_at=created_at)

                    if rating <= survey_set.alert_threshold:
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
        if not options['email']:
            self.stdout.write(self.style.WARNING(
                'Tip: run with --email your@email.com to attach this org to your account.'
            ))