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
        # ~350 today; the slow version made ~1,700. Fail loudly well before it gets slow again.
        self.assertLess(len(ctx), 600, f'seeding now runs {len(ctx)} queries')

    def test_rebuild_stays_under_a_query_budget_too(self):
        DemoSeeder().run()
        with CaptureQueriesContext(connection) as ctx:
            DemoSeeder(random_passwords=True).run(rebuild_custom=True)
        self.assertLess(len(ctx), 700, f'rebuild now runs {len(ctx)} queries')

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
