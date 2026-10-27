"""
Growth maths for the investor portal. Pure functions over Investment rows;
all money is Decimal and rounded half-up to cents only at the end.

Conventions
- "Value" always means total value: principal plus all interest earned to date,
  whether or not that interest has been paid out. Staff-posted ValuationSnapshots
  must follow the same convention.
- Compounding happens every k whole months (annual=12, quarterly=3, monthly=1).
  Part of a period is accrued as simple interest on the running balance.
- Investments with periodic payouts pay interest out rather than reinvesting it,
  so they always accrue as simple interest regardless of `compounding`.
- Withdrawals (Transaction type 'withdrawal') take money out of the position. The value shown is
  NET of them: a withdrawal removes the same share of the position that it is of its value on the
  day, and interest keeps accruing only on what is left. Posted snapshots are taken to already
  reflect withdrawals made on or before their date; later ones are subtracted from the snapshot.
- Gain = value + money already withdrawn - amount invested, so taking money out is never a loss.
"""
import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal('0.01')
ZERO = Decimal('0')
_PERIOD_MONTHS = {'annual': 12, 'quarterly': 3, 'monthly': 1}


def _q(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def add_months(d, months):
    """d + months, clamping the day to the target month's length."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def months_between(start, end):
    """Whole months elapsed from start to end (end-of-month aware)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day and end.day != calendar.monthrange(end.year, end.month)[1]:
        months -= 1
    return max(months, 0)


def _snapshots(investment):
    if investment.pk is None:
        return []
    return sorted(investment.snapshots.all(), key=lambda s: s.date)


def projected_value(investment, on_date):
    """Computed total value on `on_date`, ignoring any posted snapshots."""
    if on_date < investment.start_date:
        return ZERO
    on_date = min(on_date, investment.maturity_date)

    principal = investment.principal
    rate = investment.annual_rate / Decimal(100)

    if investment.payout_mode == 'periodic' or investment.compounding == 'simple':
        days = (on_date - investment.start_date).days
        return _q(principal * (1 + rate * days / Decimal(365)))

    k = _PERIOD_MONTHS[investment.compounding]
    periods = months_between(investment.start_date, on_date) // k
    balance = principal * (1 + rate * k / Decimal(12)) ** periods
    base = add_months(investment.start_date, periods * k)
    days = (on_date - base).days
    balance *= 1 + rate * days / Decimal(365)
    return _q(balance)


def _withdrawals(investment):
    """[(date, amount)] of money paid out of this investment, oldest first."""
    if investment.pk is None:
        return []
    return sorted(((t.date, t.amount) for t in investment.transactions.all() if t.type == 'withdrawal'),
                  key=lambda w: w[0])


def withdrawn_total(investment, on_date=None):
    """Total paid out of the investment (up to `on_date` if given)."""
    return sum((a for d, a in _withdrawals(investment) if on_date is None or d <= on_date), ZERO)


def _remaining_fraction(investment, on_date):
    """Share of the original position still invested after withdrawals up to `on_date`.

    Each withdrawal removes (amount / value on the day) of what was left, so growth afterwards is
    earned only on the remainder. A withdrawal at or above the value empties the position."""
    fraction = Decimal(1)
    for when, amount in _withdrawals(investment):
        if when > on_date:
            break
        before = projected_value(investment, max(when, investment.start_date)) * fraction
        fraction = ZERO if amount >= before else fraction * (1 - amount / before)
    return fraction


def value_on(investment, on_date):
    """Best known value on a date, net of withdrawals: latest posted snapshot, else computed."""
    if on_date < investment.start_date:
        return ZERO
    posted = [s for s in _snapshots(investment) if s.date <= on_date]
    if posted:
        latest = posted[-1]
        later = sum((a for d, a in _withdrawals(investment) if latest.date < d <= on_date), ZERO)
        return max(latest.value - later, ZERO)
    return _q(projected_value(investment, on_date) * _remaining_fraction(investment, on_date))


def current_value(investment, today=None):
    return value_on(investment, today or date.today())


def growth_series(investment, today=None):
    """Monthly chart points: {'actual': [(date, value)], 'projected': [...]}.

    Actual runs start -> today (or maturity). Projection continues from the last
    actual point to maturity, only for active investments that haven't matured.
    """
    today = today or date.today()
    end_actual = min(today, investment.maturity_date)
    actual = []
    i = 0
    while True:
        d = add_months(investment.start_date, i)
        if d > end_actual:
            break
        actual.append((d, value_on(investment, d)))
        i += 1
    if not actual or actual[-1][0] != end_actual and end_actual >= investment.start_date:
        actual.append((end_actual, value_on(investment, end_actual)))

    projected = []
    if investment.status == 'active' and today < investment.maturity_date:
        left = _remaining_fraction(investment, today)
        projected.append(actual[-1])
        d = add_months(end_actual, 1)
        while d < investment.maturity_date:
            projected.append((d, _q(projected_value(investment, d) * left)))
            d = add_months(d, 1)
        projected.append((investment.maturity_date, _q(projected_value(investment, investment.maturity_date) * left)))
    return {'actual': actual, 'projected': projected}


def pct(gain, base):
    return _q(gain / base * 100) if base else ZERO


def _load(investor, currency=None):
    qs = investor.investments.prefetch_related('snapshots', 'transactions')
    if currency:
        qs = qs.filter(currency=currency)
    return list(qs)


def portfolio_summary(investor, today=None):
    """Per-investment rows plus totals grouped by currency (never mixed)."""
    today = today or date.today()
    rows = []
    for inv in _load(investor):
        value = current_value(inv, today)
        withdrawn = withdrawn_total(inv, today)
        gain = value + withdrawn - inv.principal
        rows.append({
            'investment': inv,
            'value': value,
            'withdrawn': withdrawn,
            'gain': gain,
            'return_pct': pct(gain, inv.principal),
        })
    by_currency = []
    for cur in sorted({r['investment'].currency for r in rows}):
        mine = [r for r in rows if r['investment'].currency == cur]
        invested = sum((r['investment'].principal for r in mine), ZERO)
        value = sum((r['value'] for r in mine), ZERO)
        withdrawn = sum((r['withdrawn'] for r in mine), ZERO)
        gain = value + withdrawn - invested
        by_currency.append({
            'currency': cur,
            'count': len(mine),
            'invested': invested,
            'value': value,
            'withdrawn': withdrawn,
            'gain': gain,
            'return_pct': pct(gain, invested),
        })
    return {'rows': rows, 'by_currency': by_currency}


def portfolio_series(investor, currency, today=None):
    """Monthly total value of all of an investor's plans in one currency.

    Returns {'actual': [(date, value)], 'projected': [(date, value)]}. The
    projection runs to the latest maturity among still-active plans, using the
    computed schedule; plans that are no longer active are held flat.
    """
    today = today or date.today()
    investments = _load(investor, currency)
    if not investments:
        return {'actual': [], 'projected': []}
    first = min(i.start_date for i in investments)

    def total(d):
        return sum((value_on(i, d) for i in investments), ZERO)

    actual = []
    n = 0
    while add_months(first, n) <= today:
        d = add_months(first, n)
        actual.append((d, total(d)))
        n += 1
    if actual[-1][0] != today:
        actual.append((today, total(today)))

    active = [i for i in investments if i.status == 'active' and i.maturity_date > today]
    projected = []
    if active:
        end = max(i.maturity_date for i in active)

        left = {i.pk: _remaining_fraction(i, today) for i in active}

        def future_total(d):
            return sum(
                (_q(projected_value(i, d) * left[i.pk]) if i in active else value_on(i, today) for i in investments),
                ZERO,
            )

        projected.append(actual[-1])
        d = add_months(today, 1)
        while d < end:
            projected.append((d, future_total(d)))
            d = add_months(d, 1)
        projected.append((end, future_total(end)))
    return {'actual': actual, 'projected': projected}


def yearly_returns(investor, today=None, currency=None):
    """Per calendar year: opening value, new money added, closing value, gain, %.

    Money withdrawn in the year counts as part of the gain (it left the account, it wasn't lost).
    Return % is gain over (opening + added), not time-weighted; it is meant as
    a plain year-on-year indicator. Pass `currency` when the investor holds
    plans in more than one.
    """
    today = today or date.today()
    investments = _load(investor, currency)
    if not investments:
        return []
    first_year = min(inv.start_date.year for inv in investments)
    rows = []
    for year in range(first_year, today.year + 1):
        open_date = date(year - 1, 12, 31)
        close_date = min(date(year, 12, 31), today)
        opening = sum((value_on(i, open_date) for i in investments), ZERO)
        added = sum((i.principal for i in investments if i.start_date.year == year), ZERO)
        closing = sum((value_on(i, close_date) for i in investments), ZERO)
        withdrawn = sum((a for i in investments for d, a in _withdrawals(i)
                         if d.year == year and d <= close_date), ZERO)
        gain = closing + withdrawn - opening - added
        rows.append({
            'year': year,
            'opening': opening,
            'added': added,
            'withdrawn': withdrawn,
            'closing': closing,
            'gain': gain,
            'return_pct': pct(gain, opening + added),
        })
    return rows
