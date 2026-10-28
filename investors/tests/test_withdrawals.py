from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from investors import crypto, geo, withdrawals
from investors.models import Investment, InvestorProfile, PayoutAccount, Transaction, WithdrawalRequest

PW = 'correct-horse-battery-9'
SECRET = 'origin-secret-for-tests'
KEY = Fernet.generate_key().decode()
BLOCKED = 'Withdrawals can only be initiated from within the United States and the United Kingdom.'
US = {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'US'}
UK = {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'GB'}
FR = {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'FR'}   # a country that is NOT allowed
GOOD_ROUTING, GOOD_ACCOUNT = '021000021', '123456789'

cfg = override_settings(CF_ORIGIN_SECRET=SECRET, FIELD_ENCRYPTION_KEY=KEY)


@cfg
class GeoTests(TestCase):
    def check(self, **meta):
        return geo.withdrawals_allowed(RequestFactory().get('/', **meta))

    def test_the_united_kingdom_is_allowed_too(self):
        self.assertEqual(self.check(**UK), (True, 'GB'))
        self.assertEqual(self.check(HTTP_X_ORIGIN_VERIFY=SECRET, HTTP_CF_IPCOUNTRY='gb'), (True, 'GB'))   # case-insensitive

    def test_only_the_united_states_and_united_kingdom_are_allowed(self):
        from django.conf import settings
        self.assertEqual(set(settings.WITHDRAWAL_ALLOWED_COUNTRIES), {'US', 'GB'})
        for code in ('FR', 'DE', 'IE', 'CA', 'NG', 'AU', 'CN', 'RU', 'IN', 'UK'):   # note: 'UK' is not Cloudflare's code for Britain
            self.assertEqual(self.check(HTTP_X_ORIGIN_VERIFY=SECRET, HTTP_CF_IPCOUNTRY=code), (False, code), code)

    def test_message_names_exactly_the_allowed_countries(self):
        self.assertEqual(geo.BLOCKED_MESSAGE, 'Withdrawals can only be initiated from within the United States and the United Kingdom.')
        with override_settings(WITHDRAWAL_ALLOWED_COUNTRIES=('US',)):
            self.assertEqual(geo._blocked_message(), 'Withdrawals can only be initiated from within the United States.')
        with override_settings(WITHDRAWAL_ALLOWED_COUNTRIES=('US', 'GB', 'CA')):
            self.assertEqual(geo._blocked_message(),
                             'Withdrawals can only be initiated from within the United States, the United Kingdom and CA.')

    def test_us_through_cloudflare_is_allowed(self):
        self.assertEqual(self.check(**US), (True, 'US'))
        self.assertEqual(self.check(HTTP_X_ORIGIN_VERIFY=SECRET, HTTP_CF_IPCOUNTRY='us'), (True, 'US'))

    def test_other_country_is_blocked(self):
        self.assertEqual(self.check(**FR), (False, 'FR'))

    def test_unknown_is_blocked_in_every_form(self):
        cases = [
            {},                                                             # no Cloudflare headers at all
            {'HTTP_CF_IPCOUNTRY': 'US'},                                    # forged country, no secret
            {'HTTP_CF_IPCOUNTRY': 'US', 'HTTP_X_ORIGIN_VERIFY': 'wrong'},   # forged country, wrong secret
            {'HTTP_X_ORIGIN_VERIFY': SECRET},                               # secret but no country
            {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'XX'},    # Cloudflare could not tell
            {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'T1'},    # Tor
            {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'USA'},   # malformed
            {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': ''},
        ]
        for meta in cases:
            self.assertEqual(self.check(**meta), (False, None), meta)

    @override_settings(CF_ORIGIN_SECRET='')
    def test_fails_closed_when_secret_not_configured(self):
        self.assertEqual(self.check(**US), (False, None))
        self.assertEqual(self.check(HTTP_X_ORIGIN_VERIFY='', HTTP_CF_IPCOUNTRY='US'), (False, None))

    @override_settings(DEBUG=True, DEV_COUNTRY='FR')
    def test_dev_override_only_in_debug(self):
        self.assertEqual(self.check(**US), (False, 'FR'))

    @override_settings(DEBUG=False, DEV_COUNTRY='US')
    def test_dev_override_ignored_outside_debug(self):
        self.assertEqual(self.check(), (False, None))


@cfg
class CryptoTests(TestCase):
    def test_roundtrip_and_ciphertext_hides_plaintext(self):
        token = crypto.encrypt('123456789')
        self.assertNotIn('123456789', token)
        self.assertEqual(crypto.decrypt(token), '123456789')
        self.assertEqual(crypto.encrypt(''), '')

    def test_wrong_key_cannot_decrypt(self):
        token = crypto.encrypt('123456789')
        with override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode()), self.assertRaises(ValueError):
            crypto.decrypt(token)

    @override_settings(FIELD_ENCRYPTION_KEY='', DEBUG=False)
    def test_missing_key_is_an_error_in_production(self):
        with self.assertRaises(ImproperlyConfigured):
            crypto.encrypt('1234')

    @override_settings(FIELD_ENCRYPTION_KEY='not-a-key', DEBUG=False)
    def test_malformed_key_is_an_error(self):
        with self.assertRaises(ImproperlyConfigured):
            crypto.encrypt('1234')

    def test_mask_never_shows_more_than_last_four(self):
        self.assertEqual(crypto.mask('123456789'), '••••6789')
        self.assertEqual(crypto.mask(''), '')

    def test_routing_checksum(self):
        self.assertTrue(withdrawals.valid_routing_number('021000021'))
        for bad in ('021000022', '12345678', '0210000210', 'abcdefghi', ''):
            self.assertFalse(withdrawals.valid_routing_number(bad), bad)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        today = date.today()
        self.user = User.objects.create_user('jane', 'jane@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=self.user, display_name='Jane')
        self.inv = Investment.objects.create(
            investor=self.profile, plan_name='Growth', principal=Decimal('10000'), annual_rate=Decimal('10'),
            compounding='annual', start_date=today - timedelta(days=730), maturity_date=today + timedelta(days=365))
        other_user = User.objects.create_user('bob', 'bob@example.com', PW)
        self.other = InvestorProfile.objects.create(user=other_user, display_name='Bob')
        self.other_inv = Investment.objects.create(
            investor=self.other, plan_name='Bob Plan', principal=Decimal('5000'), annual_rate=Decimal('8'),
            start_date=today - timedelta(days=100), maturity_date=today + timedelta(days=900))
        self.client.login(username='jane', password=PW)

    def account(self, investor=None, verified=True):
        acct = PayoutAccount(investor=investor or self.profile, label='Chase checking', bank_name='Chase',
                             holder_name='Jane Doe', is_verified=verified)
        acct.set_numbers(GOOD_ACCOUNT, GOOD_ROUTING)
        acct.save()
        return acct

    def submit(self, meta=US, **fields):
        data = {'investment': self.inv.pk, 'amount': '1000.00', 'destination': 'offline',
                'password': PW, 'confirm': 'on'}
        data.update(fields)
        return self.client.post(reverse('investors:withdrawal_new'), data, **meta)


@cfg
class CountryRuleViewTests(Base):
    def test_us_sees_the_form(self):
        r = self.client.get(reverse('investors:withdrawal_new'), **US)
        self.assertContains(r, 'Submit request')
        self.assertNotContains(r, BLOCKED)

    def test_a_uk_investor_sees_the_form_and_can_submit(self):
        r = self.client.get(reverse('investors:withdrawal_new'), **UK)
        self.assertContains(r, 'Submit request')
        self.assertNotContains(r, BLOCKED)
        with mock.patch('investors.emails._send', return_value=True):
            r = self.submit(meta=UK)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WithdrawalRequest.objects.get().detected_country, 'GB')   # recorded for staff

    def test_foreign_and_unknown_get_the_identical_message_and_no_form(self):
        responses = [
            self.client.get(reverse('investors:withdrawal_new'), **FR),
            self.client.get(reverse('investors:withdrawal_new')),                                   # no headers
            self.client.get(reverse('investors:withdrawal_new'), HTTP_CF_IPCOUNTRY='US'),           # forged
            self.client.get(reverse('investors:withdrawal_new'), HTTP_X_ORIGIN_VERIFY=SECRET, HTTP_CF_IPCOUNTRY='XX'),
        ]
        for r in responses:
            self.assertContains(r, BLOCKED)
            self.assertNotContains(r, 'Submit request')

    def test_post_from_blocked_location_creates_nothing(self):
        for meta in (FR, {}, {'HTTP_CF_IPCOUNTRY': 'US'}):
            r = self.submit(meta=meta)
            self.assertEqual(r.status_code, 403)
            self.assertContains(r, BLOCKED, status_code=403)
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_list_page_shows_banner_not_button_when_blocked(self):
        r = self.client.get(reverse('investors:withdrawals'), **FR)
        self.assertContains(r, BLOCKED)
        self.assertNotContains(r, 'New withdrawal request</a>')
        self.assertContains(self.client.get(reverse('investors:withdrawals'), **US), 'New withdrawal request</a>')

    def test_blocked_investor_can_still_view_existing_requests(self):
        wr = self.make_request()
        self.assertEqual(self.client.get(reverse('investors:withdrawal_detail', args=[wr.pk]), **FR).status_code, 200)

    def make_request(self, **kw):
        defaults = dict(investor=self.profile, investment=self.inv, amount=Decimal('500'), currency='USD',
                        destination_type='offline', destination_label='To be arranged with staff')
        defaults.update(kw)
        return WithdrawalRequest.objects.create(**defaults)


@cfg
class SubmissionTests(Base):
    def test_offline_request_is_created_with_country_and_emails(self):
        with mock.patch('investors.emails._send', return_value=True) as send, self.captureOnCommitCallbacks(execute=True):
            r = self.submit()
        wr = WithdrawalRequest.objects.get()
        self.assertRedirects(r, reverse('investors:withdrawal_detail', args=[wr.pk]))
        self.assertEqual((wr.status, wr.detected_country, wr.destination_type), ('submitted', 'US', 'offline'))
        self.assertTrue(wr.reference.startswith('WD-'))
        self.assertEqual(send.call_count, 2)  # investor confirmation + staff alert
        for call in send.call_args_list:
            body = ' '.join(str(a) for a in call.args)
            for secret in ('1000', '1,000', GOOD_ACCOUNT, GOOD_ROUTING):
                self.assertNotIn(secret, body)
        self.assertIn('did not make this request', send.call_args_list[0].args[2])

    def test_typed_details_are_encrypted_and_only_masked_afterwards(self):
        with mock.patch('investors.emails._send', return_value=True):
            r = self.submit(destination='typed', holder_name='Jane Doe', bank_name='Chase',
                            routing_number='021 000 021', account_number='1234-56789')
        wr = WithdrawalRequest.objects.get()
        self.assertEqual(wr.account_last4, '6789')
        self.assertNotIn(GOOD_ACCOUNT, wr.account_number_enc + wr.routing_number_enc)
        self.assertEqual(crypto.decrypt(wr.account_number_enc), GOOD_ACCOUNT)
        self.assertEqual(crypto.decrypt(wr.routing_number_enc), GOOD_ROUTING)
        self.assertEqual(wr.destination_label, 'Chase ••••6789')
        for url in (reverse('investors:withdrawal_detail', args=[wr.pk]), reverse('investors:withdrawals')):
            page = self.client.get(url, **US).content.decode()
            self.assertNotIn(GOOD_ACCOUNT, page)
            self.assertNotIn(GOOD_ROUTING, page)
            self.assertIn('••••6789', page)

    def test_typed_details_validated(self):
        base = dict(destination='typed', holder_name='J', bank_name='Chase')
        for routing, account in [('021000022', GOOD_ACCOUNT), (GOOD_ROUTING, '12'), (GOOD_ROUTING, 'abcdef'), ('', '')]:
            r = self.submit(routing_number=routing, account_number=account, **base)
            self.assertEqual(r.status_code, 200)
        r = self.submit(destination='typed', routing_number=GOOD_ROUTING, account_number=GOOD_ACCOUNT)  # no names
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_saved_account_flow_and_ownership(self):
        acct = self.account()
        with mock.patch('investors.emails._send', return_value=True):
            self.submit(destination='saved', saved_account=acct.pk)
        wr = WithdrawalRequest.objects.get()
        self.assertEqual(wr.payout_account, acct)
        self.assertEqual(wr.destination_label, 'Chase checking (••••6789)')
        WithdrawalRequest.objects.all().delete()
        for bad in (self.account(investor=self.other), self.account(verified=False)):
            r = self.submit(destination='saved', saved_account=bad.pk)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_saved_option_hidden_without_verified_accounts(self):
        page = self.client.get(reverse('investors:withdrawal_new'), **US).content.decode()
        self.assertNotIn('Account on file', page)
        self.account()
        self.assertIn('Account on file', self.client.get(reverse('investors:withdrawal_new'), **US).content.decode())

    def test_wrong_password_rejected_and_throttled(self):
        r = self.submit(password='wrong')
        self.assertContains(r, 'not correct')
        for _ in range(5):
            self.submit(password='wrong')
        r = self.submit()  # right password, but locked out
        self.assertContains(r, 'Too many failed attempts')
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_confirmation_checkbox_required(self):
        data = {'investment': self.inv.pk, 'amount': '10', 'destination': 'offline', 'password': PW}
        self.client.post(reverse('investors:withdrawal_new'), data, **US)
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_cannot_request_more_than_available_or_from_others_investment(self):
        self.assertEqual(self.submit(amount='999999').status_code, 200)
        self.assertEqual(self.submit(investment=self.other_inv.pk).status_code, 200)
        self.assertEqual(self.submit(amount='0').status_code, 200)
        self.assertEqual(self.submit(amount='-5').status_code, 200)
        self.assertEqual(self.submit(amount='10.123').status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_open_requests_reduce_what_can_be_requested(self):
        available = withdrawals.available_to_withdraw(self.inv)
        self.assertGreater(available, Decimal('10000'))
        with mock.patch('investors.emails._send', return_value=True):
            self.submit(amount=str(available - Decimal('100')))
        self.assertAlmostEqual(withdrawals.available_to_withdraw(self.inv), Decimal('100'), delta=Decimal('0.01'))
        self.assertEqual(self.submit(amount='200').status_code, 200)  # more than what's left
        self.assertEqual(WithdrawalRequest.objects.count(), 1)

    def test_completed_withdrawals_also_reduce_available(self):
        before = withdrawals.available_to_withdraw(self.inv)
        Transaction.objects.create(investment=self.inv, type='withdrawal', amount=Decimal('2500'), date=date.today())
        self.assertEqual(before - withdrawals.available_to_withdraw(self.inv), Decimal('2500'))

    def test_declined_and_cancelled_free_up_the_amount(self):
        wr = WithdrawalRequest.objects.create(investor=self.profile, investment=self.inv, amount=Decimal('4000'),
                                              currency='USD', destination_type='offline')
        low = withdrawals.available_to_withdraw(self.inv)
        for status in ('declined', 'cancelled'):
            wr.status = status
            wr.save()
            self.assertEqual(withdrawals.available_to_withdraw(self.inv) - low, Decimal('4000'))

    def test_daily_limit(self):
        with mock.patch('investors.emails._send', return_value=True):
            for _ in range(withdrawals.MAX_OPEN_REQUESTS_PER_DAY):
                self.submit(amount='10')
            r = self.submit(amount='10')
        self.assertContains(r, 'several requests today')
        self.assertEqual(WithdrawalRequest.objects.count(), withdrawals.MAX_OPEN_REQUESTS_PER_DAY)

    def test_email_failure_never_loses_the_request(self):
        with mock.patch('investors.emails._send', side_effect=RuntimeError('down')), self.captureOnCommitCallbacks(execute=True):
            r = self.submit()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WithdrawalRequest.objects.count(), 1)


@cfg
class InvestorAccessTests(Base):
    def setUp(self):
        super().setUp()
        self.mine = WithdrawalRequest.objects.create(investor=self.profile, investment=self.inv, amount=Decimal('100'),
                                                     currency='USD', destination_type='offline')
        self.theirs = WithdrawalRequest.objects.create(investor=self.other, investment=self.other_inv, amount=Decimal('50'),
                                                       currency='USD', destination_type='offline')

    def test_cannot_see_or_cancel_someone_elses_request(self):
        self.assertEqual(self.client.get(reverse('investors:withdrawal_detail', args=[self.theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('investors:withdrawal_cancel', args=[self.theirs.pk])).status_code, 404)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.status, 'submitted')
        self.assertNotContains(self.client.get(reverse('investors:withdrawals')), self.theirs.reference)

    def test_cancel_requires_post(self):
        self.assertEqual(self.client.get(reverse('investors:withdrawal_cancel', args=[self.mine.pk])).status_code, 405)

    def test_investor_can_cancel_early_but_not_once_approved(self):
        self.client.post(reverse('investors:withdrawal_cancel', args=[self.mine.pk]))
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.status, 'cancelled')
        self.theirs_status = None
        approved = WithdrawalRequest.objects.create(investor=self.profile, investment=self.inv, amount=Decimal('1'),
                                                    currency='USD', destination_type='offline', status='approved')
        self.client.post(reverse('investors:withdrawal_cancel', args=[approved.pk]))
        approved.refresh_from_db()
        self.assertEqual(approved.status, 'approved')

    def test_declined_reason_is_shown_to_investor_but_staff_note_is_not(self):
        self.mine.status, self.mine.decline_reason, self.mine.staff_note = 'declined', 'Amount exceeds the plan terms.', 'INTERNAL-ONLY'
        self.mine.save()
        page = self.client.get(reverse('investors:withdrawal_detail', args=[self.mine.pk])).content.decode()
        self.assertIn('Amount exceeds the plan terms.', page)
        self.assertNotIn('INTERNAL-ONLY', page)

    def test_anonymous_redirected(self):
        self.client.logout()
        for url in (reverse('investors:withdrawals'), reverse('investors:withdrawal_new'),
                    reverse('investors:withdrawal_detail', args=[self.mine.pk])):
            self.assertEqual(self.client.get(url, **US).status_code, 302)


@cfg
class StaffWorkflowTests(Base):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser('root', 'root@example.com', PW)
        self.wr = WithdrawalRequest.objects.create(
            investor=self.profile, investment=self.inv, amount=Decimal('750'), currency='USD',
            destination_type='typed', holder_name='Jane Doe', bank_name='Chase', destination_label='Chase ••••6789')
        self.wr.set_typed_details(GOOD_ACCOUNT, GOOD_ROUTING)
        self.wr.save()
        self.client.logout()
        self.client.force_login(self.staff)
        self.url = reverse('admin:investors_withdrawalrequest_change', args=[self.wr.pk])

    def move(self, status, callback=False, reason='', note=''):
        data = {'status': status, 'decline_reason': reason, 'staff_note': note}
        if callback:
            data['callback_verified'] = 'on'
        with mock.patch('investors.emails._send', return_value=True) as send, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.url, data)
        self.wr.refresh_from_db()
        return r, send

    def test_cannot_skip_steps(self):
        r, _ = self.move('approved', callback=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.wr.status, 'submitted')
        r, _ = self.move('completed', callback=True)
        self.assertEqual(self.wr.status, 'submitted')

    def test_review_notifies_investor_generically(self):
        r, send = self.move('under_review')
        self.assertEqual(self.wr.status, 'under_review')
        self.assertEqual(self.wr.decided_by, self.staff)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], 'jane@example.com')
        self.assertNotIn('750', send.call_args.args[2])

    def test_decline_needs_a_real_reason(self):
        r, _ = self.move('declined')
        self.assertEqual(self.wr.status, 'submitted')
        self.assertContains(r, 'real reason')
        self.move('declined', reason='Not enough time has passed since your deposit.')
        self.assertEqual(self.wr.status, 'declined')

    def test_typed_details_need_callback_before_approval_and_completion(self):
        self.move('under_review')
        r, _ = self.move('approved')
        self.assertEqual(self.wr.status, 'under_review')
        self.assertContains(r, 'Call the investor back')
        self.move('approved', callback=True)
        self.assertEqual(self.wr.status, 'approved')

    def test_saved_account_does_not_need_callback(self):
        acct = self.account()
        self.wr.destination_type, self.wr.payout_account = 'saved', acct
        self.wr.status = 'under_review'
        self.wr.save()
        self.move('approved')
        self.assertEqual(self.wr.status, 'approved')

    def test_completing_records_one_withdrawal_and_one_email(self):
        self.move('under_review')
        self.move('approved', callback=True)
        r, send = self.move('completed', callback=True)
        self.assertEqual(self.wr.status, 'completed')
        tx = Transaction.objects.get(investment=self.inv, type='withdrawal')
        self.assertEqual((tx.amount, self.wr.transaction), (Decimal('750.00'), tx))
        self.assertIn(self.wr.reference, tx.note)
        self.assertEqual(send.call_count, 1)  # the update email only, not also a generic 'transaction' email
        self.move('completed', callback=True)
        self.assertEqual(Transaction.objects.filter(type='withdrawal').count(), 1)

    def test_terminal_states_cannot_be_reopened(self):
        self.move('declined', reason='Because.')
        r, _ = self.move('under_review')
        self.assertEqual(self.wr.status, 'declined')

    def test_staff_cannot_add_or_delete_requests(self):
        self.assertEqual(self.client.get(reverse('admin:investors_withdrawalrequest_add')).status_code, 403)
        self.assertEqual(self.client.get(reverse('admin:investors_withdrawalrequest_delete', args=[self.wr.pk])).status_code, 403)

    def test_admin_pages_never_show_full_numbers(self):
        for url in (self.url, reverse('admin:investors_withdrawalrequest_changelist')):
            page = self.client.get(url).content.decode()
            self.assertNotIn(GOOD_ACCOUNT, page)
            self.assertNotIn(GOOD_ROUTING, page)
        self.assertIn('••••6789', self.client.get(self.url).content.decode())


@cfg
class RevealTests(Base):
    def setUp(self):
        super().setUp()
        self.wr = WithdrawalRequest.objects.create(
            investor=self.profile, investment=self.inv, amount=Decimal('750'), currency='USD', destination_type='typed',
            holder_name='Jane Doe', bank_name='Chase')
        self.wr.set_typed_details(GOOD_ACCOUNT, GOOD_ROUTING)
        self.wr.save()
        self.url = reverse('admin:investors_withdrawalrequest_reveal', args=[self.wr.pk])
        self.client.logout()

    def staff_with(self, *codenames):
        user = User.objects.create_user('clerk', 'c@example.com', PW, is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.force_login(user)
        return user

    def test_confirmation_page_shows_nothing_secret_and_logs_nothing(self):
        self.client.force_login(User.objects.create_superuser('root', 'r@example.com', PW))
        page = self.client.get(self.url).content.decode()
        self.assertIn('this is logged', page)
        self.assertNotIn(GOOD_ACCOUNT, page)
        self.assertFalse(LogEntry.objects.filter(change_message__contains='Revealed').exists())

    def test_post_reveals_once_and_is_logged(self):
        staff = User.objects.create_superuser('root', 'r@example.com', PW)
        self.client.force_login(staff)
        r = self.client.post(self.url)
        self.assertContains(r, GOOD_ACCOUNT)
        self.assertContains(r, GOOD_ROUTING)
        self.assertIn('no-store', r['Cache-Control'])
        entry = LogEntry.objects.get(change_message__contains='Revealed')
        self.assertEqual((entry.user, entry.object_id), (staff, str(self.wr.pk)))

    def test_staff_without_permission_is_refused_and_nothing_logged(self):
        self.staff_with('view_withdrawalrequest', 'change_withdrawalrequest')
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertFalse(LogEntry.objects.filter(change_message__contains='Revealed').exists())
        self.assertNotContains(self.client.get(reverse('admin:investors_withdrawalrequest_change', args=[self.wr.pk])), 'Reveal bank details')

    def test_staff_with_permission_gets_the_link(self):
        self.staff_with('view_withdrawalrequest', 'change_withdrawalrequest', 'reveal_bank_details')
        self.assertContains(self.client.get(reverse('admin:investors_withdrawalrequest_change', args=[self.wr.pk])), 'Reveal bank details')
        self.assertContains(self.client.post(self.url), GOOD_ACCOUNT)

    def test_saved_account_details_revealed_through_request(self):
        acct = self.account()
        self.wr.destination_type, self.wr.payout_account = 'saved', acct
        self.wr.save()
        self.client.force_login(User.objects.create_superuser('root', 'r@example.com', PW))
        self.assertContains(self.client.post(self.url), GOOD_ACCOUNT)

    def test_offline_request_has_nothing_to_reveal(self):
        self.wr.destination_type = 'offline'
        self.wr.save()
        self.client.force_login(User.objects.create_superuser('root', 'r@example.com', PW))
        self.assertContains(self.client.post(self.url), 'no bank details to show')

    def test_wrong_key_shows_a_clear_error_instead_of_crashing(self):
        self.client.force_login(User.objects.create_superuser('root', 'r@example.com', PW))
        with override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            self.assertContains(self.client.post(self.url), 'cannot be decrypted')


@cfg
class PayoutAccountAdminTests(Base):
    def setUp(self):
        super().setUp()
        self.client.logout()
        self.client.force_login(User.objects.create_superuser('root', 'r@example.com', PW))

    def add(self, **kw):
        data = {'investor': self.profile.pk, 'label': 'Chase checking', 'bank_name': 'Chase', 'holder_name': 'Jane Doe',
                'account_number': GOOD_ACCOUNT, 'routing_number': GOOD_ROUTING, 'is_verified': 'on'}
        data.update(kw)
        return self.client.post(reverse('admin:investors_payoutaccount_add'), data)

    def test_numbers_stored_encrypted_with_masked_display(self):
        self.assertEqual(self.add().status_code, 302)
        acct = PayoutAccount.objects.get()
        self.assertNotIn(GOOD_ACCOUNT, acct.account_number_enc)
        self.assertEqual(acct.account_last4, '6789')
        self.assertIsNotNone(acct.verified_at)
        page = self.client.get(reverse('admin:investors_payoutaccount_changelist')).content.decode()
        self.assertIn('••••6789', page)
        self.assertNotIn(GOOD_ACCOUNT, page)
        self.assertNotIn(GOOD_ACCOUNT, self.client.get(reverse('admin:investors_payoutaccount_change', args=[acct.pk])).content.decode())

    def test_validation_and_required_numbers_on_add(self):
        for bad in ({'routing_number': '021000022'}, {'account_number': '12'}, {'account_number': ''}, {'routing_number': ''}):
            self.assertEqual(self.add(**bad).status_code, 200)
        self.assertEqual(PayoutAccount.objects.count(), 0)

    def test_blank_numbers_on_edit_keep_stored_values(self):
        self.add()
        acct = PayoutAccount.objects.get()
        self.client.post(reverse('admin:investors_payoutaccount_change', args=[acct.pk]), {
            'investor': self.profile.pk, 'label': 'Renamed', 'bank_name': 'Chase', 'holder_name': 'Jane Doe',
            'account_number': '', 'routing_number': '', 'is_verified': 'on'})
        acct.refresh_from_db()
        self.assertEqual(acct.label, 'Renamed')
        self.assertEqual(crypto.decrypt(acct.account_number_enc), GOOD_ACCOUNT)
        self.assertEqual(crypto.decrypt(acct.routing_number_enc), GOOD_ROUTING)

    def test_unverifying_clears_verified_at(self):
        self.add()
        acct = PayoutAccount.objects.get()
        self.client.post(reverse('admin:investors_payoutaccount_change', args=[acct.pk]), {
            'investor': self.profile.pk, 'label': 'x', 'bank_name': 'Chase', 'holder_name': 'Jane Doe',
            'account_number': '', 'routing_number': ''})
        acct.refresh_from_db()
        self.assertFalse(acct.is_verified)
        self.assertIsNone(acct.verified_at)


@cfg
class MyLocationTests(Base):
    def test_staff_can_check_how_their_connection_is_seen(self):
        self.client.logout()
        self.client.force_login(User.objects.create_superuser('root', 'r@example.com', PW))
        url = reverse('admin:investors_withdrawalrequest_my_location')
        page = self.client.get(url, **US).content.decode()
        self.assertIn('<strong>Yes</strong>', page)
        self.assertNotIn(SECRET, page)  # the secret itself is never displayed
        page = self.client.get(url).content.decode()
        self.assertIn('<strong>No</strong>', page)
        self.assertIn('Transform Rule is missing', page)
