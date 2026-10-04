"""
Tests for the product layer: per-product subscriptions, X-Product scoping,
brand config, billing, NFC tag claims, brand-aware email, and the 0018 data
migration.

Run against Postgres (DATABASE_URL) with a Redis cache (REDIS_URL), e.g.:
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/taprate \\
    REDIS_URL=redis://localhost:6379/9 python manage.py test survey
"""
import os
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import tasks
from .models import (
    Alert, ContactSubmission, Location, NfcTag, Organization, Subscription, Survey,
    SurveyResponse, User,
)
from .products import CLEANPULSE, TAPRATE
from .views import billing_views


# ── Helpers ──────────────────────────────────────────────────────────────────

def trial(days=10):
    return {'status': 'trialing', 'trial_ends_at': timezone.now() + timedelta(days=days)}


def expired_trial():
    return {'status': 'trialing', 'trial_ends_at': timezone.now() - timedelta(days=1)}


def make_org(name='Acme', **subscriptions):
    org = Organization.objects.create(name=name, slug=name.lower().replace(' ', '-'))
    for product, fields in subscriptions.items():
        Subscription.objects.create(organization=org, product=product, **fields)
    return org


def make_user(org, email='owner@example.com'):
    return User.objects.create_user(
        username=email, email=email, password='local-test-pw-1234',
        organization=org, role='owner',
    )


def api(user=None, product=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    if product:
        client.credentials(HTTP_X_PRODUCT=product)
    return client


class FakeStripeEvent(dict):
    """Mimics the stripe.Event surface WebhookView uses."""
    def _to_dict_recursive(self):
        return dict(self)


def stripe_event(etype, obj):
    return FakeStripeEvent(type=etype, data={'object': obj})


# ── Access ───────────────────────────────────────────────────────────────────

class SubscriptionAccessTests(TestCase):

    def test_access_is_per_product(self):
        org = make_org(taprate=trial())
        self.assertTrue(org.has_access(TAPRATE))
        self.assertFalse(org.has_access(CLEANPULSE))

    def test_active_subscription_allows_access_after_trial(self):
        org = make_org(taprate={'status': 'active', 'plan': 'starter'})
        self.assertTrue(org.has_access(TAPRATE))

    def test_expired_trial_blocks(self):
        org = make_org(cleanpulse=expired_trial())
        self.assertFalse(org.has_access(CLEANPULSE))

    def test_dashboard_403_names_the_brand(self):
        user = make_user(make_org(taprate=trial()))
        res = api(user, CLEANPULSE).get('/api/dashboard/locations/')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['code'], 'subscription_required')
        self.assertIn('Cleanpulse', str(res.data['detail']))

    def test_no_header_means_taprate(self):
        user = make_user(make_org(taprate=trial()))
        self.assertEqual(api(user).get('/api/dashboard/locations/').status_code, 200)

    def test_unknown_product_rejected(self):
        user = make_user(make_org(taprate=trial()))
        self.assertEqual(api(user, 'acme').get('/api/dashboard/locations/').status_code, 400)


# ── Scoping ──────────────────────────────────────────────────────────────────

class ProductScopingTests(TestCase):

    def setUp(self):
        self.org  = make_org(taprate=trial(), cleanpulse=trial())
        self.user = make_user(self.org)

    def test_location_created_with_request_product_and_listed_per_product(self):
        res = api(self.user, CLEANPULSE).post('/api/dashboard/locations/', {'name': "Men's Room"}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Location.objects.get(id=res.data['id']).product, CLEANPULSE)

        cp = api(self.user, CLEANPULSE).get('/api/dashboard/locations/').data['items']
        tr = api(self.user).get('/api/dashboard/locations/').data['items']
        self.assertEqual([l['name'] for l in cp], ["Men's Room"])
        self.assertEqual(tr, [])

    def test_other_products_location_is_404(self):
        loc = Location.objects.create(organization=self.org, name='Front Desk', product=TAPRATE)
        self.assertEqual(api(self.user, CLEANPULSE).get(f'/api/dashboard/locations/{loc.id}/').status_code, 404)
        self.assertEqual(api(self.user).get(f'/api/dashboard/locations/{loc.id}/').status_code, 200)

    def test_cannot_assign_other_products_survey_to_location(self):
        survey = Survey.objects.create(organization=self.org, name='Sales follow-up', product=TAPRATE)
        res = api(self.user, CLEANPULSE).post(
            '/api/dashboard/locations/', {'name': 'Restroom', 'survey': str(survey.id)}, format='json',
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('survey', res.data)

    def test_surveys_listed_per_product(self):
        Survey.objects.create(organization=self.org, name='Restroom check', product=CLEANPULSE)
        Survey.objects.create(organization=self.org, name='Sales follow-up', product=TAPRATE)
        names = [s['name'] for s in api(self.user, CLEANPULSE).get('/api/dashboard/surveys/').data['items']]
        self.assertEqual(names, ['Restroom check'])

    def test_alerts_listed_per_product(self):
        for product, name in [(CLEANPULSE, 'Restroom'), (TAPRATE, 'Front Desk')]:
            survey = Survey.objects.create(organization=self.org, name=name, product=product)
            loc = Location.objects.create(organization=self.org, name=name, product=product, survey=survey)
            resp = SurveyResponse.objects.create(location=loc, survey=survey, rating=1)
            Alert.objects.create(survey_response=resp, location=loc, rating=1)
        alerts = api(self.user, CLEANPULSE).get('/api/dashboard/alerts/').data['items']
        self.assertEqual([a['location'] for a in alerts], ['Restroom'])

    def test_location_link_uses_brand_domain_and_qr_route(self):
        with mock.patch.dict(os.environ, {'FRONTEND_URL': 'https://taprate.app',
                                          'CLEANPULSE_FRONTEND_URL': 'https://cleanpulse.app'}):
            cp = Location.objects.create(organization=self.org, name='Restroom', product=CLEANPULSE)
            tr = Location.objects.create(organization=self.org, name='Desk', product=TAPRATE)
            self.assertEqual(api(self.user, CLEANPULSE).get(f'/api/dashboard/locations/{cp.id}/').data['nfc_url'],
                             f'https://cleanpulse.app/qr/{cp.id}')
            self.assertEqual(api(self.user).get(f'/api/dashboard/locations/{tr.id}/').data['nfc_url'],
                             f'https://taprate.app/qr/{tr.id}')

    def test_location_delete_resyncs_overage_for_its_product(self):
        loc = Location.objects.create(organization=self.org, name='Restroom', product=CLEANPULSE)
        with mock.patch('survey.views.dashboard_views._sync_overage_quantity') as sync:
            res = api(self.user, CLEANPULSE).delete(f'/api/dashboard/locations/{loc.id}/')
        self.assertEqual(res.status_code, 204)
        sync.assert_called_once_with(self.org, CLEANPULSE)


# ── Registration, trials, /auth/me ───────────────────────────────────────────

class RegistrationAndTrialTests(TestCase):

    def test_register_starts_trial_for_requested_product(self):
        payload = {
            'email': 'new@example.com', 'password': 'local-test-pw-1234',
            'first_name': 'Pat', 'last_name': 'Lee', 'org_name': 'Bean There',
            'invite_code': 'test-invite',
        }
        with mock.patch.dict(os.environ, {'REGISTRATION_CODE': 'test-invite'}), \
             mock.patch('survey.tasks.send_welcome_email.delay') as welcome:
            res = api(product=CLEANPULSE).post('/api/auth/register/', payload, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        org = User.objects.get(email='new@example.com').organization
        self.assertEqual([s.product for s in org.subscriptions.all()], [CLEANPULSE])
        sub = org.get_subscription(CLEANPULSE)
        self.assertEqual(sub.status, 'trialing')
        self.assertIn(sub.trial_days_remaining, (29, 30))
        welcome.assert_called_once()
        self.assertEqual(welcome.call_args.args[1], CLEANPULSE)
        self.assertEqual(res.data['user']['organization']['product'], CLEANPULSE)

    def test_start_trial_for_second_product_is_idempotent(self):
        org  = make_org(taprate={'status': 'active', 'plan': 'growth'})
        user = make_user(org)
        first  = api(user, CLEANPULSE).post('/api/billing/trial/')
        second = api(user, CLEANPULSE).post('/api/billing/trial/')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['status'], 'trialing')
        self.assertEqual(org.subscriptions.filter(product=CLEANPULSE).count(), 1)
        self.assertEqual(org.get_subscription(TAPRATE).status, 'active')  # untouched

    def test_me_reports_billing_for_request_product_under_legacy_keys(self):
        org  = make_org(taprate={'status': 'active', 'plan': 'growth'}, cleanpulse=trial(days=12))
        user = make_user(org)
        Location.objects.create(organization=org, name='Desk', product=TAPRATE)

        tr = api(user).get('/api/auth/me/').data['organization']
        cp = api(user, CLEANPULSE).get('/api/auth/me/').data['organization']

        self.assertEqual((tr['product'], tr['plan'], tr['subscription_status'], tr['location_count']),
                         (TAPRATE, 'growth', 'active', 1))
        self.assertEqual((cp['product'], cp['plan'], cp['subscription_status'], cp['location_count']),
                         (CLEANPULSE, 'free', 'trialing', 0))
        self.assertIn(cp['trial_days_remaining'], (11, 12))
        self.assertEqual(sorted(s['product'] for s in cp['subscriptions']), [CLEANPULSE, TAPRATE])


class OrganizationSettingsTests(TestCase):

    def test_logo_url_validation(self):
        user = make_user(make_org(taprate=trial()))
        ok  = api(user).patch('/api/dashboard/organization/', {'logo_url': 'https://example.com/logo.png'}, format='json')
        bad = api(user).patch('/api/dashboard/organization/', {'logo_url': 'http://example.com/logo.png'}, format='json')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 400)


# ── Billing ──────────────────────────────────────────────────────────────────

class BillingTests(TestCase):

    def test_cleanpulse_checkout_disabled_until_priced(self):
        user = make_user(make_org(cleanpulse=trial()))
        with mock.patch('stripe.checkout.Session.create') as create:
            res = api(user, CLEANPULSE).post('/api/billing/checkout/', {'plan': 'starter'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Cleanpulse', res.data['detail'])
        create.assert_not_called()

    def test_checkout_refused_when_product_already_active(self):
        user = make_user(make_org(taprate={'status': 'active', 'plan': 'starter'}))
        with mock.patch.dict(billing_views.PRICE_IDS[TAPRATE], {'starter': 'price_s'}), \
             mock.patch('stripe.checkout.Session.create') as create:
            res = api(user).post('/api/billing/checkout/', {'plan': 'starter'}, format='json')
        self.assertEqual(res.status_code, 400)
        create.assert_not_called()

    def test_checkout_metadata_carries_product(self):
        org  = make_org(taprate=trial())
        user = make_user(org)
        org.stripe_customer_id = 'cus_test'
        org.save()
        with mock.patch.dict(billing_views.PRICE_IDS[TAPRATE], {'growth': 'price_g'}), \
             mock.patch('stripe.checkout.Session.create', return_value=mock.Mock(url='https://stripe.test/x')) as create:
            res = api(user).post('/api/billing/checkout/', {'plan': 'growth'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(create.call_args.kwargs['metadata']['product'], TAPRATE)

    def _post_webhook(self, event):
        with mock.patch('stripe.Webhook.construct_event', return_value=event):
            return APIClient().post('/api/billing/webhook/', b'{}', content_type='application/json',
                                    HTTP_STRIPE_SIGNATURE='t=1,v1=x')

    def test_webhook_checkout_completed_creates_product_subscription(self):
        org = make_org(taprate={'status': 'active', 'plan': 'growth', 'stripe_subscription_id': 'sub_tr'})
        res = self._post_webhook(stripe_event('checkout.session.completed', {
            'subscription': 'sub_cp',
            'metadata': {'org_id': str(org.id), 'plan': 'starter', 'product': CLEANPULSE},
        }))
        self.assertEqual(res.status_code, 200)
        cp = org.get_subscription(CLEANPULSE)
        self.assertEqual((cp.status, cp.plan, cp.stripe_subscription_id), ('active', 'starter', 'sub_cp'))
        self.assertEqual(org.get_subscription(TAPRATE).stripe_subscription_id, 'sub_tr')

    def test_webhook_update_matches_by_stripe_id_and_maps_plan(self):
        org = make_org(taprate={'status': 'active', 'plan': 'starter', 'stripe_subscription_id': 'sub_tr'})
        with mock.patch.dict(billing_views.PRICE_IDS[TAPRATE], {'starter': 'price_s', 'growth': 'price_g'}):
            self._post_webhook(stripe_event('customer.subscription.updated', {
                'id': 'sub_tr', 'status': 'past_due', 'metadata': {},
                'items': {'data': [{'price': {'id': 'price_g'}}]},
            }))
        sub = org.get_subscription(TAPRATE)
        self.assertEqual((sub.status, sub.plan), ('past_due', 'growth'))

    def test_webhook_without_product_metadata_is_taprate(self):
        org = make_org()
        self._post_webhook(stripe_event('customer.subscription.deleted', {
            'id': 'sub_legacy', 'status': 'canceled', 'metadata': {'org_id': str(org.id)},
            'items': {'data': []},
        }))
        sub = org.get_subscription(TAPRATE)
        self.assertEqual((sub.status, sub.stripe_subscription_id), ('canceled', 'sub_legacy'))

    def test_overage_counts_only_that_products_locations(self):
        org = make_org(taprate={'status': 'active', 'plan': 'starter', 'stripe_subscription_id': 'sub_tr'})
        for i in range(4):
            Location.objects.create(organization=org, name=f'Desk {i}', product=TAPRATE)
        for i in range(2):
            Location.objects.create(organization=org, name=f'Restroom {i}', product=CLEANPULSE)

        with mock.patch.dict(billing_views.OVERAGE_PRICE_IDS, {TAPRATE: 'price_over'}), \
             mock.patch('stripe.Subscription.retrieve', return_value={'items': {'data': []}}), \
             mock.patch('stripe.SubscriptionItem.create') as create:
            billing_views._sync_overage_quantity(org, TAPRATE)

        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs['quantity'], 1)  # 4 TapRate locations - 3 included


# ── NFC tags ─────────────────────────────────────────────────────────────────

class NfcTagProductTests(TestCase):

    def setUp(self):
        self.org  = make_org(taprate=trial(), cleanpulse=trial())
        self.user = make_user(self.org)

    def test_claim_sets_tag_product_and_get_reports_it(self):
        tag = NfcTag.objects.create()
        loc = Location.objects.create(organization=self.org, name='Restroom', product=CLEANPULSE)
        res = api(self.user, CLEANPULSE).post(f'/api/tags/{tag.id}/', {'location_id': str(loc.id)}, format='json')
        self.assertEqual(res.status_code, 200)
        tag.refresh_from_db()
        self.assertEqual(tag.product, CLEANPULSE)
        self.assertEqual(APIClient().get(f'/api/tags/{tag.id}/').data['product'], CLEANPULSE)

    def test_unclaimed_tag_reports_no_product(self):
        tag = NfcTag.objects.create()
        self.assertIsNone(APIClient().get(f'/api/tags/{tag.id}/').data['product'])

    def test_tag_allocated_to_other_product_cannot_be_claimed(self):
        tag = NfcTag.objects.create(product=TAPRATE)
        loc = Location.objects.create(organization=self.org, name='Restroom', product=CLEANPULSE)
        res = api(self.user, CLEANPULSE).post(f'/api/tags/{tag.id}/', {'location_id': str(loc.id)}, format='json')
        self.assertEqual(res.status_code, 403)


# ── Brand-aware email ────────────────────────────────────────────────────────

class BrandEmailTests(TestCase):

    def setUp(self):
        cache.clear()   # contact form rate limit lives in the cache
        env = {k: v for k, v in os.environ.items() if k != 'CLEANPULSE_EMAIL_ENABLED'}
        env.update({'RESEND_API_KEY': 're_test', 'ALERTS_FROM_EMAIL': 'alerts@taprate.app',
                    'TAPRATE_HELLO_EMAIL': 'hello@taprate.app'})
        env.pop('CONTACT_NOTIFY_EMAIL', None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cleanpulse_email_off_until_enabled(self):
        with mock.patch('resend.Emails.send') as send:
            result = tasks._send_email(to='a@b.com', subject='s', html_body='h', text_body='t', product=CLEANPULSE)
        self.assertIsNone(result)
        send.assert_not_called()

    def test_taprate_email_sent_from_taprate(self):
        with mock.patch('resend.Emails.send', return_value={'id': 'em_1'}) as send:
            result = tasks._send_email(to='a@b.com', subject='s', html_body='h', text_body='t', product=TAPRATE)
        self.assertTrue(result)
        self.assertEqual(send.call_args.args[0]['from'], 'TapRate <alerts@taprate.app>')

    def test_cleanpulse_alert_not_sent_and_not_marked_notified_while_disabled(self):
        org = make_org(cleanpulse=trial())
        org.alert_email = 'ops@example.com'
        org.save()
        survey = Survey.objects.create(organization=org, name='Restroom', product=CLEANPULSE)
        loc    = Location.objects.create(organization=org, name='Restroom', product=CLEANPULSE, survey=survey)
        resp   = SurveyResponse.objects.create(location=loc, survey=survey, rating=1)
        alert  = Alert.objects.create(survey_response=resp, location=loc, rating=1)
        with mock.patch('resend.Emails.send') as send:
            tasks.send_alert(str(alert.id))
        send.assert_not_called()
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'pending')

    def test_contact_lead_from_cleanpulse_notifies_ops_and_skips_autoreply(self):
        with mock.patch('resend.Emails.send', return_value={'id': 'em_1'}) as send:
            res = api(product=CLEANPULSE).post('/api/contact/', {
                'name': 'Sam', 'business_name': 'Gas & Go', 'email': 'sam@example.com',
            }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(ContactSubmission.objects.get().product, CLEANPULSE)
        self.assertEqual(send.call_count, 1)  # ops notification only — auto-reply is off for Cleanpulse
        msg = send.call_args.args[0]
        self.assertEqual(msg['to'], ['hello@taprate.app'])
        self.assertTrue(msg['subject'].startswith('[Cleanpulse]'))


# ── 0018 data migration ──────────────────────────────────────────────────────

class BillingDataMigrationTests(TransactionTestCase):
    before = [('survey', '0017_subscription_and_product')]
    after  = [('survey', '0018_copy_billing_to_subscriptions')]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_org_billing_copied_to_taprate_subscription(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        apps = executor.loader.project_state(self.before).apps
        Org      = apps.get_model('survey', 'Organization')
        Loc      = apps.get_model('survey', 'Location')
        Tag      = apps.get_model('survey', 'NfcTag')

        trial_end = timezone.now() + timedelta(days=5)
        paid  = Org.objects.create(name='Paid', slug='paid', plan='growth',
                                   subscription_status='active', stripe_subscription_id='sub_1')
        tryer = Org.objects.create(name='Trial', slug='trial', plan='free',
                                   subscription_status='trialing', trial_ends_at=trial_end)
        loc = Loc.objects.create(organization=paid, name='Desk')
        Tag.objects.create(organization=paid, location=loc)
        Tag.objects.create()

        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        apps = executor.loader.project_state(self.after).apps
        Sub = apps.get_model('survey', 'Subscription')
        Tag = apps.get_model('survey', 'NfcTag')

        p = Sub.objects.get(organization_id=paid.id)
        t = Sub.objects.get(organization_id=tryer.id)
        self.assertEqual((p.product, p.plan, p.status, p.stripe_subscription_id), ('taprate', 'growth', 'active', 'sub_1'))
        self.assertEqual((t.product, t.plan, t.status), ('taprate', '', 'trialing'))   # 'free' never carried over
        self.assertEqual(t.trial_ends_at, trial_end)
        self.assertEqual(sorted(Tag.objects.values_list('product', flat=True)), ['', 'taprate'])
