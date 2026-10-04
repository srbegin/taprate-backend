"""
Data migration: move per-org billing state into per-product Subscriptions.

Every existing org becomes a TapRate subscriber (all pre-split data is TapRate).
Claimed NFC tags inherit their location's product; unclaimed tags stay
unallocated (''). Reversible: the reverse copies the TapRate subscription back
onto the org fields (which still exist at this point in the history).
"""
from django.db import migrations

KNOWN_PLANS = {'starter', 'growth'}


def forwards(apps, schema_editor):
    Organization = apps.get_model('survey', 'Organization')
    Subscription = apps.get_model('survey', 'Subscription')
    NfcTag       = apps.get_model('survey', 'NfcTag')

    for org in Organization.objects.all():
        Subscription.objects.get_or_create(
            organization=org,
            product='taprate',
            defaults={
                # Org.plan defaulted to 'free' — never carry that into plan logic.
                'plan':                   org.plan if org.plan in KNOWN_PLANS else '',
                'status':                 org.subscription_status,
                'stripe_subscription_id': org.stripe_subscription_id,
                'trial_ends_at':          org.trial_ends_at,
            },
        )

    for tag in NfcTag.objects.filter(location__isnull=False).select_related('location'):
        tag.product = tag.location.product
        tag.save(update_fields=['product'])


def backwards(apps, schema_editor):
    Subscription = apps.get_model('survey', 'Subscription')

    for sub in Subscription.objects.filter(product='taprate').select_related('organization'):
        org = sub.organization
        org.plan                   = sub.plan or 'free'
        org.subscription_status    = sub.status
        org.stripe_subscription_id = sub.stripe_subscription_id
        org.trial_ends_at          = sub.trial_ends_at
        org.save(update_fields=[
            'plan', 'subscription_status', 'stripe_subscription_id', 'trial_ends_at',
        ])
    Subscription.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('survey', '0017_subscription_and_product'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
