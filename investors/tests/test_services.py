from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from investors import services
from investors.models import Investment, InvestorProfile, ValuationSnapshot


def make(principal, rate, compounding, start, maturity, payout='maturity', status='active'):
    return Investment(
        principal=Decimal(principal), annual_rate=Decimal(rate), compounding=compounding,
        payout_mode=payout, start_date=start, maturity_date=maturity, status=status,
    )


class ProjectedValueTests(SimpleTestCase):
    def test_simple_full_year(self):
        inv = make('10000', '8', 'simple', date(2023, 1, 1), date(2025, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2024, 1, 1)), Decimal('10800.00'))

    def test_annual_two_full_years(self):
        inv = make('10000', '10', 'annual', date(2023, 1, 1), date(2026, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2025, 1, 1)), Decimal('12100.00'))

    def test_annual_partial_period_accrues_simple(self):
        inv = make('10000', '10', 'annual', date(2023, 1, 1), date(2026, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2024, 7, 1)), Decimal('11548.49'))

    def test_quarterly(self):
        inv = make('10000', '12', 'quarterly', date(2023, 1, 1), date(2025, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2024, 1, 1)), Decimal('11255.09'))

    def test_monthly(self):
        inv = make('1200', '12', 'monthly', date(2023, 1, 1), date(2025, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2024, 1, 1)), Decimal('1352.19'))

    def test_periodic_payout_ignores_compounding(self):
        inv = make('10000', '8', 'annual', date(2023, 1, 1), date(2025, 1, 1), payout='periodic')
        self.assertEqual(services.projected_value(inv, date(2024, 1, 1)), Decimal('10800.00'))

    def test_before_start_is_zero(self):
        inv = make('10000', '8', 'annual', date(2023, 1, 1), date(2025, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2022, 12, 31)), Decimal('0'))

    def test_clamped_at_maturity(self):
        inv = make('10000', '10', 'annual', date(2023, 1, 1), date(2025, 1, 1))
        self.assertEqual(services.projected_value(inv, date(2030, 1, 1)), Decimal('12100.00'))


class DateHelperTests(SimpleTestCase):
    def test_add_months_clamps_day(self):
        self.assertEqual(services.add_months(date(2023, 1, 31), 1), date(2023, 2, 28))
        self.assertEqual(services.add_months(date(2023, 11, 15), 3), date(2024, 2, 15))

    def test_months_between_end_of_month(self):
        self.assertEqual(services.months_between(date(2023, 1, 31), date(2023, 2, 28)), 1)
        self.assertEqual(services.months_between(date(2023, 1, 15), date(2023, 2, 14)), 0)


class GrowthSeriesTests(SimpleTestCase):
    def test_actual_and_projection_meet_and_end_at_maturity(self):
        inv = make('10000', '10', 'annual', date(2023, 1, 1), date(2025, 1, 1))
        series = services.growth_series(inv, today=date(2023, 6, 15))
        self.assertEqual(series['actual'][0], (date(2023, 1, 1), Decimal('10000.00')))
        self.assertEqual(series['actual'][-1][0], date(2023, 6, 15))
        self.assertEqual(series['projected'][0], series['actual'][-1])
        self.assertEqual(series['projected'][-1], (date(2025, 1, 1), Decimal('12100.00')))

    def test_matured_has_no_projection(self):
        inv = make('10000', '10', 'annual', date(2023, 1, 1), date(2025, 1, 1), status='matured')
        series = services.growth_series(inv, today=date(2026, 1, 1))
        self.assertEqual(series['projected'], [])
        self.assertEqual(series['actual'][-1], (date(2025, 1, 1), Decimal('12100.00')))


class PortfolioTests(TestCase):
    def setUp(self):
        user = User.objects.create_user('inv1', password='x')
        self.profile = InvestorProfile.objects.create(user=user, display_name='Inv One')
        self.a = Investment.objects.create(
            investor=self.profile, plan_name='A', principal=Decimal('10000'), annual_rate=Decimal('10'),
            compounding='annual', start_date=date(2023, 1, 1), maturity_date=date(2026, 1, 1),
        )
        self.b = Investment.objects.create(
            investor=self.profile, plan_name='B', principal=Decimal('5000'), annual_rate=Decimal('8'),
            compounding='simple', start_date=date(2024, 1, 1), maturity_date=date(2027, 1, 1),
        )

    def test_summary_totals(self):
        s = services.portfolio_summary(self.profile, today=date(2025, 1, 1))
        # A: 12100.00 ; B: 5000 * (1 + .08 * 366/365) = 5401.10
        self.assertEqual(len(s['by_currency']), 1)
        usd = s['by_currency'][0]
        self.assertEqual(usd['currency'], 'USD')
        self.assertEqual(usd['invested'], Decimal('15000.00'))
        self.assertEqual(usd['value'], Decimal('17501.10'))
        self.assertEqual(usd['gain'], Decimal('2501.10'))
        self.assertEqual(usd['return_pct'], Decimal('16.67'))

    def test_snapshot_overrides_computed(self):
        ValuationSnapshot.objects.create(investment=self.a, date=date(2024, 12, 31), value=Decimal('12000.00'))
        self.assertEqual(services.current_value(self.a, date(2025, 1, 1)), Decimal('12000.00'))

    def test_yearly_returns(self):
        rows = {r['year']: r for r in services.yearly_returns(self.profile, today=date(2025, 6, 1))}
        self.assertEqual(sorted(rows), [2023, 2024, 2025])
        self.assertEqual(rows[2023]['opening'], Decimal('0'))
        self.assertEqual(rows[2023]['added'], Decimal('10000.00'))
        self.assertEqual(rows[2023]['closing'], Decimal('10997.26'))  # A on 31 Dec: 364 days, one short of a full year
        self.assertEqual(rows[2023]['gain'], Decimal('997.26'))
        self.assertEqual(rows[2023]['return_pct'], Decimal('9.97'))
        self.assertEqual(rows[2024]['opening'], rows[2023]['closing'])
        self.assertEqual(rows[2024]['added'], Decimal('5000.00'))
        self.assertEqual(rows[2025]['opening'], rows[2024]['closing'])

    def test_investor_isolation_in_summary(self):
        other = InvestorProfile.objects.create(user=User.objects.create_user('inv2', password='x'), display_name='Two')
        s = services.portfolio_summary(other, today=date(2025, 1, 1))
        self.assertEqual(s['rows'], [])
        self.assertEqual(s['by_currency'], [])


class MultiCurrencyAndSeriesTests(TestCase):
    def setUp(self):
        user = User.objects.create_user('mc', password='x')
        self.profile = InvestorProfile.objects.create(user=user, display_name='MC')
        base = dict(investor=self.profile, compounding='annual', start_date=date(2023, 1, 1),
                    maturity_date=date(2026, 1, 1), annual_rate=Decimal('10'))
        Investment.objects.create(plan_name='US', principal=Decimal('10000'), currency='USD', **base)
        Investment.objects.create(plan_name='EU', principal=Decimal('2000'), currency='EUR', **base)

    def test_currencies_never_mixed(self):
        s = services.portfolio_summary(self.profile, today=date(2025, 1, 1))
        got = {c['currency']: c for c in s['by_currency']}
        self.assertEqual(got['USD']['value'], Decimal('12100.00'))
        self.assertEqual(got['EUR']['value'], Decimal('2420.00'))

    def test_yearly_returns_filters_currency(self):
        rows = services.yearly_returns(self.profile, today=date(2024, 1, 1), currency='EUR')
        self.assertEqual(rows[0]['added'], Decimal('2000.00'))

    def test_portfolio_series_actual_then_projected(self):
        series = services.portfolio_series(self.profile, 'USD', today=date(2024, 1, 1))
        self.assertEqual(series['actual'][0], (date(2023, 1, 1), Decimal('10000.00')))
        self.assertEqual(series['actual'][-1], (date(2024, 1, 1), Decimal('11000.00')))
        self.assertEqual(series['projected'][0], series['actual'][-1])
        self.assertEqual(series['projected'][-1], (date(2026, 1, 1), Decimal('13310.00')))

    def test_portfolio_series_empty_for_other_currency(self):
        self.assertEqual(services.portfolio_series(self.profile, 'GBP'), {'actual': [], 'projected': []})


class WithdrawalAccountingTests(TestCase):
    """10,000 at 10% compounded annually from 2023-01-01 is worth 11,000 on 2024-01-01.
    Withdrawing 5,500 that day removes exactly half the position, so every later value is half of
    the no-withdrawal figure (12,100 -> 6,050 on 2025-01-01) and the investor has not lost anything."""

    def setUp(self):
        from investors.models import Transaction
        self.Transaction = Transaction
        user = User.objects.create_user('w', password='x')
        self.profile = InvestorProfile.objects.create(user=user, display_name='W')
        self.inv = Investment.objects.create(
            investor=self.profile, plan_name='W', principal=Decimal('10000'), annual_rate=Decimal('10'),
            compounding='annual', start_date=date(2023, 1, 1), maturity_date=date(2028, 1, 1))
        self.withdraw(self.inv, '5500', date(2024, 1, 1))

    def withdraw(self, inv, amount, when):
        return self.Transaction.objects.create(investment=inv, type='withdrawal', amount=Decimal(amount), date=when)

    def fresh(self):
        return Investment.objects.get(pk=self.inv.pk)

    def test_value_drops_by_the_amount_on_the_day(self):
        self.assertEqual(services.value_on(self.fresh(), date(2023, 12, 31)), Decimal('10997.26'))  # before: unchanged
        self.assertEqual(services.value_on(self.fresh(), date(2024, 1, 1)), Decimal('5500.00'))

    def test_interest_afterwards_accrues_only_on_what_is_left(self):
        self.assertEqual(services.value_on(self.fresh(), date(2025, 1, 1)), Decimal('6050.00'))
        self.assertEqual(services.value_on(self.fresh(), date(2026, 1, 1)), Decimal('6655.00'))

    def test_future_withdrawals_are_ignored(self):
        self.assertEqual(services.value_on(self.fresh(), date(2023, 6, 1)),
                         services.projected_value(self.fresh(), date(2023, 6, 1)))

    def test_gain_includes_money_already_withdrawn(self):
        s = services.portfolio_summary(self.profile, today=date(2025, 1, 1))['by_currency'][0]
        self.assertEqual((s['value'], s['withdrawn']), (Decimal('6050.00'), Decimal('5500.00')))
        self.assertEqual(s['gain'], Decimal('1550.00'))          # 6,050 + 5,500 - 10,000
        self.assertEqual(s['return_pct'], Decimal('15.50'))
        row = services.portfolio_summary(self.profile, today=date(2025, 1, 1))['rows'][0]
        self.assertEqual((row['gain'], row['withdrawn']), (Decimal('1550.00'), Decimal('5500.00')))

    def test_withdrawing_is_never_a_loss_on_the_day(self):
        s = services.portfolio_summary(self.profile, today=date(2024, 1, 1))['by_currency'][0]
        self.assertEqual(s['gain'], Decimal('1000.00'))          # same as holding it: 11,000 - 10,000

    def test_two_withdrawals_compose(self):
        self.withdraw(self.inv, '1650', date(2025, 1, 1))        # 6,050 left -> 4,400
        inv = self.fresh()
        self.assertEqual(services.value_on(inv, date(2025, 1, 1)), Decimal('4400.00'))
        self.assertEqual(services.value_on(inv, date(2026, 1, 1)), Decimal('4840.00'))
        self.assertEqual(services.withdrawn_total(inv, date(2026, 1, 1)), Decimal('7150.00'))
        self.assertEqual(services.withdrawn_total(inv, date(2024, 6, 1)), Decimal('5500.00'))

    def test_withdrawing_everything_empties_the_position(self):
        self.withdraw(self.inv, '999999', date(2025, 1, 1))
        inv = self.fresh()
        self.assertEqual(services.value_on(inv, date(2025, 1, 1)), Decimal('0.00'))
        self.assertEqual(services.value_on(inv, date(2027, 1, 1)), Decimal('0.00'))

    def test_snapshot_already_reflects_earlier_withdrawals_but_later_ones_are_subtracted(self):
        ValuationSnapshot.objects.create(investment=self.inv, date=date(2024, 6, 30), value=Decimal('5900.00'))
        self.withdraw(self.inv, '900', date(2024, 8, 15))
        inv = self.fresh()
        self.assertEqual(services.value_on(inv, date(2024, 8, 1)), Decimal('5900.00'))   # 5,500 on 1 Jan is inside the snapshot
        self.assertEqual(services.value_on(inv, date(2024, 9, 1)), Decimal('5000.00'))   # only the 900 after it is subtracted

    def test_projection_is_scaled_to_what_remains(self):
        series = services.growth_series(self.fresh(), today=date(2024, 6, 1))
        maturity = date(2028, 1, 1)
        self.assertEqual(series['projected'][-1], (maturity, services.value_on(self.fresh(), maturity)))
        self.assertLess(series['actual'][-1][1], Decimal('7000'))

    def test_portfolio_series_is_net(self):
        series = services.portfolio_series(self.profile, 'USD', today=date(2025, 1, 1))
        self.assertEqual(series['actual'][-1], (date(2025, 1, 1), Decimal('6050.00')))
        self.assertEqual(series['projected'][-1][1], services.value_on(self.fresh(), date(2028, 1, 1)))

    def test_yearly_returns_treat_withdrawals_as_gain_not_loss(self):
        rows = {r['year']: r for r in services.yearly_returns(self.profile, today=date(2025, 6, 1))}
        self.assertEqual(rows[2023]['gain'], Decimal('997.26'))
        r = rows[2024]
        self.assertEqual((r['opening'], r['withdrawn'], r['closing']), (Decimal('10997.26'), Decimal('5500.00'), Decimal('6050.00')))
        self.assertEqual(r['gain'], Decimal('552.74'))           # 6,050 + 5,500 - 10,997.26
        self.assertEqual(rows[2025]['withdrawn'], Decimal('0'))

    def test_availability_is_not_reduced_twice(self):
        from investors.withdrawals import available_to_withdraw
        self.assertEqual(available_to_withdraw(self.fresh(), today=date(2025, 1, 1)), Decimal('6050.00'))

    def test_availability_for_income_plans_excludes_interest_already_paid(self):
        from investors.withdrawals import available_to_withdraw
        income = Investment.objects.create(
            investor=self.profile, plan_name='Income', principal=Decimal('10000'), annual_rate=Decimal('7.5'),
            compounding='simple', payout_mode='periodic', start_date=date(2024, 1, 1), maturity_date=date(2027, 1, 1))
        for when in (date(2024, 4, 1), date(2024, 7, 1)):
            self.Transaction.objects.create(investment=income, type='payout', amount=Decimal('187.50'), date=when)
        income = Investment.objects.get(pk=income.pk)
        self.assertEqual(services.value_on(income, date(2024, 7, 1)), Decimal('10373.97'))
        self.assertEqual(available_to_withdraw(income, today=date(2024, 7, 1)), Decimal('9998.97'))  # 10,373.97 - 375

    def test_withdrawal_dated_before_the_start_does_not_break_anything(self):
        self.withdraw(self.inv, '100', date(2022, 1, 1))
        value = services.value_on(self.fresh(), date(2023, 6, 1))
        self.assertGreaterEqual(value, Decimal('0'))
