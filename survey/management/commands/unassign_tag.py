from django.core.management.base import BaseCommand, CommandError
from survey.models import NfcTag


class Command(BaseCommand):
    help = 'Unassign an NFC tag by UUID, making it available to claim again.'

    def add_arguments(self, parser):
        parser.add_argument('uuid', type=str, help='UUID of the NFC tag to unassign')

    def handle(self, *args, **options):
        try:
            tag = NfcTag.objects.get(id=options['uuid'])
        except NfcTag.DoesNotExist:
            raise CommandError(f"No tag found with UUID: {options['uuid']}")

        tag.organization = None
        tag.location = None
        tag.claimed_at = None
        tag.save()

        self.stdout.write(self.style.SUCCESS(f"Tag {options['uuid']} unassigned successfully."))