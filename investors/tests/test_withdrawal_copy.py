"""Every investor-facing sentence about calls and checks must say the same thing: we review and fraud check
every request, and we MAY call or email to confirm. None may promise a guaranteed phone call."""
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from investors.models import Investment, InvestorProfile, WithdrawalRequest

PW = 'correct-horse-battery-9'
SECRET = 'origin-secret-for-tests'
US = {'HTTP_X_ORIGIN_VERIFY': SECRET, 'HTTP_CF_IPCOUNTRY': 'US'}
FORBIDDEN = [r'will call you', r'we call you', r'by phone', r'call you on', r'on the number we hold', r'we call you before']


@override_settings(CF_ORIGIN_SECRET=SECRET)
class WithdrawalCopyTests(TestCase):
    def setUp(self):
        cache.clear()
        user = User.objects.create_user('jane', 'jane@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=user, display_name='Jane')
        self.inv = Investment.objects.create(
            investor=self.profile, plan_name='Plan', principal=Decimal('50000'), annual_rate=Decimal('9'),
            start_date=date.today() - timedelta(days=700), maturity_date=date.today() + timedelta(days=700))
        self.client.login(username='jane', password=PW)

    def text(self, response):
        html = response.content.decode()
        html = re.sub(r'<(script|style).*?</\1>', '', html, flags=re.S)
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))

    def submit(self):
        return self.client.post(reverse('investors:withdrawal_new'), {
            'investment': self.inv.pk, 'amount': '100', 'destination': 'offline', 'password': PW, 'confirm': 'on'},
            follow=True, **US)

    def assert_no_guaranteed_call(self, text, where):
        for pattern in FORBIDDEN:
            self.assertIsNone(re.search(pattern, text, re.I), f'{where} still promises a call: "{pattern}"')

    def test_the_request_form_says_exactly_what_was_asked_for(self):
        text = self.text(self.client.get(reverse('investors:withdrawal_new'), **US))
        self.assertIn('We review every request and fraud check every request, we call if necessary or email when we should. '
                      'Funds are never moved automatically.', text)
        self.assertIn('We may call or email you to confirm these details before paying.', text)
        self.assert_no_guaranteed_call(text, 'request form')

    def test_the_withdrawals_list(self):
        text = self.text(self.client.get(reverse('investors:withdrawals'), **US))
        self.assertIn('We review and fraud check every request, and we may call or email you to confirm details before anything is paid.', text)
        self.assert_no_guaranteed_call(text, 'withdrawals list')

    def test_the_request_detail_page(self):
        wr = WithdrawalRequest.objects.create(investor=self.profile, investment=self.inv, amount=Decimal('100'), currency='USD',
                                              destination_type='offline', destination_label='To be arranged with staff')
        text = self.text(self.client.get(reverse('investors:withdrawal_detail', args=[wr.pk]), **US))
        self.assertIn('Our team is reviewing and fraud checking this request. We may call or email you to confirm details before any payment is made.', text)
        self.assert_no_guaranteed_call(text, 'request detail page')

    def test_the_message_shown_after_submitting(self):
        with mock.patch('investors.emails._send', return_value=True):
            response = self.submit()
        text = self.text(response)
        self.assertRegex(text, r'Request WD-\d{4}-[A-Z0-9]{6} submitted\. We may call or email you to confirm details before anything is paid\.')
        self.assert_no_guaranteed_call(text, 'confirmation message')

    def test_the_confirmation_email(self):
        with mock.patch('investors.emails._send', return_value=True) as send, self.captureOnCommitCallbacks(execute=True):
            self.submit()
        to, subject, html = send.call_args_list[0].args
        text = re.sub(r'<[^>]+>', ' ', html)
        self.assertIn('We review and fraud check every request, and we may call or email you to confirm details before anything is paid.', re.sub(r'\s+', ' ', text))
        self.assertIn('If you did not make this request, contact us immediately', text)      # the safety line is untouched
        self.assert_no_guaranteed_call(text, 'confirmation email')

    def test_no_investor_facing_template_or_email_promises_a_guaranteed_call(self):
        from pathlib import Path
        from django.conf import settings
        root = Path(settings.BASE_DIR) / 'investors'
        files = list((root / 'templates' / 'investors').rglob('*.html')) + [root / 'emails.py', root / 'views.py']
        for f in files:
            self.assert_no_guaranteed_call(f.read_text(), f.name)

    def test_the_staff_side_rules_are_unchanged(self):
        # This was a wording change only: typed or offline destinations still need a recorded call-back before approval.
        from investors.withdrawals import transition_error
        wr = WithdrawalRequest(investor=self.profile, investment=self.inv, amount=Decimal('1'), currency='USD',
                               destination_type='typed', status='under_review', callback_verified=False)
        self.assertIn('Call the investor back', transition_error(wr, 'approved'))
