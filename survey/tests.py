"""
Tests for the product layer (per-product subscriptions, X-Product scoping,
brand config, billing, NFC tag claims, brand-aware email, the 0018 data
migration) and for issues questions (builder, submit, deduped alerts,
insights, issue alert email).

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
    Alert, ContactSubmission, IssueOption, Location, NfcTag, Organization, Question,
    ResponseIssue, Subscription, Survey, SurveyResponse, User,
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


# ── Issues questions ─────────────────────────────────────────────────────────

class IssueQuestionFixture:
    """A TapRate org with a restroom survey: a rating question then an issues question."""

    def setUp(self):
        cache.clear()
        self.org  = make_org(taprate=trial())
        self.user = make_user(self.org)
        self.survey = Survey.objects.create(organization=self.org, name='Restroom Check', alert_threshold=2)
        self.rating_q = Question.objects.create(
            organization=self.org, survey=self.survey, question='How clean?', scale_type='stars', position=0,
        )
        self.issues_q = Question.objects.create(
            organization=self.org, survey=self.survey, question='Anything need attention?',
            question_type='issues', position=1,
        )
        self.soap   = IssueOption.objects.create(question=self.issues_q, label='Out of soap', position=0)
        self.trash  = IssueOption.objects.create(question=self.issues_q, label='Trash is full', position=1)
        self.odor   = IssueOption.objects.create(question=self.issues_q, label='Bad odor', position=2, alerts=False)
        self.location = Location.objects.create(organization=self.org, name='Restroom 2', survey=self.survey)

    def token(self):
        res = APIClient().post(f'/api/survey/location/{self.location.id}/session/')
        self.assertEqual(res.status_code, 201)
        return res.data['token']

    def submit(self, issue_ids=(), rating=4, token=None):
        token = token or self.token()
        payload = {'responses': [
            {'question_id': str(self.rating_q.id), 'rating': rating},
            {'question_id': str(self.issues_q.id), 'issue_ids': [str(i) for i in issue_ids]},
        ]}
        return APIClient().post(f'/api/survey/{token}/response/', payload, format='json')


@mock.patch('survey.tasks.send_alert.delay')
@mock.patch('survey.tasks.send_issue_alerts.delay')
class IssueSubmitTests(IssueQuestionFixture, TestCase):

    def test_public_survey_includes_options_and_hides_inactive_questions(self, issue_mail, rating_mail):
        Question.objects.create(organization=self.org, survey=self.survey, question='Hidden', position=2, active=False)
        survey = APIClient().get(f'/api/survey/{self.token()}/').data['survey']
        self.assertEqual([q['question'] for q in survey['questions']], ['How clean?', 'Anything need attention?'])
        issues = survey['questions'][1]
        self.assertEqual(issues['question_type'], 'issues')
        self.assertEqual([o['label'] for o in issues['options']], ['Out of soap', 'Trash is full', 'Bad odor'])
        self.assertNotIn('alerts', issues['options'][0])   # internal flag stays private

    def test_issues_recorded_and_alerting_ones_alert(self, issue_mail, rating_mail):
        res = self.submit([self.soap.id, self.odor.id])
        self.assertEqual(res.status_code, 201, res.data)

        answer = SurveyResponse.objects.get(question=self.issues_q)
        self.assertIsNone(answer.rating)
        self.assertEqual(sorted(answer.issues.values_list('label', flat=True)), ['Bad odor', 'Out of soap'])

        alert = Alert.objects.get(kind='issue')            # 'Bad odor' has alerts off
        self.assertEqual((alert.issue_label, alert.report_count, alert.status), ('Out of soap', 1, 'pending'))
        issue_mail.assert_called_once_with([str(alert.id)])
        rating_mail.assert_not_called()

    def test_repeat_report_bumps_open_alert_without_new_email(self, issue_mail, rating_mail):
        self.submit([self.soap.id])
        self.submit([self.soap.id, self.trash.id])

        soap = Alert.objects.get(issue_label='Out of soap')
        self.assertEqual(soap.report_count, 2)
        self.assertEqual(Alert.objects.filter(kind='issue').count(), 2)
        self.assertEqual(issue_mail.call_count, 2)
        self.assertEqual(issue_mail.call_args_list[1].args[0],
                         [str(Alert.objects.get(issue_label='Trash is full').id)])   # only the new issue

    def test_resolving_rearms_the_issue(self, issue_mail, rating_mail):
        self.submit([self.soap.id])
        Alert.objects.filter(kind='issue').update(status='resolved')
        self.submit([self.soap.id])
        self.assertEqual(Alert.objects.filter(issue_label='Out of soap').count(), 2)
        self.assertEqual(issue_mail.call_count, 2)

    def test_nothing_selected_is_recorded_as_all_good(self, issue_mail, rating_mail):
        self.assertEqual(self.submit([]).status_code, 201)
        answer = SurveyResponse.objects.get(question=self.issues_q)
        self.assertEqual(answer.issues.count(), 0)
        self.assertFalse(Alert.objects.exists())
        issue_mail.assert_not_called()

    def test_low_rating_alert_still_works_alongside_issues(self, issue_mail, rating_mail):
        self.submit([self.trash.id], rating=1)
        self.assertEqual(sorted(Alert.objects.values_list('kind', flat=True)), ['issue', 'low_rating'])
        rating_mail.assert_called_once()

    def test_unknown_issue_rejected_without_burning_the_token(self, issue_mail, rating_mail):
        token = self.token()
        other = IssueOption.objects.create(
            question=Question.objects.create(organization=self.org, question_type='issues', question='x'),
            label='Elsewhere',
        )
        self.assertEqual(self.submit([other.id], token=token).status_code, 400)
        self.assertEqual(self.submit([self.soap.id], token=token).status_code, 201)

    def test_rating_question_requires_rating(self, issue_mail, rating_mail):
        token = self.token()
        payload = {'responses': [{'question_id': str(self.rating_q.id)}]}
        res = APIClient().post(f'/api/survey/{token}/response/', payload, format='json')
        self.assertEqual(res.status_code, 400)

    def test_inactive_question_rejected(self, issue_mail, rating_mail):
        self.issues_q.active = False
        self.issues_q.save()
        self.assertEqual(self.submit([self.soap.id]).status_code, 400)

    def test_test_mode_records_issues_but_raises_no_alerts(self, issue_mail, rating_mail):
        self.org.test_mode = True
        self.org.save()
        self.submit([self.soap.id], rating=1)
        self.assertEqual(ResponseIssue.objects.count(), 1)
        self.assertFalse(Alert.objects.exists())


class IssueBuilderTests(TestCase):

    def setUp(self):
        self.org    = make_org(taprate=trial())
        self.user   = make_user(self.org)
        self.survey = Survey.objects.create(organization=self.org, name='Restroom Check')
        self.url    = f'/api/dashboard/surveys/{self.survey.id}/questions/'

    def test_create_issues_question_with_options(self):
        res = api(self.user).post(self.url, {
            'question': 'Anything need attention?', 'question_type': 'issues',
            'options': [{'label': 'Out of soap'}, {'label': 'Trash is full', 'alerts': False}],
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual([(o['label'], o['alerts']) for o in res.data['options']],
                         [('Out of soap', True), ('Trash is full', False)])

    def test_issues_question_needs_options_and_unique_labels(self):
        empty = api(self.user).post(self.url, {'question': 'Q', 'question_type': 'issues', 'options': []}, format='json')
        dupes = api(self.user).post(self.url, {
            'question': 'Q', 'question_type': 'issues', 'options': [{'label': 'Soap'}, {'label': 'soap'}],
        }, format='json')
        self.assertEqual((empty.status_code, dupes.status_code), (400, 400))

    def test_update_options_keeps_ids_adds_and_removes_but_history_survives(self):
        q = Question.objects.create(organization=self.org, survey=self.survey, question='Q', question_type='issues')
        soap  = IssueOption.objects.create(question=q, label='Out of soap', position=0)
        trash = IssueOption.objects.create(question=q, label='Trash is full', position=1)
        resp  = SurveyResponse.objects.create(survey=self.survey, question=q)
        ResponseIssue.objects.create(survey_response=resp, option=trash, label=trash.label)

        res = api(self.user).patch(f'{self.url}{q.id}/', {'options': [
            {'id': str(soap.id), 'label': 'No soap'},
            {'label': 'Wet floor'},
        ]}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([o['label'] for o in res.data['options']], ['No soap', 'Wet floor'])
        self.assertEqual(res.data['options'][0]['id'], str(soap.id))
        self.assertEqual(ResponseIssue.objects.get().label, 'Trash is full')   # snapshot kept

    def test_switch_question_off(self):
        q = Question.objects.create(organization=self.org, survey=self.survey, question='Q')
        res = api(self.user).patch(f'{self.url}{q.id}/', {'active': False}, format='json')
        self.assertEqual((res.status_code, res.data['active']), (200, False))

    def test_new_survey_with_nested_issues_question(self):
        res = api(self.user).post('/api/dashboard/surveys/', {
            'name': 'Restroom', 'questions': [
                {'question': 'How clean?', 'scale_type': 'stars'},
                {'question': 'Issues?', 'question_type': 'issues', 'options': [{'label': 'Out of soap'}]},
            ],
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual([q['question_type'] for q in res.data['questions']], ['rating', 'issues'])
        self.assertEqual(res.data['questions'][1]['options'][0]['label'], 'Out of soap')


@mock.patch('survey.tasks.send_alert.delay')
@mock.patch('survey.tasks.send_issue_alerts.delay')
class IssueInsightsTests(IssueQuestionFixture, TestCase):

    def test_rating_metrics_ignore_issue_answers_and_issues_block_reports(self, issue_mail, rating_mail):
        self.submit([self.soap.id, self.trash.id], rating=4)
        self.submit([self.soap.id], rating=2)
        self.submit([], rating=5)

        data = api(self.user).get('/api/dashboard/insights/').data
        self.assertEqual(data['summary']['total_responses'], 3)          # rated answers only
        self.assertEqual(data['summary']['avg_rating'], round(11 / 3, 2))
        self.assertNotIn('None', data['distribution'])
        self.assertEqual(data['issues']['answered'], 3)
        self.assertEqual(data['issues']['with_issues'], 2)
        self.assertEqual(data['issues']['top'][0], {'label': 'Out of soap', 'count': 2})

        soap = next(a for a in data['pending_alerts'] if a['kind'] == 'issue' and a['issue_label'] == 'Out of soap')
        self.assertEqual(soap['report_count'], 2)


class IssueAlertEmailTests(IssueQuestionFixture, TestCase):

    def setUp(self):
        super().setUp()
        self.org.alert_email = 'ops@example.com'
        self.org.save()
        env = {k: v for k, v in os.environ.items()}
        env.update({'RESEND_API_KEY': 're_test', 'ALERTS_FROM_EMAIL': 'alerts@taprate.app'})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _alerts(self, comment=''):
        resp = SurveyResponse.objects.create(location=self.location, survey=self.survey,
                                             question=self.issues_q, comment=comment)
        return [
            Alert.objects.create(survey_response=resp, location=self.location, kind='issue',
                                 issue_option=o, issue_label=o.label)
            for o in (self.soap, self.trash)
        ]

    def test_one_email_lists_issues_escapes_comment_and_marks_notified(self):
        alerts = self._alerts(comment='<a href="http://evil">click</a>')
        with mock.patch('resend.Emails.send', return_value={'id': 'em_1'}) as send:
            tasks.send_issue_alerts([str(a.id) for a in alerts])
        send.assert_called_once()
        msg = send.call_args.args[0]
        self.assertEqual(msg['to'], ['ops@example.com'])
        self.assertIn('Out of soap, Trash is full', msg['subject'])
        self.assertNotIn('<a href="http://evil">', msg['html'])
        self.assertEqual(set(Alert.objects.values_list('status', flat=True)), {'owner_notified'})

    def test_alerts_disabled_sends_nothing(self):
        self.org.alerts_enabled = False
        self.org.save()
        alerts = self._alerts()
        with mock.patch('resend.Emails.send') as send:
            tasks.send_issue_alerts([str(a.id) for a in alerts])
        send.assert_not_called()
