"""Per-investor overrides of the withdrawal location rule (set by superusers in the admin)."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from investors import geo
from investors.models import Investment, InvestorProfile, WithdrawalRequest

PW = 'correct-horse-battery-9'
SECRET = 'origin-secret-for-tests'


def via_cloudflare(country):
    return {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': country}


US, GB, CA, FR = (via_cloudflare(c) for c in ('US', 'GB', 'CA', 'FR'))
NOWHERE = {}                                                       # no Cloudflare headers: location unknown
FORGED = {'HTTP_CF_IPCOUNTRY': 'US'}                               # country claim without the secret
STANDARD = 'Withdrawals can only be initiated from within the United States and the United Kingdom.'


@override_settings(CF_ORIGIN_SECRET=SECRET)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        today = date.today()

        def investor(username, demo=False):
            user = User.objects.create_user(username, f'{username}@example.com', PW)
            profile = InvestorProfile.objects.create(user=user, display_name=username.title(), is_demo=demo)
            Investment.objects.create(investor=profile, plan_name=f'{username} plan', principal=Decimal('50000'),
                                      annual_rate=Decimal('9'), start_date=today - timedelta(days=700),
                                      maturity_date=today + timedelta(days=700))
            return profile

        self.jane, self.bob, self.demo = investor('jane'), investor('bob'), investor('demoacct', demo=True)
        self.client.login(username='jane', password=PW)

    def submit(self, meta, who='jane', amount='100'):
        self.client.login(username=who, password=PW)
        profile = InvestorProfile.objects.get(user__username=who)
        inv = profile.investments.get()
        return self.client.post(reverse('investors:withdrawal_new'), {
            'investment': inv.pk, 'amount': amount, 'destination': 'offline', 'password': PW, 'confirm': 'on'}, **meta)

    def page(self, meta, who='jane'):
        self.client.login(username=who, password=PW)
        return self.client.get(reverse('investors:withdrawal_new'), **meta)

    def set_rule(self, profile, **fields):
        for key, value in fields.items():
            setattr(profile, key, value)
        profile.full_clean()
        profile.save()


class DefaultsTests(Base):
    def test_everyone_starts_on_the_standard_rule(self):
        for p in (self.jane, self.demo):
            self.assertFalse(p.withdrawal_any_location)
            self.assertEqual((p.withdrawal_extra_countries, p.extra_countries, p.location_rule_label), ('', [], 'Standard'))
        self.assertContains(self.page(FR), STANDARD)
        self.assertEqual(self.submit(FR).status_code, 403)

    def test_standard_countries_do_not_count_as_overrides(self):
        with mock.patch('investors.emails._send', return_value=True):
            for meta in (US, GB):
                self.submit(meta)
        self.assertEqual(WithdrawalRequest.objects.count(), 2)
        self.assertFalse(WithdrawalRequest.objects.filter(location_override=True).exists())


class ExtraCountriesTests(Base):
    def test_an_extra_country_is_allowed_for_that_investor_only(self):
        self.set_rule(self.jane, withdrawal_extra_countries='CA')
        self.assertNotContains(self.page(CA), 'Withdrawal unavailable')
        with mock.patch('investors.emails._send', return_value=True):
            self.assertEqual(self.submit(CA).status_code, 302)
        wr = WithdrawalRequest.objects.get()
        self.assertEqual((wr.detected_country, wr.location_override), ('CA', True))
        self.assertEqual(self.submit(CA, who='bob').status_code, 403)         # someone else, same country: still refused

    def test_it_does_not_open_other_countries_or_unknown_locations(self):
        self.set_rule(self.jane, withdrawal_extra_countries='CA')
        for meta in (FR, NOWHERE, FORGED):
            self.assertEqual(self.submit(meta).status_code, 403, meta)
        self.assertFalse(WithdrawalRequest.objects.exists())

    def test_a_forged_country_cannot_use_an_extra_country(self):
        self.set_rule(self.jane, withdrawal_extra_countries='CA')
        forged = {'HTTP_CF_IPCOUNTRY': 'CA'}                                   # claims Canada without the origin secret
        self.assertEqual(self.submit(forged).status_code, 403)

    def test_the_standard_countries_keep_working_alongside(self):
        self.set_rule(self.jane, withdrawal_extra_countries='CA')
        with mock.patch('investors.emails._send', return_value=True):
            self.assertEqual(self.submit(US).status_code, 302)
        self.assertFalse(WithdrawalRequest.objects.get().location_override)

    def test_the_refusal_message_names_the_extras_for_that_investor(self):
        self.set_rule(self.jane, withdrawal_extra_countries='CA, IE')
        page = self.page(FR).content.decode()
        self.assertIn('the United States, the United Kingdom, CA and IE.', page)
        self.assertContains(self.page(FR, who='bob'), STANDARD)               # others still see the standard text

    def test_messy_but_valid_input_is_cleaned_up(self):
        self.set_rule(self.jane, withdrawal_extra_countries=' ie , ca  ca,')
        self.assertEqual(self.jane.withdrawal_extra_countries, 'CA, IE')
        self.assertEqual(self.jane.extra_countries, ['CA', 'IE'])
        self.assertEqual(self.jane.location_rule_label, 'Standard + CA, IE')

    def test_bad_codes_are_rejected_with_helpful_messages(self):
        for bad, text in (('UK', 'GB, not UK'), ('USA', 'two-letter'), ('C', 'two-letter'), ('1A', 'two-letter'), ('CA, UK', 'GB, not UK')):
            self.jane.withdrawal_extra_countries = bad
            with self.assertRaises(ValidationError) as ctx:
                self.jane.full_clean()
            self.assertIn(text, str(ctx.exception), bad)


class AnyLocationTests(Base):
    def test_allows_any_country_and_even_an_unknown_location(self):
        self.set_rule(self.jane, withdrawal_any_location=True)
        with mock.patch('investors.emails._send', return_value=True):
            for meta in (FR, NOWHERE, FORGED, CA):
                self.assertEqual(self.submit(meta).status_code, 302, meta)
        self.assertEqual(WithdrawalRequest.objects.filter(location_override=True).count(), 4)
        # what staff will see: real countries where Cloudflare vouched for them, blank where it could not
        self.assertEqual(sorted(WithdrawalRequest.objects.values_list('detected_country', flat=True)), ['', '', 'CA', 'FR'])

    def test_only_that_investor_is_affected(self):
        self.set_rule(self.jane, withdrawal_any_location=True)
        self.assertEqual(self.submit(FR, who='bob').status_code, 403)
        self.assertContains(self.page(NOWHERE, who='bob'), STANDARD)

    def test_it_applies_to_demo_accounts_too(self):
        self.set_rule(self.demo, withdrawal_any_location=True)
        with mock.patch('investors.emails._send', return_value=True):
            self.assertEqual(self.submit(FR, who='demoacct').status_code, 302)

    def test_switching_it_off_restores_the_block(self):
        self.set_rule(self.jane, withdrawal_any_location=True)
        self.assertNotContains(self.page(FR), 'Withdrawal unavailable')
        self.set_rule(self.jane, withdrawal_any_location=False)
        self.assertContains(self.page(FR), STANDARD)
        self.assertEqual(self.submit(FR).status_code, 403)

    def test_the_withdrawals_list_follows_the_override(self):
        r = self.client.get(reverse('investors:withdrawals'), **FR)
        self.assertContains(r, STANDARD)
        self.set_rule(self.jane, withdrawal_any_location=True)
        r = self.client.get(reverse('investors:withdrawals'), **FR)
        self.assertNotContains(r, STANDARD)
        self.assertContains(r, 'New withdrawal request</a>')


class OtherRulesStayInForceTests(Base):
    def test_the_override_only_relaxes_the_location_check(self):
        self.set_rule(self.jane, withdrawal_any_location=True)
        inv = self.jane.investments.get()
        base = {'investment': inv.pk, 'amount': '100', 'destination': 'offline', 'password': PW, 'confirm': 'on'}
        url = reverse('investors:withdrawal_new')
        self.assertEqual(self.client.post(url, {**base, 'password': 'wrong'}, **FR).status_code, 200)   # password still required
        self.assertEqual(self.client.post(url, {**base, 'amount': '999999999'}, **FR).status_code, 200)  # amount still limited
        data = dict(base); data.pop('confirm')
        self.assertEqual(self.client.post(url, data, **FR).status_code, 200)                              # confirmation still required
        self.assertFalse(WithdrawalRequest.objects.exists())

    def test_staff_cannot_use_the_portal_even_with_an_override(self):
        staff = User.objects.create_user('staffer', 's@example.com', PW, is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse('investors:withdrawal_new'), **FR).status_code, 404)


class AdminTests(Base):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('root', 'root@corp.com', PW)
        self.change = reverse('admin:investors_investorprofile_change', args=[self.jane.pk])

    def post(self, **extra):
        data = {'display_name': 'Jane', 'phone': '', 'is_active': 'on'}
        data.update(extra)
        return self.client.post(self.change, data)

    def clerk(self):
        user = User.objects.create_user('clerk', 'clerk@corp.com', PW, is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label='investors'))
        return user

    def test_superusers_see_and_can_set_both_fields(self):
        self.client.force_login(self.root)
        page = self.client.get(self.change).content.decode()
        self.assertIn('name="withdrawal_any_location"', page)
        self.assertIn('name="withdrawal_extra_countries"', page)
        self.assertEqual(self.post(withdrawal_extra_countries='ca, ie', withdrawal_any_location='on').status_code, 302)
        self.jane.refresh_from_db()
        self.assertTrue(self.jane.withdrawal_any_location)
        self.assertEqual(self.jane.withdrawal_extra_countries, 'CA, IE')

    def test_bad_input_is_shown_as_a_form_error(self):
        self.client.force_login(self.root)
        r = self.post(withdrawal_extra_countries='UK')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'GB, not UK')
        self.jane.refresh_from_db()
        self.assertEqual(self.jane.withdrawal_extra_countries, '')

    def test_ordinary_staff_can_not_change_them_even_by_forging_the_form(self):
        self.client.force_login(self.clerk())
        page = self.client.get(self.change).content.decode()
        self.assertNotIn('name="withdrawal_any_location"', page)
        self.post(withdrawal_extra_countries='CA', withdrawal_any_location='on')
        self.jane.refresh_from_db()
        self.assertFalse(self.jane.withdrawal_any_location)
        self.assertEqual(self.jane.withdrawal_extra_countries, '')

    def test_every_change_is_recorded_in_the_admin_history(self):
        self.client.force_login(self.root)
        self.post(withdrawal_any_location='on')
        entry = LogEntry.objects.filter(user=self.root, object_id=str(self.jane.pk)).latest('id')
        self.assertIn('withdrawal', entry.change_message.lower())

    def test_the_list_shows_each_investors_rule(self):
        self.set_rule(self.jane, withdrawal_extra_countries='CA')
        self.set_rule(self.bob, withdrawal_any_location=True)
        self.client.force_login(self.root)
        page = self.client.get(reverse('admin:investors_investorprofile_changelist')).content.decode()
        for label in ('Standard + CA', 'Anywhere', 'Standard'):
            self.assertIn(label, page)

    def test_reviewing_staff_can_see_which_requests_were_allowed_by_an_override(self):
        self.set_rule(self.jane, withdrawal_any_location=True)
        with mock.patch('investors.emails._send', return_value=True):
            self.submit(FR)
            self.submit(US, who='bob')
        self.client.force_login(self.root)
        flagged = WithdrawalRequest.objects.get(investor=self.jane)
        normal = WithdrawalRequest.objects.get(investor=self.bob)
        self.assertTrue(flagged.location_override and not normal.location_override)
        page = self.client.get(reverse('admin:investors_withdrawalrequest_change', args=[flagged.pk])).content.decode()
        self.assertIn('Location override', page)
        changelist = self.client.get(reverse('admin:investors_withdrawalrequest_changelist') + '?location_override__exact=1').content.decode()
        self.assertIn(flagged.reference, changelist)
        self.assertNotIn(normal.reference, changelist)

    def test_the_override_flag_cannot_be_edited_by_staff(self):
        self.set_rule(self.jane, withdrawal_any_location=True)
        with mock.patch('investors.emails._send', return_value=True):
            self.submit(FR)
        wr = WithdrawalRequest.objects.get()
        self.client.force_login(self.root)
        self.client.post(reverse('admin:investors_withdrawalrequest_change', args=[wr.pk]),
                         {'status': 'under_review', 'decline_reason': '', 'staff_note': '', 'location_override': ''})
        wr.refresh_from_db()
        self.assertTrue(wr.location_override)                                  # read-only for everyone


class GeoFunctionTests(Base):
    def decide(self, meta, profile=None):
        from django.test import RequestFactory
        return geo.withdrawal_location(RequestFactory().get('/', **meta), profile)

    def test_decisions(self):
        self.assertEqual(self.decide(US), (True, 'US', False))
        self.assertEqual(self.decide(FR), (False, 'FR', False))
        self.assertEqual(self.decide(FR, self.jane), (False, 'FR', False))
        self.set_rule(self.jane, withdrawal_extra_countries='FR')
        self.assertEqual(self.decide(FR, self.jane), (True, 'FR', True))
        self.assertEqual(self.decide(NOWHERE, self.jane), (False, None, False))
        self.set_rule(self.jane, withdrawal_any_location=True)
        self.assertEqual(self.decide(NOWHERE, self.jane), (True, None, True))

    def test_no_profile_means_the_standard_rule(self):
        self.assertEqual(self.decide(CA, None), (False, 'CA', False))

    def test_the_standard_message_is_unchanged_without_a_profile(self):
        self.assertEqual(geo.blocked_message(), STANDARD)
        self.assertEqual(geo.blocked_message(None), STANDARD)
