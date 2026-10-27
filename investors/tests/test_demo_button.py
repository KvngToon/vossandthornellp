import importlib
import os
import re
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings
from django.urls import reverse

from investors.demo_data import DEMO_USERNAMES, PASSWORD, SEEDED, DemoSeeder

SEEDED_NAMES = [spec['username'] for spec in SEEDED]
from investors.models import (Document, Investment, InvestorProfile, PayoutAccount, Transaction, WithdrawalRequest)

PW = 'correct-horse-battery-9'
URL_NAME = 'admin:investors_investorprofile_demo_data'
CREATE, REMOVE = 'CREATE DEMO DATA', 'REMOVE DEMO DATA'
SECRET = 'origin-secret-for-tests'
KEY = Fernet.generate_key().decode()
US = {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'US'}


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class ButtonBase(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=self.tmp)
        self.addCleanup(setattr, field, 'storage', keep)
        self.url = reverse(URL_NAME)
        self.root = User.objects.create_superuser('root', 'root@corp.com', PW)
        self.client.force_login(self.root)

    def create(self, phrase=CREATE, years='3'):
        return self.client.post(self.url, {'action': 'create', 'confirm': phrase, 'years': years})

    def remove(self, phrase=REMOVE):
        return self.client.post(self.url, {'action': 'remove', 'confirm': phrase})

    def real_client(self, username='realclient', email='real@client.com', demo=False):
        user = User.objects.create_user(username, email, 'their-own-password-1')
        profile = InvestorProfile.objects.create(user=user, display_name='Real Client', is_demo=demo)
        inv = Investment.objects.create(investor=profile, plan_name='Real Plan', principal=Decimal('75000'),
                                        annual_rate=Decimal('9'), start_date=date(2024, 1, 1), maturity_date=date(2028, 1, 1))
        doc = Document.objects.create(investor=profile, title='Real statement', file=ContentFile(b'%PDF-real', name='r.pdf'))
        return user, profile, inv, doc


class AccessTests(ButtonBase):
    def test_link_shown_to_superusers_only(self):
        changelist = reverse('admin:investors_investorprofile_changelist')
        self.assertContains(self.client.get(changelist), 'Demo data…')
        staff = User.objects.create_user('clerk', 'c@corp.com', PW, is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label='investors'))
        self.client.force_login(staff)
        self.assertNotContains(self.client.get(changelist), 'Demo data…')

    def test_regular_staff_cannot_open_or_use_it_even_with_every_investor_permission(self):
        staff = User.objects.create_user('clerk', 'c@corp.com', PW, is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label='investors'))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.remove().status_code, 403)
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_anonymous_and_investors_are_turned_away(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        user = User.objects.create_user('inv', 'i@x.com', PW)
        InvestorProfile.objects.create(user=user, display_name='I')
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 302)  # not staff: bounced to the admin login


class CreateTests(ButtonBase):
    @override_settings(FIELD_ENCRYPTION_KEY='', DEBUG=False)
    def test_missing_encryption_key_gives_a_clear_message_and_creates_nothing(self):
        r = self.create()
        self.assertContains(r, 'Nothing was created')
        self.assertContains(r, 'FIELD_ENCRYPTION_KEY')
        self.assertEqual(InvestorProfile.objects.count(), 0)
        self.assertEqual(User.objects.filter(username__in=DEMO_USERNAMES).count(), 0)

    def test_requires_the_exact_phrase_and_valid_years(self):
        for phrase in ('', 'create demo data', 'CREATE DEMO', 'yes'):
            self.assertContains(self.create(phrase=phrase), 'exactly to confirm')
        self.assertContains(self.create(years='0'), 'between 1 and 10')
        self.assertContains(self.create(years='abc'), 'between 1 and 10')
        self.assertContains(self.create(years='11'), 'between 1 and 10')
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_creates_flagged_demo_accounts_with_one_time_random_passwords(self):
        r = self.create()
        self.assertContains(r, 'Demo data ready')
        self.assertRegex(r.content.decode(), r'created in [0-9.]+s')
        self.assertIn('no-store', r['Cache-Control'])
        self.assertEqual(set(InvestorProfile.objects.filter(is_demo=True).values_list('user__username', flat=True)), set(SEEDED_NAMES))
        shown = dict(re.findall(r'<td><code>([\w-]+)</code></td><td>[^<]+</td><td><code>([^<]+)</code></td>', r.content.decode()))
        self.assertEqual(set(shown), set(SEEDED_NAMES))
        self.assertEqual(len(set(shown.values())), len(shown))          # all different
        self.assertNotIn(PASSWORD, shown.values())                       # never the well-known local password
        self.assertNotIn(PASSWORD, r.content.decode())
        for username, password in shown.items():                         # the displayed passwords really work
            self.assertTrue(User.objects.get(username=username).check_password(password), username)
            self.assertNotIn(password, User.objects.get(username=username).password)  # stored hashed only

    def test_demo_accounts_are_never_staff_and_chidi_must_change_password(self):
        self.create()
        for user in User.objects.filter(username__in=SEEDED_NAMES):
            self.assertFalse(user.is_staff or user.is_superuser)
            self.assertTrue(user.email.endswith('@example.com'))
        self.assertTrue(InvestorProfile.objects.get(user__username='demo-chidi').must_change_password)

    def test_a_slow_database_gives_a_clear_message_instead_of_a_crash(self):
        with override_settings(DEMO_SEED_MAX_SECONDS=-1):
            r = self.create()
        self.assertContains(r, '--timeout 90')
        self.assertContains(r, 'nothing was created')
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_form_defaults_to_six_years(self):
        self.assertContains(self.client.get(self.url), 'name="years" value="6"')

    def test_full_dataset_present_and_action_is_logged(self):
        self.create(years='2')
        self.assertEqual(InvestorProfile.objects.filter(is_demo=True).count(), len(SEEDED_NAMES))
        self.assertGreaterEqual(WithdrawalRequest.objects.count(), 7)  # recent ones plus years of old history
        self.assertEqual(Document.objects.count(), 0)                 # no PDFs
        self.assertEqual(PayoutAccount.objects.count(), 3)            # demo x2, colin
        entry = LogEntry.objects.get(object_repr='Demo data')
        self.assertEqual(entry.user, self.root)
        self.assertIn('Created demo data', entry.change_message)

    def test_sends_no_emails(self):
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            self.create()
        send.assert_not_called()

    def test_running_again_resets_passwords_without_duplicating(self):
        first = self.create().content.decode()
        count = Investment.objects.count()
        old = re.search(r'<code>demo</code></td><td>[^<]+</td><td><code>([^<]+)</code>', first).group(1)
        self.create()
        self.assertEqual(Investment.objects.count(), count)
        self.assertFalse(User.objects.get(username='demo').check_password(old))


class CollisionSafetyTests(ButtonBase):
    def test_real_account_using_a_demo_username_is_never_touched_and_nothing_is_created(self):
        user, profile, inv, doc = self.real_client(username='demo-chidi', email='real@client.com')
        r = self.create()
        self.assertContains(r, 'already belongs to a real account')
        self.assertFalse(InvestorProfile.objects.filter(is_demo=True).exists())  # all-or-nothing: no partial set left behind
        user.refresh_from_db()
        self.assertTrue(user.check_password('their-own-password-1'))
        self.assertEqual(Investment.objects.count(), 1)

    def test_real_account_using_a_demo_email_is_refused(self):
        User.objects.create_user('someone', 'demo.investor@example.com', 'x-password-123')  # no profile at all
        self.assertContains(self.create(), 'already belongs to a real account')
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_real_investor_with_demo_username_and_example_email_but_unflagged_is_refused(self):
        self.real_client(username='demo', email='demo.investor@example.com', demo=False)
        self.assertContains(self.create(), 'already belongs to a real account')

    def test_staff_user_with_a_demo_name_is_never_demoted_or_changed(self):
        staff = User.objects.create_user('demo', 'demo.investor@example.com', 'x-password-123', is_staff=True)
        self.assertContains(self.create(), 'already belongs to a real account')
        staff.refresh_from_db()
        self.assertTrue(staff.is_staff)
        self.assertTrue(staff.check_password('x-password-123'))


class RemoveTests(ButtonBase):
    def setUp(self):
        super().setUp()
        self.create()

    def test_requires_the_exact_phrase(self):
        for phrase in ('', 'remove demo data', 'REMOVE'):
            self.assertContains(self.remove(phrase=phrase), 'exactly to confirm')
        self.assertEqual(InvestorProfile.objects.filter(is_demo=True).count(), len(SEEDED_NAMES))

    def test_removes_all_demo_data_files_and_accounts(self):
        with mock.patch('investors.demo_data.INCLUDE_DOCUMENTS', True):
            DemoSeeder().run(reset=True)   # this test is about stored PDFs being cleaned up as well
        files = [d.file.name for d in Document.objects.all()]
        self.assertTrue(files and all(os.path.exists(os.path.join(self.tmp, f)) for f in files))
        r = self.remove()
        self.assertContains(r, f'Removed {len(SEEDED_NAMES)} demo investors')
        for model in (InvestorProfile, Investment, Transaction, Document, PayoutAccount, WithdrawalRequest):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertFalse(User.objects.filter(username__in=DEMO_USERNAMES).exists())
        self.assertFalse(any(os.path.exists(os.path.join(self.tmp, f)) for f in files))
        self.assertTrue(LogEntry.objects.filter(change_message__contains='Removed demo data').exists())

    def test_real_clients_survive_removal_including_lookalikes(self):
        real, profile, inv, doc = self.real_client()
        lookalike, lprofile, linv, ldoc = self.real_client(username='demo-elena-fan', email='fan@example.com', demo=True)
        legacy_named, nprofile, ninv, ndoc = self.real_client(username='elena', email='elena@gmail.com', demo=False)
        self.remove()
        for obj in (real, lookalike, legacy_named, profile, lprofile, nprofile, inv, linv, ninv, doc, ldoc, ndoc):
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists(), obj)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, doc.file.name)))
        self.assertTrue(User.objects.get(username='realclient').check_password('their-own-password-1'))

    def test_flag_alone_is_not_enough_a_flagged_non_demo_name_is_kept(self):
        _, profile, _, _ = self.real_client(username='acme-director', email='d@example.com', demo=True)
        DemoSeeder().remove()
        self.assertTrue(InvestorProfile.objects.filter(pk=profile.pk).exists())

    def test_removing_when_nothing_exists_is_harmless(self):
        self.remove()
        self.assertContains(self.remove(), 'Removed 0 demo investors')
        self.assertContains(self.client.get(self.url), 'There is no demo data right now')

    def test_removal_is_total_so_the_names_are_free_to_reuse(self):
        self.remove()
        self.create()
        self.assertEqual(InvestorProfile.objects.filter(is_demo=True).count(), len(SEEDED_NAMES))

    def test_accounts_created_by_earlier_versions_are_still_removable(self):
        old_user = User.objects.create_user('demo-elena', 'elena.marsh@example.com', 'x-password-123')
        old = InvestorProfile.objects.create(user=old_user, display_name='Elena Marsh', is_demo=True)
        self.remove()
        self.assertFalse(InvestorProfile.objects.filter(pk=old.pk).exists())


@override_settings(CF_ORIGIN_SECRET=SECRET, FIELD_ENCRYPTION_KEY=KEY)
class DemoAccountsNeverEmailTests(ButtonBase):
    def setUp(self):
        super().setUp()
        self.create()
        self.demo = InvestorProfile.objects.get(user__username='demo')

    def test_banner_shown_for_demo_accounts_only(self):
        self.client.logout()
        password = 'a-known-pass-123'
        self.demo.user.set_password(password)
        self.demo.user.save()
        self.client.post(reverse('investors:login'), {'username': 'demo', 'password': password})
        self.assertContains(self.client.get(reverse('investors:home')), 'Demonstration account')
        self.client.logout()
        user, profile, inv, doc = self.real_client()
        self.client.post(reverse('investors:login'), {'username': 'realclient', 'password': 'their-own-password-1'})
        self.assertNotContains(self.client.get(reverse('investors:home')), 'Demonstration account')

    def test_new_records_for_a_demo_investor_send_nothing(self):
        inv = self.demo.investments.first()
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            Transaction.objects.create(investment=inv, type='payout', amount=Decimal('10'), date=date.today())
            Document.objects.create(investor=self.demo, title='New', file=ContentFile(b'x', name='n.pdf'))
        send.assert_not_called()

    def test_demo_withdrawal_request_and_staff_update_send_nothing(self):
        self.client.logout()
        self.demo.user.set_password('a-known-pass-123')
        self.demo.user.save()
        self.client.post(reverse('investors:login'), {'username': 'demo', 'password': 'a-known-pass-123'})
        inv = self.demo.investments.get(plan_name='Heritage Growth Plan')
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse('investors:withdrawal_new'), {
                'investment': inv.pk, 'amount': '100', 'destination': 'offline', 'password': 'a-known-pass-123', 'confirm': 'on'}, **US)
            self.assertEqual(r.status_code, 302)
            wr = WithdrawalRequest.objects.latest('id')
            clerk = User.objects.create_user('clerk', 'clerk@corp.com', PW, is_staff=True)  # ordinary staff, not a superuser
            clerk.user_permissions.add(*Permission.objects.filter(codename__in=['view_withdrawalrequest', 'change_withdrawalrequest']))
            self.client.force_login(clerk)
            self.client.post(reverse('admin:investors_withdrawalrequest_change', args=[wr.pk]),
                             {'status': 'under_review', 'decline_reason': '', 'staff_note': ''})
        wr.refresh_from_db()
        self.assertEqual(wr.status, 'under_review')
        send.assert_not_called()


class BackfillMigrationTests(TestCase):
    def test_only_exact_legacy_demo_accounts_get_flagged(self):
        migration = importlib.import_module('investors.migrations.0006_investorprofile_is_demo')
        from django.apps import apps

        def make(username, email):
            return InvestorProfile.objects.create(user=User.objects.create_user(username, email, 'x-password-123'), display_name=username)

        legacy = make('elena', 'elena.marsh@example.com')
        local = make('demo', 'demo.investor@example.com')
        real_same_name = make('other', 'other@realbank.com')
        real_other = make('someone', 'someone@example.com')
        migration.flag_existing_demo_accounts(apps, None)
        for p in (legacy, local, real_same_name, real_other):
            p.refresh_from_db()
        self.assertTrue(legacy.is_demo and local.is_demo)
        self.assertFalse(real_same_name.is_demo)   # right username, wrong email domain
        self.assertFalse(real_other.is_demo)       # right domain, wrong username
