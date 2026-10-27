import os
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings
from django.urls import reverse

from investors import crypto, services
from investors.demo_data import INVESTORS, STYLES
from investors.models import Document, Investment, InvestorProfile, PayoutAccount, WithdrawalRequest
from investors.services import add_months

PW = 'correct-horse-battery-9'
KEY = Fernet.generate_key().decode()
CHANGELIST = reverse('admin:investors_investorprofile_changelist')
PHRASE = 'FILL DEMO HISTORY'


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=self.tmp)
        self.addCleanup(setattr, field, 'storage', keep)
        self.root = User.objects.create_superuser('root', 'root@corp.com', PW)
        self.client.force_login(self.root)

    def make(self, username, name, demo=True, email=None, staff=False):
        user = User.objects.create_user(username, email or f'{username}@custom-domain.com', 'their-own-pass-123', is_staff=staff)
        return InvestorProfile.objects.create(user=user, display_name=name, is_demo=demo, phone='555')

    def post(self, profiles, confirm=PHRASE, apply=True, **extra):
        data = {'action': 'fill_with_demo_history', '_selected_action': [p.pk for p in profiles]}
        if apply:
            data.update({'apply': '1', 'confirm': confirm, 'style': 'demo', 'years': '6'})
        data.update(extra)
        return self.client.post(CHANGELIST, data, follow=True)


class ConfirmationPageTests(Base):
    def test_shows_what_exists_and_changes_nothing_yet(self):
        p = self.make('d1', 'Demo One')
        r = self.post([p], apply=False)
        self.assertContains(r, 'Fill with demo history')
        self.assertContains(r, 'Demo One')
        self.assertContains(r, PHRASE)
        self.assertFalse(Investment.objects.exists())

    def test_wrong_phrase_or_bad_inputs_change_nothing(self):
        p = self.make('d1', 'Demo One')
        self.assertContains(self.post([p], confirm='fill'), 'exactly to confirm')
        self.assertContains(self.post([p], years='0'), 'between 1 and 10')
        self.assertContains(self.post([p], style='not-a-style'), 'Choose one of the listed styles')
        self.assertFalse(Investment.objects.exists())

    def test_offers_every_style(self):
        p = self.make('d1', 'Demo One')
        page = self.post([p], apply=False).content.decode()
        for key in STYLES:
            self.assertIn(f'value="{key}"', page)


class FillingTests(Base):
    def test_fills_an_empty_demo_account_with_the_full_history_and_keeps_its_identity(self):
        p = self.make('d1', 'Demo One')
        old_hash, old_email = p.user.password, p.user.email
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            r = self.post([p])
        self.assertContains(r, 'Filled: Demo One')
        self.assertContains(r, 'logins are unchanged')
        p.refresh_from_db()
        p.user.refresh_from_db()
        self.assertEqual((p.user.password, p.user.email, p.user.username, p.display_name, p.phone), (old_hash, old_email, 'd1', 'Demo One', '555'))
        self.assertTrue(p.is_demo)
        self.assertEqual(min(p.investments.values_list('start_date', flat=True)), add_months(date.today(), -72))
        totals = services.portfolio_summary(p)['by_currency']
        self.assertGreaterEqual(max(t['value'] for t in totals), Decimal('570000'))
        self.assertGreaterEqual(p.withdrawals.filter(status='completed').count(), 3)
        self.assertGreaterEqual(p.withdrawals.filter(status='declined').count(), 2)
        self.assertEqual(p.documents.count(), 0)                     # no PDFs
        self.assertEqual(p.payout_accounts.first().holder_name, 'Demo One')            # uses the account's own name
        self.assertTrue(all(wr.holder_name in ('', 'Demo One') for wr in p.withdrawals.all()))
        send.assert_not_called()

    def test_every_style_works_and_meets_the_floor(self):
        for n, style in enumerate(STYLES):
            p = self.make(f'd{n}', f'Demo {n}')
            self.post([p], style=style)
            main = max(t['value'] for t in services.portfolio_summary(p)['by_currency'])
            self.assertGreaterEqual(main, Decimal('570000'), style)
        flagship = InvestorProfile.objects.get(user__username='d' + str(list(STYLES).index('colin-firth')))
        self.assertGreater(max(t['value'] for t in services.portfolio_summary(flagship)['by_currency']), Decimal('1000000'))

    def test_the_years_option_is_honoured(self):
        p = self.make('d1', 'Demo One')
        self.post([p], years='3')
        heritage = p.investments.get(plan_name='Heritage Growth Plan')
        self.assertEqual(heritage.start_date, add_months(date.today(), -36))

    def test_several_accounts_at_once(self):
        a, b = self.make('d1', 'One'), self.make('d2', 'Two')
        self.post([a, b])
        self.assertTrue(a.investments.exists() and b.investments.exists())
        self.assertEqual(LogEntry.objects.filter(change_message__startswith='Filled with demo history').count(), 2)

    def test_the_action_is_logged_with_who_and_what(self):
        p = self.make('d1', 'Demo One')
        self.post([p])
        entry = LogEntry.objects.get(object_id=str(p.pk), change_message__startswith='Filled with demo history')
        self.assertEqual(entry.user, self.root)

    def test_the_filled_account_can_be_used_in_the_portal(self):
        p = self.make('d1', 'Demo One')
        self.post([p])
        self.client.logout()
        self.client.post(reverse('investors:login'), {'username': 'd1', 'password': 'their-own-pass-123'})
        for name in ('home', 'performance', 'documents', 'withdrawals'):
            self.assertEqual(self.client.get(reverse(f'investors:{name}')).status_code, 200, name)


class SafetyTests(Base):
    def test_real_clients_are_refused_outright_even_with_everything_ticked(self):
        real = self.make('realclient', 'Real Client', demo=False)
        r = self.post([real], replace='on')
        self.assertContains(r, 'Not a demo account: Real Client')
        self.assertFalse(Investment.objects.exists())

    def test_a_mixed_selection_changes_nothing_at_all(self):
        demo, real = self.make('d1', 'Demo One'), self.make('realclient', 'Real Client', demo=False)
        self.post([demo, real])
        self.assertFalse(Investment.objects.exists())

    def test_staff_accounts_are_refused_even_if_flagged(self):
        staff_profile = self.make('staffer', 'Staffer', demo=True, staff=True)
        self.assertContains(self.post([staff_profile]), 'Not a demo account')
        self.assertFalse(Investment.objects.exists())

    def test_existing_data_is_never_overwritten_by_default(self):
        p = self.make('d1', 'Demo One')
        Investment.objects.create(investor=p, plan_name='Hand-made', principal=Decimal('999'), annual_rate=Decimal('5'),
                                  start_date=date(2025, 1, 1), maturity_date=date(2028, 1, 1))
        r = self.post([p])
        self.assertContains(r, 'already has data')
        self.assertEqual(list(p.investments.values_list('plan_name', flat=True)), ['Hand-made'])

    def test_running_it_twice_does_not_duplicate(self):
        p = self.make('d1', 'Demo One')
        self.post([p])
        count = p.investments.count()
        self.assertContains(self.post([p]), 'already has data')
        self.assertEqual(p.investments.count(), count)

    @mock.patch('investors.demo_data.INCLUDE_DOCUMENTS', True)   # this test is about stored PDFs being replaced
    def test_replace_rebuilds_only_the_selected_account_and_removes_its_files(self):
        target, bystander = self.make('d1', 'Demo One'), self.make('d2', 'Demo Two')
        self.post([target, bystander])
        files = [d.file.name for d in target.documents.all()]
        keep_ids = set(bystander.investments.values_list('pk', flat=True))
        old_ids = set(target.investments.values_list('pk', flat=True))
        self.post([target], replace='on')
        self.assertTrue(old_ids.isdisjoint(set(target.investments.values_list('pk', flat=True))))   # rebuilt
        self.assertEqual(set(bystander.investments.values_list('pk', flat=True)), keep_ids)         # untouched
        self.assertFalse(any(os.path.exists(os.path.join(self.tmp, f)) for f in files))              # old PDFs gone
        self.assertEqual(target.investments.count(), len([p for p in INVESTORS if p['username'] == 'demo'][0]['plans']))
        self.assertEqual(PayoutAccount.objects.filter(investor=target).count(), 2)                  # not doubled

    def test_replace_never_touches_real_clients_data(self):
        demo = self.make('d1', 'Demo One')
        real = self.make('realclient', 'Real Client', demo=False)
        inv = Investment.objects.create(investor=real, plan_name='Real Plan', principal=Decimal('75000'), annual_rate=Decimal('9'),
                                        start_date=date(2024, 1, 1), maturity_date=date(2028, 1, 1))
        self.post([demo])
        self.post([demo], replace='on')
        self.assertTrue(Investment.objects.filter(pk=inv.pk).exists())
        self.assertEqual(real.investments.count(), 1)

    def test_existing_verified_accounts_on_file_are_reused_not_duplicated(self):
        p = self.make('d1', 'Demo One')
        acct = PayoutAccount(investor=p, label='Mine', bank_name='My Bank', holder_name='Demo One', is_verified=True)
        acct.set_numbers('4455667788', '021000021')
        acct.save()
        self.post([p])
        self.assertEqual(list(PayoutAccount.objects.filter(investor=p).values_list('label', flat=True)), ['Mine'])
        self.assertTrue(p.withdrawals.filter(destination_type='saved', payout_account=acct).exists())


class PermissionTests(Base):
    def clerk(self):
        user = User.objects.create_user('clerk', 'clerk@corp.com', PW, is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label='investors'))
        return user

    def test_ordinary_staff_neither_see_nor_can_use_the_action(self):
        p = self.make('d1', 'Demo One')
        self.client.force_login(self.clerk())
        self.assertNotContains(self.client.get(CHANGELIST), 'fill_with_demo_history')
        self.assertNotContains(self.client.get(CHANGELIST), 'sign_in_as_demo_investor')
        self.client.post(CHANGELIST, {'action': 'fill_with_demo_history', '_selected_action': [p.pk], 'apply': '1',
                                      'confirm': PHRASE, 'style': 'demo', 'years': '6'})
        self.assertFalse(Investment.objects.exists())

    def test_superusers_see_the_action(self):
        self.make('d1', 'Demo One')   # Django hides the actions menu on an empty list
        page = self.client.get(CHANGELIST)
        self.assertContains(page, 'fill_with_demo_history')
        self.assertContains(page, 'sign_in_as_demo_investor')
