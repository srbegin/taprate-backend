import csv
from django.core.management.base import BaseCommand
from survey.models import NfcTag


class Command(BaseCommand):
    help = 'Import NFC tag UUIDs from a Seritag CSV file.'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='Path to the CSV file')

    def handle(self, *args, **options):
        path = options['csv_file']
        created = skipped = 0

        with open(path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                uid = row.get('UID list', '').strip()
                if not uid:
                    continue
                _, was_created = NfcTag.objects.get_or_create(id=uid)
                if was_created:
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done — {created} tags imported, {skipped} already existed.'
        ))