import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from investors import storage as storage_mod
from investors.models import Document, Investment, InvestorProfile
from investors.validators import MAX_BYTES, validate_document

PW = 'correct-horse-battery-9'


class DocumentTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        original = field.storage
        field.storage = FileSystemStorage(location=self.tmp)
        self.addCleanup(setattr, field, 'storage', original)

        def make(email, name):
            return InvestorProfile.objects.create(user=User.objects.create_user(email, email, PW), display_name=name)
        self.a, self.b = make('a@example.com', 'Alice'), make('b@example.com', 'Bob')
        self.doc_a = Document.objects.create(investor=self.a, title='Q1 Statement 2026',
                                             file=ContentFile(b'%PDF-alice', name='secret-account-1234.pdf'))
        self.doc_b = Document.objects.create(investor=self.b, title='Bob Agreement',
                                             file=ContentFile(b'%PDF-bob', name='bob.pdf'))
        self.client.login(username='a@example.com', password=PW)


class DownloadTests(DocumentTestBase):
    def test_own_document_downloads_as_attachment(self):
        r = self.client.get(reverse('investors:document_download', args=[self.doc_a.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), b'%PDF-alice')
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertIn('q1-statement-2026.pdf', r['Content-Disposition'])
        self.assertIn('no-store', r['Cache-Control'])

    def test_other_investors_document_is_404(self):
        r = self.client.get(reverse('investors:document_download', args=[self.doc_b.pk]))
        self.assertEqual(r.status_code, 404)

    def test_anonymous_redirected(self):
        self.client.logout()
        r = self.client.get(reverse('investors:document_download', args=[self.doc_a.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/investors/login/', r['Location'])

    def test_list_shows_only_own_documents(self):
        r = self.client.get(reverse('investors:documents'))
        self.assertContains(r, 'Q1 Statement 2026')
        self.assertNotContains(r, 'Bob Agreement')

    def test_storage_key_hides_original_filename(self):
        self.assertNotIn('secret-account', self.doc_a.file.name)
        self.assertTrue(self.doc_a.file.name.startswith(f'investor_documents/{self.a.pk}/'))
        self.assertTrue(self.doc_a.file.name.endswith('.pdf'))

    def test_files_are_not_served_from_media_url(self):
        r = self.client.get(settings.MEDIA_URL + self.doc_a.file.name)
        self.assertEqual(r.status_code, 404)
        self.assertFalse(str(settings.PRIVATE_MEDIA_ROOT).startswith(str(settings.MEDIA_ROOT)))

    def test_supabase_download_redirects_to_short_lived_signed_url(self):
        fake = mock.Mock()
        fake.url.return_value = 'https://signed.example/file?sig=abc'
        # FieldFile takes its storage from the field, so swap it for the request.
        field = Document._meta.get_field('file')
        keep = field.storage
        field.storage = fake
        try:
            with mock.patch.object(storage_mod, 'using_supabase', return_value=True):
                r = self.client.get(reverse('investors:document_download', args=[self.doc_a.pk]))
        finally:
            field.storage = keep
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], 'https://signed.example/file?sig=abc')
        kwargs = fake.url.call_args.kwargs
        self.assertEqual(kwargs['expire'], 60)
        self.assertIn('attachment', kwargs['parameters']['ResponseContentDisposition'])
        self.assertIn('q1-statement-2026.pdf', kwargs['parameters']['ResponseContentDisposition'])


class ValidationTests(TestCase):
    def test_rejects_dangerous_extensions(self):
        for name in ['evil.exe', 'page.html', 'x.svg', 'noext']:
            with self.assertRaises(ValidationError, msg=name):
                validate_document(SimpleUploadedFile(name, b'x'))

    def test_accepts_allowed_types_case_insensitively(self):
        validate_document(SimpleUploadedFile('Statement.PDF', b'x'))

    def test_rejects_oversize(self):
        big = SimpleUploadedFile('big.pdf', b'x')
        big.size = MAX_BYTES + 1
        with self.assertRaises(ValidationError):
            validate_document(big)

    def test_investment_must_belong_to_same_investor(self):
        a = InvestorProfile.objects.create(user=User.objects.create_user('a', 'a@x.com', PW), display_name='A')
        b = InvestorProfile.objects.create(user=User.objects.create_user('b', 'b@x.com', PW), display_name='B')
        inv_b = Investment.objects.create(investor=b, plan_name='P', principal=Decimal('1'), annual_rate=Decimal('1'),
                                          start_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            Document(investor=a, investment=inv_b, title='t').clean()


class StorageConfigTests(TestCase):
    def test_local_fallback_is_filesystem(self):
        self.assertIsInstance(storage_mod.document_storage(), FileSystemStorage)

    @override_settings(SUPABASE_S3_BUCKET='investor-docs', SUPABASE_S3_ENDPOINT='https://x.storage.supabase.co/storage/v1/s3',
                       SUPABASE_S3_REGION='eu-west-2', SUPABASE_S3_ACCESS_KEY_ID='id', SUPABASE_S3_SECRET_ACCESS_KEY='secret')
    def test_supabase_storage_is_private_and_signed(self):
        s = storage_mod.document_storage()
        self.assertEqual(s.bucket_name, 'investor-docs')
        self.assertEqual(s.endpoint_url, 'https://x.storage.supabase.co/storage/v1/s3')
        self.assertTrue(s.querystring_auth)
        self.assertEqual(s.querystring_expire, 60)
        self.assertIsNone(s.default_acl)
        self.assertFalse(s.file_overwrite)
