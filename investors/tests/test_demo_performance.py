"""Demo seeding runs inside one web request. Against a network database every query costs a round trip,
and gunicorn kills a worker after its timeout, so the seeder must stay query-light and upload files in
parallel. (An early version made ~1,700 queries and timed out in production.)"""
import shutil
import tempfile
import threading
from unittest import mock

from cryptography.fernet import Fernet
from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from investors import demo_data
from investors.demo_data import DemoSeeder, _in_parallel
from investors.models import Document, Investment, InvestorProfile, ValuationSnapshot

KEY = Fernet.generate_key().decode()


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class SeedingStaysQueryLightTests(TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=tmp)
        self.addCleanup(setattr, field, 'storage', keep)

    def test_full_seeding_stays_under_a_query_budget(self):
        with CaptureQueriesContext(connection) as ctx:
            DemoSeeder(random_passwords=True).run()
        # Fail loudly well before it gets slow again.
        self.assertLess(len(ctx), 150, f'seeding now runs {len(ctx)} queries')  # ~90 today; the slow version made ~1,700

    def test_rebuild_stays_under_a_query_budget_too(self):
        DemoSeeder().run()
        with CaptureQueriesContext(connection) as ctx:
            DemoSeeder(random_passwords=True).run(rebuild_custom=True)
        self.assertLess(len(ctx), 200, f'rebuild now runs {len(ctx)} queries')

    def test_monthly_valuations_are_computed_without_rereading_the_database(self):
        DemoSeeder().run()
        inv = Investment.objects.get(investor__user__username='colin-firth', plan_name='Firth Family Growth Plan')
        ValuationSnapshot.objects.filter(investment=inv).delete()
        with CaptureQueriesContext(connection) as ctx:
            DemoSeeder().make_snapshots(inv)
        self.assertGreater(ValuationSnapshot.objects.filter(investment=inv).count(), 60)
        self.assertLessEqual(len(ctx), 6)   # one fetch + prefetches + one bulk insert, not ~4 queries per month

    def test_snapshot_values_are_unchanged_by_the_optimisation(self):
        from investors import services
        DemoSeeder().run()
        inv = Investment.objects.prefetch_related('snapshots', 'transactions').get(
            investor__user__username='demo', plan_name='Heritage Growth Plan')
        for snap in inv.snapshots.all():
            # value_on prefers a posted snapshot, so compare with the pure computed path instead
            fresh = Investment.objects.get(pk=inv.pk)
            expected = services.projected_value(fresh, snap.date) * services._remaining_fraction(fresh, snap.date)
            self.assertAlmostEqual(float(snap.value), float(expected), delta=0.01)


class ParallelHelperTests(TestCase):
    def test_runs_everything_and_keeps_order(self):
        self.assertEqual(_in_parallel(lambda n: n * 2, range(25)), [n * 2 for n in range(25)])

    def test_really_uses_several_threads(self):
        seen = set()
        barrier = threading.Barrier(4, timeout=5)

        def work(_):
            seen.add(threading.get_ident())
            barrier.wait()          # only passes if four calls are in flight at once

        _in_parallel(work, range(4))
        self.assertGreaterEqual(len(seen), 4)

    def test_errors_propagate_by_default(self):
        def boom(n):
            if n == 3:
                raise RuntimeError('upload failed')
        with self.assertRaises(RuntimeError):
            _in_parallel(boom, range(6))

    def test_cleanup_mode_logs_and_keeps_going(self):
        done = []

        def fn(n):
            if n == 2:
                raise RuntimeError('delete failed')
            done.append(n)

        with self.assertLogs('investors.demo_data', level='ERROR') as logs:
            _in_parallel(fn, range(6), swallow='Could not delete a stored demo file')
        self.assertEqual(sorted(done), [0, 1, 3, 4, 5])
        self.assertTrue(any('Could not delete' in line for line in logs.output))


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class FileCleanupTests(TestCase):
    def setUp(self):
        patcher = mock.patch.object(demo_data, 'INCLUDE_DOCUMENTS', True)   # these tests are about stored files
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=self.tmp)
        self.addCleanup(setattr, field, 'storage', keep)
        DemoSeeder().run()

    def test_each_stored_file_is_deleted_exactly_once(self):
        count = Document.objects.count()
        with mock.patch.object(FileSystemStorage, 'delete', autospec=True, side_effect=FileSystemStorage.delete) as delete, \
                self.captureOnCommitCallbacks(execute=True):
            DemoSeeder().clear_data()
        self.assertEqual(delete.call_count, count)     # not doubled by the document post_delete signal
        self.assertEqual(Document.objects.count(), 0)

    def test_the_delete_signal_is_reconnected_afterwards(self):
        import os
        DemoSeeder().clear_data()
        DemoSeeder().run()
        doc = Document.objects.first()
        path = os.path.join(self.tmp, doc.file.name)
        self.assertTrue(os.path.exists(path))
        with self.captureOnCommitCallbacks(execute=True):
            doc.delete()
        self.assertFalse(os.path.exists(path))          # normal deletions still clean their file up

    def test_a_failing_file_delete_does_not_stop_the_cleanup(self):
        real = FileSystemStorage.delete
        calls = {'n': 0}

        def flaky(self_, name):
            calls['n'] += 1
            if calls['n'] == 3:
                raise OSError('storage hiccup')
            return real(self_, name)

        with mock.patch.object(FileSystemStorage, 'delete', autospec=True, side_effect=flaky):
            DemoSeeder().clear_data()
        self.assertEqual(Document.objects.count(), 0)   # rows are all gone even though one file failed


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class BatchedInsertTests(TestCase):
    """Withdrawals and their ledger rows are inserted in batches with pre-checked reference numbers."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=tmp)
        self.addCleanup(setattr, field, 'storage', keep)

    def test_references_are_unique_and_never_collide_with_existing_ones(self):
        from investors.models import WithdrawalRequest
        DemoSeeder().run()
        existing = set(Investment.objects.values_list('reference', flat=True)) | set(WithdrawalRequest.objects.values_list('reference', flat=True))
        DemoSeeder().run(rebuild_custom=True)
        refs = list(WithdrawalRequest.objects.values_list('reference', flat=True))
        self.assertEqual(len(refs), len(set(refs)))
        self.assertTrue(all(r.startswith('WD-') for r in refs))
        self.assertEqual(len(existing), len(existing))
        invs = list(Investment.objects.values_list('reference', flat=True))
        self.assertEqual(len(invs), len(set(invs)))

    def test_every_paid_withdrawal_points_at_exactly_its_own_ledger_row(self):
        from investors.models import Transaction, WithdrawalRequest
        DemoSeeder().run()
        paid = WithdrawalRequest.objects.filter(status='completed')
        self.assertTrue(paid.exists())
        for wr in paid:
            self.assertEqual((wr.transaction.type, wr.transaction.amount, wr.transaction.investment_id),
                             ('withdrawal', wr.amount, wr.investment_id))
            self.assertEqual(wr.transaction.note, f'Withdrawal {wr.reference}')
        self.assertEqual(Transaction.objects.filter(type='withdrawal').count(), paid.count())  # no stray ledger rows

    def test_reference_collisions_are_avoided(self):
        from investors.models import WithdrawalRequest
        seeder = DemoSeeder()
        seeder._used_refs['wr'] = set()
        picks = iter(['WD-2026-AAAAAA', 'WD-2026-AAAAAA', 'WD-2026-BBBBBB'])
        with mock.patch.object(demo_data, '_withdrawal_reference', side_effect=lambda: next(picks)):
            first, second = seeder._reference('wr'), seeder._reference('wr')
        self.assertEqual((first, second), ('WD-2026-AAAAAA', 'WD-2026-BBBBBB'))   # the duplicate was skipped


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class TimeGuardTests(TestCase):
    def test_a_run_that_takes_too_long_stops_cleanly_and_creates_nothing(self):
        from investors.demo_data import DemoDataError
        with override_settings(DEMO_SEED_MAX_SECONDS=-1):   # already out of time
            with self.assertRaises(DemoDataError) as ctx:
                DemoSeeder().run()
        self.assertIn('--timeout 90', str(ctx.exception))
        self.assertIn('nothing was created', str(ctx.exception))
        self.assertFalse(InvestorProfile.objects.exists())                      # rolled back, no half-built accounts

    def test_the_elapsed_time_is_recorded(self):
        seeder = DemoSeeder()
        with mock.patch.object(demo_data.DemoSeeder, 'seed_investor', return_value=0):
            seeder.run()
        self.assertGreaterEqual(seeder.elapsed, 0)
