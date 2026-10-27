from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from investors.models import Investment, InvestorProfile, Transaction
from investors.services import add_months

PW = 'correct-horse-battery-9'


class PortalViewTests(TestCase):
    def setUp(self):
        cache.clear()
        today = date.today()

        def make(email, name):
            user = User.objects.create_user(email, email, PW)
            return InvestorProfile.objects.create(user=user, display_name=name)

        self.a = make('a@example.com', 'Alice')
        self.b = make('b@example.com', 'Bob')
        start = add_months(today, -18)
        self.inv_a = Investment.objects.create(
            investor=self.a, plan_name='Alice Plan', principal=Decimal('10000'), annual_rate=Decimal('10'),
            compounding='annual', start_date=start, maturity_date=add_months(start, 60))
        self.inv_b = Investment.objects.create(
            investor=self.b, plan_name='Bob Secret Plan', principal=Decimal('77777'), annual_rate=Decimal('8'),
            compounding='annual', start_date=start, maturity_date=add_months(start, 36), currency='EUR')
        Transaction.objects.create(investment=self.inv_a, type='deposit', amount=Decimal('10000'), date=start, note='Initial')
        self.client.login(username='a@example.com', password=PW)

    def test_dashboard_shows_own_data_only(self):
        r = self.client.get(reverse('investors:home'))
        self.assertContains(r, 'Alice Plan')
        self.assertContains(r, self.inv_a.reference)
        self.assertNotContains(r, 'Bob Secret Plan')
        self.assertNotContains(r, '77,777')

    def test_dashboard_currency_param_cannot_reveal_other_investors_currency(self):
        r = self.client.get(reverse('investors:home') + '?currency=EUR')  # Alice holds no EUR
        self.assertNotContains(r, 'Bob Secret Plan')
        self.assertEqual(r.context['currency'], 'USD')

    def test_default_currency_is_largest_holding(self):
        Investment.objects.create(
            investor=self.a, plan_name='Small EUR', principal=Decimal('100'), annual_rate=Decimal('5'),
            compounding='annual', currency='EUR', start_date=date.today(), maturity_date=add_months(date.today(), 12))
        r = self.client.get(reverse('investors:home'))
        self.assertEqual(r.context['currency'], 'USD')
        self.assertEqual(r.context['currencies'], ['EUR', 'USD'])

    def test_own_investment_detail(self):
        r = self.client.get(reverse('investors:investment', args=[self.inv_a.reference]))
        self.assertContains(r, 'Alice Plan')
        self.assertContains(r, 'Initial')
        self.assertEqual(r.context['value'].quantize(Decimal('0.01')), r.context['value'])

    def test_other_investors_investment_is_404(self):
        r = self.client.get(reverse('investors:investment', args=[self.inv_b.reference]))
        self.assertEqual(r.status_code, 404)

    def test_unknown_reference_is_404_same_as_forbidden(self):
        r = self.client.get(reverse('investors:investment', args=['INV-0000-NOPE00']))
        self.assertEqual(r.status_code, 404)

    def test_performance_page(self):
        r = self.client.get(reverse('investors:performance'))
        self.assertContains(r, 'Performance by year')
        self.assertEqual(r.context['rows'][0]['added'], Decimal('10000.00'))
        self.assertNotContains(r, '77,777')

    def test_anonymous_redirected_on_every_page(self):
        self.client.logout()
        for name, args in [('home', []), ('performance', []), ('investment', [self.inv_a.reference])]:
            r = self.client.get(reverse(f'investors:{name}', args=args))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn('/investors/login/', r['Location'])

    def test_investor_with_no_investments_sees_empty_state(self):
        user = User.objects.create_user('c@example.com', 'c@example.com', PW)
        InvestorProfile.objects.create(user=user, display_name='Carol')
        self.client.login(username='c@example.com', password=PW)
        r = self.client.get(reverse('investors:home'))
        self.assertContains(r, 'no investments on record')
        self.assertEqual(self.client.get(reverse('investors:performance')).status_code, 200)

    def test_public_site_links_to_investor_login(self):
        self.client.logout()
        self.assertContains(self.client.get('/'), reverse('investors:login'))

    def test_money_filter(self):
        from investors.templatetags.investor_extras import money
        self.assertEqual(money(Decimal('1234567.5'), 'USD'), '$1,234,567.50')
        self.assertEqual(money(Decimal('-5'), 'EUR'), '-€5.00')
        self.assertEqual(money(Decimal('10'), 'CHF'), 'CHF 10.00')


class WithdrawalDisplayTests(TestCase):
    def setUp(self):
        today = date.today()
        user = User.objects.create_user('wd', 'wd@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=user, display_name='Wd')
        start = add_months(today, -24)
        self.inv = Investment.objects.create(
            investor=self.profile, plan_name='Growth Plan', principal=Decimal('10000'), annual_rate=Decimal('10'),
            compounding='annual', start_date=start, maturity_date=add_months(today, 36))
        Transaction.objects.create(investment=self.inv, type='withdrawal', amount=Decimal('4000'), date=add_months(today, -6))
        self.client.login(username='wd', password=PW)

    def test_dashboard_value_is_net_and_says_so(self):
        r = self.client.get(reverse('investors:home'))
        totals = r.context['totals']
        self.assertEqual(totals['withdrawn'], Decimal('4000.00'))
        self.assertEqual(totals['gain'], totals['value'] + Decimal('4000.00') - Decimal('10000'))
        self.assertContains(r, 'after $4,000.00 withdrawn')
        self.assertContains(r, 'including money withdrawn')

    def test_investment_page_shows_withdrawn_and_corrected_gain(self):
        r = self.client.get(reverse('investors:investment', args=[self.inv.reference]))
        self.assertContains(r, 'after $4,000.00 withdrawn')
        self.assertEqual(r.context['gain'], r.context['value'] + Decimal('4000.00') - Decimal('10000'))
        self.assertGreater(r.context['gain'], Decimal('0'))  # withdrawing must not look like a loss

    def test_performance_page_has_a_withdrawn_column_only_when_needed(self):
        self.assertContains(self.client.get(reverse('investors:performance')), '<th class="num">Withdrawn</th>')
        Transaction.objects.filter(type='withdrawal').delete()
        self.assertNotContains(self.client.get(reverse('investors:performance')), '<th class="num">Withdrawn</th>')
