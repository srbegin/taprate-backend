"""
Management command: wipe_demo

Removes the demo organization and all associated data. The user account
attached to it (if any) has its organization field cleared.

Usage:
    python manage.py wipe_demo
"""

from django.core.management.base import BaseCommand
from survey.models import Organization

DEMO_ORG_SLUG = 'demo-coffee-co'


class Command(BaseCommand):
    help = 'Wipe the demo organization and all associated data'

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=DEMO_ORG_SLUG)
        except Organization.DoesNotExist:
            self.stdout.write(self.style.WARNING('No demo org found — nothing to wipe.'))
            return

        # Clear org from any attached users before deleting
        # (FK is SET_NULL so this happens automatically, but being explicit)
        attached_users = org.members.all()
        count = attached_users.count()
        attached_users.update(organization=None)

        org.delete()  # cascades to locations, surveys, responses, alerts, tags

        self.stdout.write(self.style.SUCCESS(
            f'Demo org wiped. {count} user(s) detached.'
        ))