from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import shutil
import tempfile

from investors.models import Document, Investment, InvestorProfile, NotificationLog, Transaction, ValuationSnapshot

PW = 'correct-horse-battery-9'


class NotificationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep = field.storage
        field.storage = FileSystemStorage(location=self.tmp)
        self.addCleanup(setattr, field, 'storage', keep)

        self.today = timezone.localdate()
        self.user = User.objects.create_user('jane@example.com', 'jane@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=self.user, display_name='Jane')
        with self.captureOnCommitCallbacks(execute=False):
            self.inv = Investment.objects.create(
                investor=self.profile, plan_name='Secret Plan Name', principal=Decimal('123456.78'),
                annual_rate=Decimal('9'), start_date=self.today - timedelta(days=400),
                maturity_date=self.today + timedelta(days=700))
        patcher = mock.patch('investors.emails._send', return_value=True)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self, days_ago=0, value='130000.00'):
        with self.captureOnCommitCallbacks(execute=True):
            return ValuationSnapshot.objects.create(
                investment=self.inv, date=self.today - timedelta(days=days_ago), value=Decimal(value))

    def test_new_snapshot_sends_one_generic_email(self):
        self.snapshot()
        self.assertEqual(self.send.call_count, 1)
        to, subject, html = self.send.call_args.args
        self.assertEqual(to, 'jane@example.com')
        self.assertIn('/investors/', html)

    def test_email_contains_no_financial_details(self):
        self.snapshot(value='130000.00')
        with self.captureOnCommitCallbacks(execute=True):
            Transaction.objects.create(investment=self.inv, type='payout', amount=Decimal('4321.00'), date=self.today)
        for call in self.send.call_args_list:
            body = ' '.join(str(a) for a in call.args)
            for secret in ('130,000', '130000', '123456', '123,456', '4321', '4,321', 'Secret Plan Name'):
                self.assertNotIn(secret, body)

    def test_cooldown_batches_burst_into_one_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            for i in range(5):
                ValuationSnapshot.objects.create(
                    investment=self.inv, date=self.today - timedelta(days=i), value=Decimal('1000') + i)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(NotificationLog.objects.filter(success=True).count(), 1)

    def test_kinds_have_independent_cooldowns(self):
        self.snapshot()
        with self.captureOnCommitCallbacks(execute=True):
            Transaction.objects.create(investment=self.inv, type='payout', amount=Decimal('10'), date=self.today)
        self.assertEqual(self.send.call_count, 2)

    def test_cooldown_expires(self):
        self.snapshot()
        NotificationLog.objects.update(sent_at=timezone.now() - timedelta(hours=1))
        self.snapshot(days_ago=1)
        self.assertEqual(self.send.call_count, 2)

    def test_backdated_history_does_not_notify(self):
        self.snapshot(days_ago=200)
        with self.captureOnCommitCallbacks(execute=True):
            Transaction.objects.create(investment=self.inv, type='deposit', amount=Decimal('10'),
                                       date=self.today - timedelta(days=300))
        self.send.assert_not_called()

    def test_editing_existing_record_does_not_notify(self):
        snap = self.snapshot()
        self.send.reset_mock()
        NotificationLog.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            snap.value = Decimal('999.00')
            snap.save()
        self.send.assert_not_called()

    def test_failed_send_is_logged_and_does_not_start_cooldown(self):
        self.send.return_value = False
        self.snapshot()
        self.assertEqual(NotificationLog.objects.filter(success=False).count(), 1)
        self.send.return_value = True
        self.snapshot(days_ago=1)
        self.assertEqual(NotificationLog.objects.filter(success=True).count(), 1)

    def test_send_exception_never_breaks_the_save(self):
        self.send.side_effect = RuntimeError('resend down')
        snap = self.snapshot()  # must not raise
        self.assertIsNotNone(snap.pk)

    def test_inactive_investor_not_notified(self):
        self.profile.is_active = False
        self.profile.save()
        self.snapshot()
        self.send.assert_not_called()

    def test_document_notifies_with_documents_link(self):
        with self.captureOnCommitCallbacks(execute=True):
            Document.objects.create(investor=self.profile, title='Q3 Statement',
                                    file=ContentFile(b'x', name='a.pdf'))
        to, subject, html = self.send.call_args.args
        self.assertIn('document', subject)
        self.assertIn('/investors/documents/', html)
        self.assertNotIn('Q3 Statement', html)

    def test_admin_action_sends_regardless_of_cooldown(self):
        self.snapshot()
        admin = User.objects.create_superuser('root', 'root@example.com', PW)
        self.client.force_login(admin)
        r = self.client.post(reverse('admin:investors_investorprofile_changelist'), {
            'action': 'send_update_email', '_selected_action': [self.profile.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(NotificationLog.objects.filter(kind='manual', success=True).exists())
