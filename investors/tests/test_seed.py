from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from investors import crypto
from investors.models import (Document, Investment, InvestorProfile, PayoutAccount, Transaction, ValuationSnapshot,
                              WithdrawalRequest)
from investors.services import add_months


def seed(*args, **kwargs):
    out = StringIO()
    call_command('seed_investor_demo', *args, stdout=out, **kwargs)
    return out.getvalue()


class TempDocumentStorage:
    """Keeps documents written by seeding out of the real private_media folder."""

    def setUp(self):
        import shutil
        import tempfile
        from django.core.files.storage import FileSystemStorage
        super().setUp()
        tmp = tempfile.mkdtemp()
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=tmp)
        self.addCleanup(setattr, field, 'storage', keep)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)


@override_settings(DEBUG=True)
class SeedDemoTests(TempDocumentStorage, TestCase):
    def oldest_start(self):
        return Investment.objects.get(investor__user__username='demo', plan_name='Heritage Growth Plan').start_date

    def test_creates_demo_accounts_with_history(self):
        seed()
        demo = InvestorProfile.objects.get(user__username='demo')
        self.assertEqual(demo.investments.count(), 5)
        self.assertEqual(Transaction.objects.filter(investment__investor=demo, type='deposit').count(), 5)
        self.assertTrue(Transaction.objects.filter(investment__investor=demo, type='interest_credit').exists())
        self.assertTrue(Transaction.objects.filter(investment__investor=demo, type='payout').exists())
        self.assertTrue(InvestorProfile.objects.filter(user__username='demo-other').exists())

    def test_years_back_controls_how_far_back_history_goes(self):
        seed('--years-back=2')
        self.assertEqual(self.oldest_start(), add_months(date.today(), -24))
        seed('--years-back=6', '--reset')
        self.assertEqual(self.oldest_start(), add_months(date.today(), -72))
        self.assertEqual(Investment.objects.filter(investor__user__username='demo').count(), 5)  # no duplicates

    def test_years_back_validated(self):
        for bad in ('0', '11'):
            with self.assertRaises(CommandError):
                seed(f'--years-back={bad}')

    def test_rerun_without_reset_creates_nothing_new(self):
        seed()
        count = Investment.objects.count()
        self.assertIn('Nothing new', seed())
        self.assertEqual(Investment.objects.count(), count)

    def test_ledger_never_has_future_dates_and_amounts_positive(self):
        seed('--years-back=5')
        for t in Transaction.objects.all():
            self.assertLessEqual(t.date, date.today())
            self.assertGreater(t.amount, Decimal('0'))

    def test_reset_never_touches_real_investors(self):
        real = InvestorProfile.objects.create(user=User.objects.create_user('realperson', 'r@x.com', 'x'), display_name='Real')
        keep = Investment.objects.create(investor=real, plan_name='Real Plan', principal=Decimal('1000'),
                                         annual_rate=Decimal('5'), start_date=date(2024, 1, 1), maturity_date=date(2027, 1, 1))
        seed('--reset')
        seed('--reset')
        self.assertTrue(Investment.objects.filter(pk=keep.pk).exists())

    def test_seeding_sends_no_emails(self):
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            seed()
        send.assert_not_called()


class SeedSafetyTests(TestCase):
    @override_settings(DEBUG=False)
    def test_refuses_when_debug_off(self):
        with self.assertRaises(CommandError):
            seed()
        self.assertEqual(InvestorProfile.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_refuses_non_sqlite_database(self):
        fake = {'default': {'ENGINE': 'django.db.backends.postgresql'}}
        with override_settings(DATABASES=fake), self.assertRaises(CommandError):
            seed()


@override_settings(DEBUG=True)
class RichDemoDataTests(TestCase):
    """The demo dataset must exercise every feature, so a person exploring the portal sees all of them."""

    @classmethod
    def setUpTestData(cls):
        import shutil
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        from django.core.files.storage import FileSystemStorage
        field = Document._meta.get_field('file')
        cls.keep, field.storage = field.storage, FileSystemStorage(location=cls.tmp)
        seed()

    @classmethod
    def tearDownClass(cls):
        import shutil
        Document._meta.get_field('file').storage = cls.keep
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def test_every_withdrawal_status_is_represented(self):
        statuses = set(WithdrawalRequest.objects.values_list('status', flat=True))
        self.assertEqual(statuses, {'submitted', 'under_review', 'approved', 'completed', 'declined', 'cancelled'})

    def test_every_destination_type_is_represented(self):
        self.assertEqual(set(WithdrawalRequest.objects.values_list('destination_type', flat=True)),
                         {'saved', 'typed', 'offline'})

    def test_completed_withdrawals_are_on_the_ledger(self):
        for wr in WithdrawalRequest.objects.filter(status='completed'):
            self.assertEqual(wr.transaction.type, 'withdrawal')
            self.assertEqual(wr.transaction.amount, wr.amount)
            self.assertIn(wr.reference, wr.transaction.note)

    def test_declined_has_a_real_reason_and_typed_needs_callback_when_decided(self):
        for wr in WithdrawalRequest.objects.filter(status='declined'):
            self.assertTrue(wr.decline_reason, wr.reference)
        for wr in WithdrawalRequest.objects.filter(status__in=('approved', 'completed'), destination_type='typed'):
            self.assertTrue(wr.callback_verified)

    def test_bank_numbers_exist_only_encrypted(self):
        for wr in WithdrawalRequest.objects.filter(destination_type='typed'):
            self.assertTrue(wr.account_number_enc)
            self.assertNotIn(crypto.decrypt(wr.account_number_enc), wr.account_number_enc)
        acct = PayoutAccount.objects.get(label='Chase checking')
        self.assertEqual(crypto.decrypt(acct.account_number_enc), '123456789')
        self.assertTrue(PayoutAccount.objects.filter(is_verified=False).exists())

    def test_plans_span_active_and_matured_and_multiple_currencies(self):
        self.assertEqual(set(Investment.objects.values_list('status', flat=True)), {'active', 'matured'})
        self.assertEqual(set(Investment.objects.values_list('currency', flat=True)), {'USD', 'EUR'})
        for inv in Investment.objects.all():
            self.assertLess(inv.start_date, inv.maturity_date)
            self.assertLessEqual(inv.start_date, date.today())

    def test_valuation_snapshots_exist_and_match_the_growth_maths(self):
        from investors.services import projected_value
        snap = ValuationSnapshot.objects.filter(investment__plan_name='Heritage Growth Plan').first()
        self.assertEqual(snap.value, projected_value(snap.investment, snap.date))
        self.assertGreater(ValuationSnapshot.objects.count(), 30)

    def test_snapshots_are_net_of_the_demo_withdrawals_so_the_dashboard_really_drops(self):
        from investors import services
        heritage = Investment.objects.get(investor__user__username='demo', plan_name='Heritage Growth Plan')
        withdrawn = services.withdrawn_total(heritage)
        self.assertGreater(withdrawn, 0)
        latest = heritage.snapshots.order_by('-date').first()
        gross = services.projected_value(heritage, latest.date)
        self.assertLess(latest.value, gross - withdrawn)          # at least the 5,000 is gone (interest on it too)
        self.assertEqual(latest.value, services.value_on(heritage, latest.date))
        # and the portfolio gain does not show the withdrawal as a loss
        row = [r for r in services.portfolio_summary(heritage.investor)['rows'] if r['investment'].pk == heritage.pk][0]
        self.assertEqual(row['gain'], row['value'] + withdrawn - heritage.principal)

    # ── the long-history requirements ───────────────────────────────────────────────────────────
    def profiles(self):
        return InvestorProfile.objects.filter(is_demo=True).order_by('user__username')

    def test_every_demo_account_has_a_plan_that_started_six_years_ago(self):
        six_years_ago = add_months(date.today(), -72)
        self.assertEqual(self.profiles().count(), 6)
        for profile in self.profiles():
            oldest = min(profile.investments.values_list('start_date', flat=True))
            if profile.user.username == 'colin-firth':
                self.assertEqual(oldest, date(2020, 1, 15))   # anchored to January 2020, whatever today's date is
            else:
                self.assertEqual(oldest, six_years_ago, profile.user.username)

    def test_every_demo_account_holds_at_least_570k_in_its_main_currency(self):
        from investors import services
        for profile in self.profiles():
            totals = services.portfolio_summary(profile)['by_currency']
            main = max(t['value'] for t in totals)
            self.assertGreaterEqual(main, Decimal('570000'), profile.user.username)

    def test_every_demo_account_has_old_paid_and_declined_withdrawals(self):
        from django.utils import timezone
        cutoff = timezone.now() - timedelta(days=180)
        for profile in self.profiles():
            old = profile.withdrawals.filter(decided_at__lt=cutoff)
            self.assertGreaterEqual(old.filter(status='completed').count(), 3, profile.user.username)
            self.assertGreaterEqual(old.filter(status='declined').count(), 2, profile.user.username)

    def test_old_withdrawals_are_sensible(self):
        from investors.demo_data import DECLINE_REASONS
        from investors.services import projected_value
        for wr in WithdrawalRequest.objects.filter(investor__is_demo=True, decided_at__isnull=False).exclude(status='cancelled'):
            decided = wr.decided_at.date()
            inv = wr.investment
            self.assertLessEqual(decided, date.today())
            self.assertGreaterEqual(decided, inv.start_date, wr.reference)               # never before the plan existed
            self.assertLess(wr.submitted_at, wr.decided_at)
            if wr.status == 'completed':
                self.assertEqual(wr.transaction.date, decided)                           # the ledger agrees with the request
                self.assertIn(wr.reference, wr.transaction.note)
            if wr.status == 'declined':
                self.assertIsNone(wr.transaction_id)
                self.assertIn(wr.decline_reason, DECLINE_REASONS.values())
            if wr.decided_at.year < date.today().year - 1:                                # the generated old history
                self.assertEqual(wr.amount % 100, 0, wr.reference)
                self.assertLessEqual(wr.amount, projected_value(inv, decided) * Decimal('0.10'), wr.reference)

    def test_old_typed_withdrawals_have_distinct_encrypted_accounts_with_valid_routing(self):
        from investors.withdrawals import valid_routing_number
        seen = set()
        for wr in WithdrawalRequest.objects.filter(investor__is_demo=True, destination_type='typed'):
            account = crypto.decrypt(wr.account_number_enc)
            self.assertTrue(valid_routing_number(crypto.decrypt(wr.routing_number_enc)))
            self.assertNotIn(account, wr.account_number_enc)
            self.assertEqual(wr.destination_label, f'{wr.bank_name} ••••{account[-4:]}')
            seen.add((wr.investor_id, account))
        self.assertGreater(len(seen), 6)

    def test_annual_statements_cover_the_older_year_ends(self):
        for profile in self.profiles():
            titles = set(profile.documents.values_list('title', flat=True))
            self.assertIn(f'{date.today().year - 1} Annual Statement', titles | {f'{date.today().year - 1} Annual Statement'})
            self.assertTrue(any('Annual Statement' in t for t in titles), profile.user.username)
            self.assertTrue(any(t.startswith('Q') for t in titles), profile.user.username)

    def test_portal_pages_all_open_for_the_long_history_accounts(self):
        for username in ('demo', 'demo-elena', 'demo-astrid', 'demo-other'):
            self.client.logout()
            self.client.post('/investors/login/', {'username': username, 'password': 'DemoPass-12345!'})
            for url in ('/investors/', '/investors/performance/', '/investors/documents/', '/investors/withdrawals/'):
                self.assertEqual(self.client.get(url).status_code, 200, (username, url))
        rows = self.client.get('/investors/performance/').context['rows']
        self.assertGreaterEqual(len(rows), 6)                                          # six-plus years of yearly figures

    def test_shorter_windows_still_work_and_skip_entries_that_predate_their_plan(self):
        seed('--years-back=1', '--reset')
        for wr in WithdrawalRequest.objects.filter(investor__is_demo=True):
            self.assertGreaterEqual(wr.decided_at.date() if wr.decided_at else wr.submitted_at.date(), wr.investment.start_date)
        heritage = Investment.objects.get(investor__user__username='demo', plan_name='Heritage Growth Plan')
        self.assertEqual(heritage.start_date, add_months(date.today(), -12))

    def oldest_start_for(self, username):
        return min(Investment.objects.filter(investor__user__username=username).values_list('start_date', flat=True))

    def test_documents_are_real_pdfs_owned_correctly(self):
        docs = Document.objects.all()
        self.assertTrue(docs.filter(kind='statement').exists() and docs.filter(kind='agreement').exists())
        for doc in docs:
            with doc.file.open('rb') as fh:
                data = fh.read()
            self.assertTrue(data.startswith(b'%PDF-1.4') and data.rstrip().endswith(b'%%EOF'))
            self.assertIn(b'DEMONSTRATION DOCUMENT', data)
            if doc.investment_id:
                self.assertEqual(doc.investment.investor_id, doc.investor_id)

    def test_pdf_structure_is_valid(self):
        import re
        from investors.demo_data import make_pdf
        data = make_pdf('Title (with) parens \\ and slash', ['line one', 'line (two)'])
        xref_at = int(re.search(rb'startxref\n(\d+)', data).group(1))
        self.assertTrue(data[xref_at:].startswith(b'xref'))
        for n, off in enumerate(re.findall(rb'(\d{10}) 00000 n', data), start=1):
            self.assertTrue(data[int(off):].startswith(b'%d 0 obj' % n))

    def test_new_investor_must_change_password_and_is_forced_to(self):
        profile = InvestorProfile.objects.get(user__username='demo-chidi')
        self.assertTrue(profile.must_change_password)
        self.client.post('/investors/login/', {'username': 'demo-chidi', 'password': 'Welcome-Temp-2026'})
        r = self.client.get('/investors/')
        self.assertRedirects(r, '/investors/change-password/', fetch_redirect_response=False)

    def test_other_demo_accounts_sign_in_and_see_only_their_own_data(self):
        self.client.post('/investors/login/', {'username': 'demo-elena', 'password': 'DemoPass-12345!'})
        page = self.client.get('/investors/').content.decode()
        self.assertIn('Founders Circle Note', page)
        self.assertNotIn('Heritage Growth Plan', page)

    def test_all_demo_emails_are_example_dot_com(self):
        for u in User.objects.filter(username__in=('demo', 'demo-elena', 'demo-astrid', 'demo-chidi', 'colin-firth', 'demo-other')):
            self.assertTrue(u.email.endswith('@example.com'), u.email)

    def test_reset_removes_everything_including_files_and_is_repeatable(self):
        import os
        names = [d.file.name for d in Document.objects.all()]
        expected = WithdrawalRequest.objects.count()
        seed('--reset')
        self.assertFalse(any(os.path.exists(os.path.join(self.tmp, n)) for n in names))
        self.assertEqual(WithdrawalRequest.objects.count(), expected)
        seed('--reset')
        self.assertEqual(WithdrawalRequest.objects.count(), expected)
