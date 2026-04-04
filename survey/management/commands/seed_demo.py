"""
Management command: seed_demo

Creates a realistic demo organization with locations, surveys, and 30 days of
response data so the insights dashboard has something to display.

Usage:
    python manage.py seed_demo
    python manage.py seed_demo --email you@example.com   # attach to your account
    python manage.py seed_demo --flush                   # wipe and re-seed
"""

import random
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone

from survey.models import Organization, User, Survey, Location, SurveyResponse, Alert

DEMO_ORG_SLUG = 'demo-coffee-co'

LOCATIONS = ['Main Counter', 'Drive-Through', 'Patio', 'Restrooms']

SURVEYS = [
    {
        'name': 'General Experience',
        'question': 'How was your visit today?',
        'scale_type': 'stars',
        'comments_enabled': True,
        'comments_prompt': 'Tell us more…',
    },
    {
        'name': 'Staff Friendliness',
        'question': 'How friendly was our team?',
        'scale_type': 'emoji',
        'comments_enabled': False,
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
            _, deleted = Organization.objects.filter(slug=DEMO_ORG_SLUG).delete()
            self.stdout.write(self.style.WARNING(f'Flushed demo org'))

        # Org
        org, created = Organization.objects.get_or_create(
            slug=DEMO_ORG_SLUG,
            defaults={'name': 'Demo Coffee Co.', 'brand_color': '#c8975a'},
        )
        self.stdout.write(f'{"Created" if created else "Using"} org: {org.name}')

        # Optionally attach to a real user account
        if options['email']:
            try:
                user = User.objects.get(email=options['email'])
                user.organization = org
                user.save()
                self.stdout.write(f'Attached to user: {user.email}')
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'User {options["email"]} not found'))

        # Surveys
        surveys = []
        for d in SURVEYS:
            s, _ = Survey.objects.get_or_create(
                organization=org, name=d['name'],
                defaults={k: v for k, v in d.items() if k != 'name'},
            )
            surveys.append(s)

        # Locations
        locations = []
        for name in LOCATIONS:
            loc, _ = Location.objects.get_or_create(
                organization=org, name=name,
                defaults={'survey': surveys[0]},
            )
            locations.append(loc)

        # Responses
        now = timezone.now()
        count = 0
        for days_ago in range(DAYS, 0, -1):
            base_day = (now - timedelta(days=days_ago)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for _ in range(random.randint(*RESPONSES_PER_DAY)):
                loc = random.choice(locations)
                survey = random.choice(surveys)
                mean, std = LOCATION_BIAS.get(loc.name, (3.8, 1.0))
                rating = clamp(random.gauss(mean, std), 1, 5)
                created_at = base_day + timedelta(
                    hours=random.randint(7, 21),
                    minutes=random.randint(0, 59),
                )

                resp = SurveyResponse(
                    location=loc,
                    survey=survey,
                    rating=rating,
                    comment=random.choice(COMMENTS),
                )
                resp.save()
                # Backdate past auto_now_add
                SurveyResponse.objects.filter(pk=resp.pk).update(created_at=created_at)

                if rating <= 2:
                    Alert.objects.get_or_create(
                        survey_response=resp,
                        defaults={
                            'location': loc,
                            'rating': rating,
                            'status': random.choice(['pending', 'resolved']),
                            'created_at': created_at,
                        },
                    )
                count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {count} responses across {DAYS} days across {len(locations)} locations.'
        ))
        if not options['email']:
            self.stdout.write(self.style.WARNING(
                'Run with --email your@email.com to attach this org to your account.'
            ))