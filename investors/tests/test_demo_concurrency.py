"""Two demo-data runs at the same moment (a double click, two tabs) used to race: both created the same
user and one died with a duplicate-username 500 in production. They are now serialised and any collision
is reported on the page."""
import shutil
import tempfile
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse

from investors import demo_data
from investors.demo_data import DEMO_LOCK_ID, DemoDataError, DemoSeeder
from investors.models import Document, InvestorProfile

PW = 'correct-horse-battery-9'
KEY = Fernet.generate_key().decode()
URL = reverse('admin:investors_investorprofile_demo_data')


def fake_postgres(acquired):
    """A stand-in for django.db.connection that reports PostgreSQL and a given lock outcome."""
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = (acquired,)
    conn = mock.MagicMock(vendor='postgresql')
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class Base(TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=tmp)
        self.addCleanup(setattr, field, 'storage', keep)


class AdvisoryLockTests(Base):
    def test_a_second_run_is_turned_away_when_the_lock_is_taken(self):
        conn, cursor = fake_postgres(acquired=False)
        with mock.patch.object(demo_data, 'connection', conn):
            with self.assertRaises(DemoDataError) as ctx:
                DemoSeeder().run()
        self.assertIn('already in progress', str(ctx.exception))
        self.assertFalse(InvestorProfile.objects.exists())                       # nothing was created
        self.assertEqual(cursor.execute.call_args.args, ('SELECT pg_try_advisory_xact_lock(%s)', [DEMO_LOCK_ID]))

    def test_the_run_proceeds_normally_when_the_lock_is_free(self):
        conn, cursor = fake_postgres(acquired=True)
        with mock.patch.object(demo_data, 'connection', conn):
            DemoSeeder().run()
        self.assertTrue(InvestorProfile.objects.filter(user__username='colin-firth').exists())
        cursor.execute.assert_called_once()                                      # asked for the lock exactly once

    def test_every_writer_takes_the_same_lock(self):
        DemoSeeder().run()
        profile = InvestorProfile.objects.get(user__username='demo')
        before = InvestorProfile.objects.count()
        for action in (lambda: DemoSeeder().remove(),
                       lambda: DemoSeeder().fill_profile(profile, template='demo', replace=True),
                       lambda: DemoSeeder().run(rebuild_custom=True)):
            conn, _ = fake_postgres(acquired=False)
            with mock.patch.object(demo_data, 'connection', conn):
                with self.assertRaises(DemoDataError):
                    action()
        self.assertEqual(InvestorProfile.objects.count(), before)                # all three refused, nothing touched
        self.assertTrue(User.objects.filter(username='demo').exists())

    def test_no_lock_query_is_issued_on_sqlite(self):
        from django.db import connection as real
        self.assertNotEqual(real.vendor, 'postgresql')                           # the test database
        with mock.patch.object(real, 'cursor', wraps=real.cursor) as cursor:
            DemoSeeder()._take_lock()
        cursor.assert_not_called()

    def test_the_lock_is_taken_before_any_work_so_a_refusal_leaves_no_trace(self):
        conn, _ = fake_postgres(acquired=False)
        with mock.patch.object(demo_data, 'connection', conn), mock.patch.object(DemoSeeder, 'seed_investor') as seed:
            with self.assertRaises(DemoDataError):
                DemoSeeder().run()
        seed.assert_not_called()


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class AdminPageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser('root', 'r@corp.com', PW))

    def create(self):
        return self.client.post(URL, {'action': 'create', 'confirm': 'CREATE DEMO DATA', 'years': '6'})

    def test_a_duplicate_key_collision_becomes_a_message_not_a_500(self):
        with mock.patch.object(DemoSeeder, 'run', side_effect=IntegrityError('duplicate key value violates unique constraint')):
            r = self.create()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Nothing was created because another change happened at the same moment')
        self.assertContains(r, 'click only once')

    def test_a_run_already_in_progress_is_explained_on_the_page(self):
        conn, _ = fake_postgres(acquired=False)
        with mock.patch.object(demo_data, 'connection', conn):
            r = self.create()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'already in progress')
        self.assertFalse(InvestorProfile.objects.exists())

    def test_removal_while_a_run_is_in_progress_is_also_refused_politely(self):
        self.create()
        conn, _ = fake_postgres(acquired=False)
        with mock.patch.object(demo_data, 'connection', conn):
            r = self.client.post(URL, {'action': 'remove', 'confirm': 'REMOVE DEMO DATA'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'already in progress')
        self.assertTrue(InvestorProfile.objects.filter(user__username='demo').exists())

    def test_the_forms_disable_their_button_on_first_click(self):
        self.create()                                                            # so the remove form is shown too
        page = self.client.get(URL).content.decode()
        self.assertGreaterEqual(page.count("b.disabled=true"), 2)                # create + remove
        self.assertIn('Working', page)

    def test_the_fill_action_page_disables_its_button_too(self):
        self.create()
        profile = InvestorProfile.objects.get(user__username='demo')
        r = self.client.post(reverse('admin:investors_investorprofile_changelist'),
                             {'action': 'fill_with_demo_history', '_selected_action': [profile.pk]})
        self.assertContains(r, 'b.disabled=true')

    def test_running_twice_in_a_row_is_safe_and_does_not_duplicate_anything(self):
        self.create()
        first = (User.objects.filter(username='demo').count(), InvestorProfile.objects.count())
        r = self.create()                                                        # the second click, after the first finished
        self.assertContains(r, 'Demo data ready')
        self.assertEqual((User.objects.filter(username='demo').count(), InvestorProfile.objects.count()), first)
