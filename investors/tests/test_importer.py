from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from investors import importer
from investors.models import Investment, InvestorProfile, Transaction, ValuationSnapshot

PW = 'correct-horse-battery-9'
HEADER = 'investment,date,type,amount,note\n'


class ImporterBase(TestCase):
    def setUp(self):
        profile = InvestorProfile.objects.create(user=User.objects.create_user('inv', 'i@x.com', PW), display_name='I')
        self.inv = Investment.objects.create(
            investor=profile, plan_name='P', principal=Decimal('50000'), annual_rate=Decimal('9'),
            start_date=date(2022, 3, 1), maturity_date=date(2028, 3, 1))
        self.ref = self.inv.reference

    def csv(self, *lines):
        return HEADER + '\n'.join(l.replace('REF', self.ref) for l in lines) + '\n'


class ParseTests(ImporterBase):
    def test_valid_rows(self):
        r = importer.parse(self.csv('REF,2022-03-01,deposit,"50,000.00",Initial',
                                    'REF,2023-03-01,interest_credit,4500,Year 1',
                                    'REF,2023-03-31,valuation,54500.50,March'))
        self.assertTrue(r.ok, r.errors)
        self.assertEqual([x['type'] for x in r.rows], ['deposit', 'interest_credit', 'valuation'])
        self.assertEqual(r.rows[0]['amount'], Decimal('50000.00'))

    def test_bom_and_header_case_and_blank_lines_tolerated(self):
        text = '﻿Investment, Date ,TYPE,Amount\n\n' + f'{self.ref},2023-01-01,payout,10\n'
        self.assertTrue(importer.parse(text).ok)

    def test_each_kind_of_problem_is_reported_with_its_line(self):
        future = (date.today() + timedelta(days=2)).isoformat()
        r = importer.parse(self.csv(
            'INV-0000-NOPE00,2023-01-01,deposit,10,',   # 2: unknown investment
            'REF,01/02/2023,deposit,10,',               # 3: bad date format
            'REF,2023-01-01,bonus,10,',                 # 4: bad type
            'REF,2023-01-01,deposit,$10,',              # 5: currency symbol
            'REF,2023-01-01,deposit,10.123,',           # 6: 3 decimals
            'REF,2023-01-01,deposit,0,',                # 7: zero transaction
            'REF,2023-01-01,deposit,-5,',               # 8: negative
            f'REF,{future},deposit,10,',                # 9: future
            'REF,2021-12-31,deposit,10,',               # 10: before investment start
        ))
        self.assertEqual(r.rows, [])
        self.assertEqual({line for line, _ in r.errors}, set(range(2, 11)))

    def test_valuation_may_be_zero_but_transaction_may_not(self):
        self.assertTrue(importer.parse(self.csv('REF,2023-01-01,valuation,0,')).ok)

    def test_duplicates_within_file_and_against_database_rejected(self):
        Transaction.objects.create(investment=self.inv, type='payout', amount=Decimal('10.00'), date=date(2023, 1, 1))
        ValuationSnapshot.objects.create(investment=self.inv, date=date(2023, 2, 1), value=Decimal('1'))
        r = importer.parse(self.csv(
            'REF,2023-01-01,payout,10.00,',             # already in DB
            'REF,2023-02-01,valuation,5,',              # snapshot already in DB
            'REF,2023-05-01,payout,7,', 'REF,2023-05-01,payout,7,',  # twice in file
        ))
        self.assertEqual([line for line, _ in r.errors], [2, 3, 5])
        self.assertEqual(len(r.rows), 1)

    def test_structure_errors(self):
        self.assertFalse(importer.parse('').ok)
        self.assertIn('Missing required', importer.parse('a,b\n1,2\n').errors[0][1])
        self.assertIn('no data rows', importer.parse(HEADER).errors[0][1])

    def test_row_limit(self):
        rows = [f'REF,2023-01-01,payout,{i + 1},' for i in range(importer.MAX_ROWS + 5)]
        r = importer.parse(self.csv(*rows))
        self.assertTrue(any('Too many rows' in m for _, m in r.errors))


class AdminImportFlowTests(ImporterBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser('root', 'r@x.com', PW))
        self.url = reverse('admin:investors_investment_import_history')

    def upload(self, text):
        return self.client.post(self.url, {'file': SimpleUploadedFile('h.csv', text.encode('utf-8'))})

    def test_button_and_sample_available(self):
        self.assertContains(self.client.get(reverse('admin:investors_investment_changelist')), 'Import history (CSV)')
        r = self.client.get(self.url + '?sample=1')
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertTrue(importer.SAMPLE.startswith('investment,date,type,amount,note'))

    def test_preview_saves_nothing(self):
        r = self.upload(self.csv('REF,2023-03-01,interest_credit,4500,Year 1'))
        self.assertContains(r, 'Preview: 1 row')
        self.assertContains(r, 'Confirm and import')
        self.assertEqual(Transaction.objects.count(), 0)

    def test_confirm_saves_everything_logs_and_sends_no_email(self):
        text = self.csv('REF,2023-03-01,interest_credit,4500,Year 1', 'REF,2023-03-31,valuation,54500,March')
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.url, {'csv_text': text})
        self.assertRedirects(r, reverse('admin:investors_investment_changelist'))
        self.assertEqual(Transaction.objects.filter(investment=self.inv).count(), 1)
        self.assertEqual(ValuationSnapshot.objects.filter(investment=self.inv).count(), 1)
        send.assert_not_called()
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.inv.pk), change_message__contains='Imported 2').exists())

    def test_file_with_any_error_imports_nothing(self):
        r = self.upload(self.csv('REF,2023-03-01,payout,10,ok', 'REF,2023-03-02,payout,abc,bad'))
        self.assertContains(r, '1 problem')
        self.assertNotContains(r, 'Confirm and import')
        self.assertEqual(Transaction.objects.count(), 0)

    def test_tampered_confirm_is_revalidated(self):
        r = self.client.post(self.url, {'csv_text': self.csv('REF,2023-03-02,payout,abc,bad')})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_non_utf8_and_oversize_and_missing_file(self):
        r = self.client.post(self.url, {'file': SimpleUploadedFile('h.csv', b'\xff\xfe\x00bad')})
        self.assertContains(r, 'not UTF-8')
        big = SimpleUploadedFile('h.csv', b'x' * (importer.MAX_BYTES + 1))
        self.assertContains(self.client.post(self.url, {'file': big}), 'too large')
        self.assertContains(self.client.post(self.url, {}), 'Choose a CSV file')

    def test_importing_twice_is_blocked_as_duplicate(self):
        text = self.csv('REF,2023-03-01,payout,10,x')
        self.client.post(self.url, {'csv_text': text})
        r = self.client.post(self.url, {'csv_text': text})
        self.assertContains(r, 'already recorded')
        self.assertEqual(Transaction.objects.count(), 1)

    def test_requires_staff_with_permission(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)  # to admin login
        User.objects.create_user('plain', 'p@x.com', PW, is_staff=True)
        self.client.login(username='plain', password=PW)
        self.assertEqual(self.client.get(self.url).status_code, 403)  # staff without model permission
