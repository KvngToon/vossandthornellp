import os
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase
from django.urls import reverse

from investors.models import Document, Investment, InvestorProfile

PW = 'correct-horse-battery-9'


class SecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('jane@example.com', 'jane@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=self.user, display_name='Jane')

    def login(self):
        self.client.post(reverse('investors:login'), {'username': 'jane@example.com', 'password': PW})

    def test_portal_pages_are_not_cacheable_or_indexable(self):
        self.login()
        for name in ('home', 'performance', 'documents'):
            r = self.client.get(reverse(f'investors:{name}'))
            cc = r['Cache-Control']
            self.assertIn('no-store', cc, name)
            self.assertIn('private', cc, name)
            self.assertIn('noindex', r['X-Robots-Tag'], name)

    def test_login_ignores_offsite_next_redirect(self):
        r = self.client.post(reverse('investors:login') + '?next=https://evil.example/steal',
                             {'username': 'jane@example.com', 'password': PW})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/investors/')

    def test_login_honours_safe_next(self):
        r = self.client.post(reverse('investors:login') + '?next=/investors/performance/',
                             {'username': 'jane@example.com', 'password': PW})
        self.assertEqual(r['Location'], '/investors/performance/')

    def test_session_cookie_is_httponly_and_secure(self):
        self.login()
        cookie = self.client.cookies['sessionid']
        self.assertTrue(cookie['httponly'])
        self.assertTrue(cookie['secure'])

    def test_password_reset_is_rate_limited_per_email(self):
        with mock.patch('investors.forms.send_password_reset_email') as send:
            for _ in range(6):
                r = self.client.post(reverse('investors:password_reset'), {'email': 'jane@example.com'})
                self.assertRedirects(r, reverse('investors:password_reset_done'))  # always identical
            self.assertEqual(send.call_count, 3)

    def test_password_minimum_length_is_12(self):
        with self.assertRaises(ValidationError):
            validate_password('Sh0rt-pw-1')  # 10 characters
        validate_password('a-long-enough-Pass-77')

    def test_session_is_rotated_on_login(self):
        self.client.get(reverse('investors:login'))
        before = self.client.session.session_key
        self.login()
        self.assertNotEqual(before, self.client.session.session_key)

    def test_password_change_invalidates_other_sessions(self):
        self.login()
        self.assertEqual(self.client.get(reverse('investors:home')).status_code, 200)
        self.user.set_password('Totally-different-pw-42')
        self.user.save()
        self.assertEqual(self.client.get(reverse('investors:home')).status_code, 302)  # signed out

    def test_staff_cannot_be_linked_as_investor(self):
        staff = User.objects.create_user('s@example.com', 's@example.com', PW, is_staff=True)
        with self.assertRaises(ValidationError):
            InvestorProfile(user=staff, display_name='S').clean()


class DocumentCleanupTests(TestCase):
    def test_deleting_a_document_removes_the_stored_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep = field.storage
        field.storage = FileSystemStorage(location=tmp)
        self.addCleanup(setattr, field, 'storage', keep)

        profile = InvestorProfile.objects.create(user=User.objects.create_user('j', 'j@x.com', PW), display_name='J')
        doc = Document.objects.create(investor=profile, title='T', file=ContentFile(b'x', name='a.pdf'))
        path = os.path.join(tmp, doc.file.name)
        self.assertTrue(os.path.exists(path))
        with self.captureOnCommitCallbacks(execute=True):
            doc.delete()
        self.assertFalse(os.path.exists(path))
