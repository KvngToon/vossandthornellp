"""Engine for the FICTIONAL demo dataset: invented investors with a rich, realistic history.

Used by the `seed_investor_demo` command (local only) and by the superuser-only admin button (which can
run in production). Safety rules, enforced here so every caller gets them:

* every demo profile is flagged `is_demo`; removal only ever deletes flagged demo profiles whose
  username is a known demo name AND whose email is @example.com;
* an existing non-demo user that collides with a demo username or email is never reused or modified;
* demo accounts are never emailed (see notifications / views / admin), and history is written with
  bulk_create so no signal can send anything.
"""
import logging
import time as clock  # `time` is already datetime.time in this file
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .models import (Document, Investment, InvestorProfile, NotificationLog, PayoutAccount, Transaction,
                     ValuationSnapshot, WithdrawalRequest, _withdrawal_reference)
from .passwords import generate_temporary_password
from .services import add_months, months_between, projected_value, value_on

logger = logging.getLogger(__name__)

PASSWORD = 'DemoPass-12345!'
TEMP_PASSWORD = 'Welcome-Temp-2026'   # for the account that must still change its password
TEST_ROUTING_JPM, TEST_ROUTING_BOA = '021000021', '026009593'  # real, valid ABA numbers used purely as test data

# Plan tuple: (name, principal, annual %, compounding, payout mode, currency,
#              share of the --years-back window, minimum months back, months until maturity (<=0 = already matured),
#              post monthly valuation snapshots?)
#
# Old withdrawals: (plan name, months ago, % of the plan's value that day, outcome, destination[, decline reason key]).
# Entries that would fall before their plan existed are skipped, so a shorter --years-back still works.
DECLINE_REASONS = {
    'EARLY': "Early withdrawal of this amount before the plan's maturity date is not permitted under its terms. "
             'You may request up to the interest accrued to date.',
    'VERIFY': 'We could not verify the destination account with you by phone. Please contact us to confirm your '
              'bank details, then submit a new request.',
    'LIMIT': 'The amount requested was more than was available on this investment on the day it was reviewed. '
             'A smaller request can be submitted.',
}
TYPED_BANKS = [('Chase', '021000021'), ('Wells Fargo', '026009593'), ('Citibank', '021000089')]  # valid ABA test numbers

INVESTORS = [
    dict(username='demo', email='demo.investor@example.com', name='Demo Investor', phone='+1 212 555 0148',
         plans=[
             ('Heritage Growth Plan', '150000', '9', 'annual', 'maturity', 'USD', 1.00, 6, 36, True),
             ('Quarterly Compounder', '100000', '11', 'quarterly', 'maturity', 'USD', 0.75, 6, 30, True),
             ('Income Plan', '120000', '7.5', 'simple', 'periodic', 'USD', 0.50, 6, 24, False),
             ('Euro Reserve', '40000', '6', 'annual', 'maturity', 'EUR', 0.30, 3, 24, False),
             ('Capital Preservation Note', '60000', '5.5', 'annual', 'maturity', 'USD', 1.00, 30, -6, False),
         ], accounts='demo', withdrawals='demo',
         old=[('Heritage Growth Plan', 62, 3, 'completed', 'typed'), ('Heritage Growth Plan', 50, 4, 'declined', 'offline', 'EARLY'),
              ('Quarterly Compounder', 44, 2.5, 'completed', 'saved'), ('Heritage Growth Plan', 36, 5, 'completed', 'saved'),
              ('Income Plan', 30, 6, 'declined', 'typed', 'VERIFY'), ('Capital Preservation Note', 24, 7, 'completed', 'typed'),
              ('Quarterly Compounder', 16, 3, 'declined', 'offline', 'LIMIT'), ('Heritage Growth Plan', 10, 4, 'completed', 'saved')],
         note='The fullest account: 5 plans, statements, accounts on file, and withdrawal requests in every status, old and new.'),
    dict(seed=False, username='demo-elena', email='elena.marsh@example.com', name='Elena Marsh', phone='+1 415 555 0192',
         plans=[
             ('Founders Circle Note', '300000', '10', 'annual', 'maturity', 'USD', 1.00, 12, 48, True),
             ('Atlantic Income Plan', '150000', '8', 'simple', 'periodic', 'USD', 0.70, 12, 30, False),
             ('Short-Term Bridge', '40000', '6.5', 'simple', 'maturity', 'USD', 0.50, 6, -3, False),
         ], accounts='elena', withdrawals='elena',
         old=[('Founders Circle Note', 60, 2, 'completed', 'saved'), ('Founders Circle Note', 48, 3, 'completed', 'saved'),
              ('Atlantic Income Plan', 38, 4, 'declined', 'offline', 'EARLY'), ('Short-Term Bridge', 30, 8, 'completed', 'typed'),
              ('Founders Circle Note', 22, 3, 'declined', 'typed', 'VERIFY'), ('Atlantic Income Plan', 12, 3, 'completed', 'saved')],
         note='Large USD portfolio with a matured plan; old paid and declined withdrawals plus a completed and an approved recent one.'),
    dict(seed=False, username='demo-astrid', email='astrid.lindqvist@example.com', name='Astrid Lindqvist', phone='+1 646 555 0107',
         plans=[
             ('Nordic Income Plan', '200000', '6', 'simple', 'periodic', 'EUR', 1.00, 9, 27, False),
             ('Euro Reserve', '150000', '5', 'annual', 'maturity', 'EUR', 0.80, 6, 30, True),
             ('Baltic Growth Note', '120000', '7', 'annual', 'maturity', 'EUR', 0.50, 6, 36, False),
         ], accounts=False, withdrawals=None,
         old=[('Nordic Income Plan', 58, 3, 'completed', 'typed'), ('Euro Reserve', 40, 4, 'declined', 'offline', 'EARLY'),
              ('Nordic Income Plan', 32, 3, 'completed', 'typed'), ('Baltic Growth Note', 20, 5, 'completed', 'offline'),
              ('Euro Reserve', 14, 3, 'declined', 'typed', 'LIMIT')],
         note='EUR investor with an income plan paying out quarterly and a long withdrawal history.'),
    dict(username='demo-chidi', email='chidi.okafor@example.com', name='Chidi Okafor', phone='+1 305 555 0166',
         password=TEMP_PASSWORD, must_change=True,
         plans=[
             ('Starter Growth Plan', '200000', '8', 'annual', 'maturity', 'USD', 1.00, 6, 18, True),
             ('Momentum Plan', '150000', '9', 'quarterly', 'maturity', 'USD', 0.70, 6, 30, False),
             ('Stability Note', '100000', '6', 'annual', 'maturity', 'USD', 0.50, 6, 24, False),
         ], accounts=False, withdrawals=None,
         old=[('Starter Growth Plan', 64, 2, 'completed', 'typed'), ('Momentum Plan', 46, 4, 'declined', 'offline', 'VERIFY'),
              ('Starter Growth Plan', 34, 3, 'completed', 'typed'), ('Stability Note', 24, 5, 'completed', 'offline'),
              ('Momentum Plan', 12, 3, 'declined', 'typed', 'EARLY')],
         note=f'Long-standing history, but set to a temporary password ({TEMP_PASSWORD}): shows the forced change at first sign-in.'),
    dict(username='colin-firth', email='colin.firth@example.com', name='Colin Firth', phone='+1 917 555 0121',
         since=date(2020, 1, 15),  # his main plan always starts in January 2020, whatever today's date is
         plans=[
             ('Firth Family Growth Plan', '550000', '9', 'annual', 'maturity', 'USD', 1.00, 6, 36, True),
             ('Cornerstone Income Note', '300000', '7.5', 'simple', 'periodic', 'USD', 0.80, 6, 30, True),
             ('Euro Reserve', '80000', '5', 'annual', 'maturity', 'EUR', 0.50, 6, 30, False),
         ], accounts='colin', withdrawals='colin',
         old=[('Firth Family Growth Plan', 74, 2, 'completed', 'saved'), ('Firth Family Growth Plan', 60, 3, 'completed', 'saved'), ('Firth Family Growth Plan', 46, 4, 'declined', 'offline', 'EARLY'),
              ('Cornerstone Income Note', 40, 3, 'completed', 'typed'), ('Firth Family Growth Plan', 30, 3, 'completed', 'saved'),
              ('Cornerstone Income Note', 24, 5, 'declined', 'typed', 'VERIFY'), ('Euro Reserve', 14, 4, 'completed', 'typed'),
              ('Firth Family Growth Plan', 9, 3, 'declined', 'offline', 'LIMIT')],
         note='Colin Firth: always created with the demo data. Over $1M, a record since January 2020, an account on file and open requests.'),
    dict(seed=False, username='demo-other', email='other.investor@example.com', name='Other Investor', phone='+1 202 555 0134',
         plans=[
             ('Private Plan', '400000', '8', 'annual', 'maturity', 'USD', 1.00, 6, 36, True),
             ('Reserve Plan', '150000', '7', 'annual', 'maturity', 'USD', 0.60, 6, 30, False),
         ], accounts=False, withdrawals=None,
         old=[('Private Plan', 62, 2, 'completed', 'typed'), ('Private Plan', 50, 3, 'declined', 'offline', 'LIMIT'),
              ('Private Plan', 36, 2.5, 'completed', 'typed'), ('Reserve Plan', 22, 4, 'declined', 'typed', 'VERIFY'),
              ('Private Plan', 14, 3, 'completed', 'offline')],
         note='Second account, for checking that investors cannot see each other.'),
]
# Only these are created when the demo data is seeded; the others stay available as styles for the admin's
# 'Fill with demo history' action. Fewer accounts means fewer database round trips per click.
SEEDED = [i for i in INVESTORS if i.get('seed', True)]
# PDF statements/agreements are not generated: each is a file upload to Supabase Storage, and the Demo data
# button has to finish inside one web request. Flip to True (tests do) to generate them again.
INCLUDE_DOCUMENTS = False
DEMO_USERNAMES = tuple(i['username'] for i in INVESTORS)
STYLES = {
    'demo': 'Mixed USD and EUR portfolio: 5 plans, accounts on file, withdrawals in every status',
    'demo-elena': 'Large USD portfolio with a matured plan and a completed withdrawal',
    'demo-astrid': 'EUR portfolio led by an income plan paying quarterly',
    'demo-chidi': 'USD growth portfolio of 3 plans',
    'colin-firth': 'USD/EUR portfolio with an income note, an account on file and open requests',
    'demo-other': 'USD private plans, 2 plans',
}
LEGACY_DEMO_USERNAMES = ('elena', 'astrid', 'chidi', 'other')  # names used before the demo- prefix existed
DEMO_EMAIL_DOMAIN = '@example.com'


def make_pdf(title, lines):
    """A small, valid one-page PDF (Helvetica). ASCII/Latin-1 only; the text is clearly marked DEMO."""
    def esc(s):
        s = s.encode('latin-1', 'replace').decode('latin-1')
        return s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    ops = ['BT', '/F1 16 Tf', '50 790 Td', f'({esc(title)}) Tj', '/F1 10 Tf', '0 -28 Td', '14 TL']
    for line in lines:
        ops.append(f'({esc(line)}) Tj')
        ops.append('T*')
    ops.append('ET')
    stream = '\n'.join(ops).encode('latin-1')
    objs = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
    ]
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % n + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1)
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)
    return bytes(out)


def quarter_ends(today, count):
    """The `count` most recent quarter-end dates on or before `today`, oldest first."""
    ends, year, q = [], today.year, (today.month - 1) // 3 + 1
    while len(ends) < count + 1:
        month = q * 3
        last = date(year, month, 1) + timedelta(days=31)
        last = date(last.year, last.month, 1) - timedelta(days=1)
        if last <= today:
            ends.append(last)
        q -= 1
        if q == 0:
            q, year = 4, year - 1
    return sorted(ends[:count])


def aware(d):
    return timezone.make_aware(datetime.combine(d, time(9, 0)))




FILE_WORKERS = 8  # parallel uploads/deletes; each is a network round trip when documents live in Supabase Storage


def _in_parallel(fn, items, swallow=None):
    """Run fn(item) for every item on a small thread pool. Touches no database connection, only
    file storage, so it is safe on any backend. With `swallow` set, per-item failures are logged
    instead of raised (used for cleanup, where one bad file must not block the rest)."""
    def guarded(item):
        try:
            return fn(item)
        except Exception:
            if swallow is None:
                raise
            logger.exception(swallow)
    with ThreadPoolExecutor(max_workers=FILE_WORKERS) as pool:
        return list(pool.map(guarded, items))


class DemoDataError(Exception):
    """Raised when demo data cannot be created safely (for example a real account uses a demo name)."""


class DemoSeeder:
    def __init__(self, years=6, random_passwords=False, today=None):
        self.years = years
        self.random_passwords = random_passwords
        self.today = today or date.today()
        self.credentials = []   # (username, display name, password, note) for every account created or reset
        self.elapsed = 0.0
        self.deadline = None
        self._used_refs = {}    # reference numbers already taken, loaded once instead of one query per row
        self._wr_batch, self._tx_batch = [], []

    # ── round-trip savers ────────────────────────────────────────────────────────────────────────
    def _check_time(self):
        """Stop cleanly (the whole run rolls back) before the web server would kill the worker."""
        if self.deadline is not None and clock.monotonic() > self.deadline:
            raise DemoDataError(
                'Stopped after taking too long, so nothing was created. The database is responding slowly or the '
                "server's request timeout is too short: make sure the Render Start Command includes --timeout 90, then try again.")

    def _reference(self, kind):
        """A fresh reference number, checked against a set loaded once (the model's own check is one query per row)."""
        if kind not in self._used_refs:
            model = Investment if kind == 'inv' else WithdrawalRequest
            self._used_refs[kind] = set(model.objects.values_list('reference', flat=True))
        while True:
            ref = Investment()._generate_reference() if kind == 'inv' else _withdrawal_reference()
            if ref not in self._used_refs[kind]:
                self._used_refs[kind].add(ref)
                return ref

    def _queue(self, wr, ledger_date=None):
        """Hold a withdrawal request (and its ledger row, if it was paid) for one batched insert."""
        wr.reference = self._reference('wr')
        wr._ledger_note = None
        if ledger_date is not None:
            wr._ledger_note = f'Withdrawal {wr.reference}'
            self._tx_batch.append(Transaction(investment=wr.investment, type='withdrawal', amount=wr.amount,
                                              date=ledger_date, note=wr._ledger_note))
        self._wr_batch.append(wr)

    def flush_withdrawals(self):
        txs, wrs = self._tx_batch, self._wr_batch
        self._tx_batch, self._wr_batch = [], []
        if txs:
            Transaction.objects.bulk_create(txs)  # bulk_create skips signals: history must never email anyone
            if any(t.pk is None for t in txs):    # a backend that can't return ids: look them up in one query
                found = {t.note: t for t in Transaction.objects.filter(type='withdrawal', note__in=[t.note for t in txs])}
                txs = [found[t.note] for t in txs]
        by_note = {t.note: t for t in txs}
        for wr in wrs:
            if wr._ledger_note:
                wr.transaction = by_note[wr._ledger_note]
        if wrs:
            WithdrawalRequest.objects.bulk_create(wrs)

    # ── public API ───────────────────────────────────────────────────────────────────────────────
    def run(self, reset=False, rebuild_custom=False):
        """Create (or top up) the demo accounts. Returns the number of investments created.

        `reset` deletes the named demo accounts' data first. `rebuild_custom` extends that to demo accounts
        that carry a superuser's own email (so an adopted account is rebuilt too)."""
        started = clock.monotonic()
        self.deadline = started + getattr(settings, 'DEMO_SEED_MAX_SECONDS', 75)
        try:
            with transaction.atomic():
                if reset or rebuild_custom:
                    self.clear_data(self.demo_profiles(any_email=rebuild_custom))
                return sum(self.seed_investor(spec) for spec in SEEDED)
        finally:
            self.elapsed = clock.monotonic() - started
            logger.info('Demo data run took %.1fs', self.elapsed)

    def clear_data(self, profiles=None):
        """Delete demo accounts' data but keep the accounts themselves (the five named ones by default)."""
        profiles = self.demo_profiles() if profiles is None else profiles
        WithdrawalRequest.objects.filter(investor__in=profiles).delete()
        PayoutAccount.objects.filter(investor__in=profiles).delete()
        NotificationLog.objects.filter(investor__in=profiles).delete()
        docs = list(Document.objects.filter(investor__in=profiles))
        _in_parallel(lambda d: d.file.storage.delete(d.file.name) if d.file else None, docs,
                     swallow='Could not delete a stored demo file')
        # The files are already gone, so don't let the per-document delete signal queue them all again.
        from django.db.models.signals import post_delete
        from .signals import document_removed
        post_delete.disconnect(document_removed, sender=Document)
        try:
            Document.objects.filter(pk__in=[d.pk for d in docs]).delete()
        finally:
            post_delete.connect(document_removed, sender=Document)
        Investment.objects.filter(investor__in=profiles).delete()

    def remove(self):
        """Delete every demo account AND its data. Returns {'investors': n}."""
        with transaction.atomic():
            profiles = self.demo_profiles()
            users = list(profiles.values_list('user_id', flat=True))
            self.clear_data()
            count = profiles.count()
            profiles.delete()
            get_user_model().objects.filter(pk__in=users).delete()
        return {'investors': count}

    @staticmethod
    def demo_profiles(any_email=False):
        """Flagged demo profiles that ALSO match a demo username and, normally, an example.com email.

        `any_email` additionally includes demo-flagged accounts a superuser gave their own email (for
        example the hand-adopted Colin Firth); only ever used when the superuser explicitly asks to rebuild."""
        names = list(DEMO_USERNAMES) + list(LEGACY_DEMO_USERNAMES)
        qs = InvestorProfile.objects.filter(is_demo=True, user__username__in=names).exclude(user__is_staff=True)
        return qs if any_email else qs.filter(user__email__iendswith=DEMO_EMAIL_DOMAIN)

    @staticmethod
    def exists():
        return DemoSeeder.demo_profiles().exists()

    # # ── one investor ─────────────────────────────────────────────────────────────────────────────
    def password_for(self, spec):
        if self.random_passwords:
            return generate_temporary_password()
        return spec.get('password', PASSWORD)

    def seed_investor(self, spec):
        User = get_user_model()
        user = User.objects.filter(username=spec['username']).first() or User.objects.filter(email__iexact=spec['email']).first()
        own_identity = False  # an existing demo account whose email/name/phone belong to the superuser who set it up
        if user is not None:
            profile = getattr(user, 'investor_profile', None)
            if profile is None or not profile.is_demo or user.is_staff:
                raise DemoDataError(
                    f'The username "{spec["username"]}" / email "{spec["email"]}" already belongs to a real account. '
                    'Demo data will not touch it.')
            own_identity = not user.email.lower().endswith(DEMO_EMAIL_DOMAIN)
        else:
            user = User(email=spec['email'])
        password = self.password_for(spec)
        if not own_identity:
            user.username, user.email = spec['username'], spec['email']
        user.is_staff = user.is_superuser = False
        user.set_password(password)
        user.save()
        profile, _ = InvestorProfile.objects.get_or_create(user=user, defaults={'display_name': spec['name']})
        if not own_identity:
            profile.display_name, profile.phone = spec['name'], spec['phone']
        profile.must_change_password, profile.is_demo, profile.is_active = spec.get('must_change', False), True, True
        profile.save()
        self.credentials.append((user.username, profile.display_name, password, spec.get('note', '')))

        if profile.investments.exists():
            return 0
        return self.populate(profile, spec)

    def populate(self, profile, spec):
        """Build the plans, ledger, withdrawals, valuations and documents described by `spec` for an
        existing demo profile. Never touches the profile's login, email or name."""
        self._check_time()
        investments = [self.make_plan(profile, spec, plan) for plan in spec['plans']]
        accounts = list(profile.payout_accounts.filter(is_verified=True)) or (
            self.make_accounts(profile, spec) if spec['accounts'] else [])
        if spec.get('withdrawals'):
            self.make_withdrawals(profile, investments, accounts, spec['withdrawals'])
        if spec.get('old'):
            self.make_old_withdrawals(profile, investments, accounts, spec['old'])
        self.flush_withdrawals()   # one batched insert; snapshots below read the ledger back
        for inv in investments:
            self._check_time()
            if inv.wants_snapshots:
                self.make_snapshots(inv)
        if INCLUDE_DOCUMENTS and spec.get('documents', True):
            fresh = list(Investment.objects.filter(pk__in=[i.pk for i in investments])
                         .prefetch_related('snapshots', 'transactions').order_by('pk'))
            self.make_documents(profile, fresh)
        self.make_notifications(profile)
        return len(investments)

    def fill_profile(self, profile, template='demo', replace=False):
        """Give an existing DEMO account a full multi-year history, styled like one of the five templates.

        Refuses real clients and staff outright. An account that already has data is left alone unless
        `replace` is set, in which case its investments, withdrawals, documents and accounts on file are
        deleted first. Returns the number of investments created."""
        spec = next((i for i in INVESTORS if i['username'] == template), None)
        if spec is None:
            raise DemoDataError(f'Unknown style "{template}".')
        if not profile.is_demo or profile.user.is_staff:
            raise DemoDataError(f'{profile.display_name} is not a demo account. Demo history is never added to real clients.')
        has_data = (profile.investments.exists() or profile.withdrawals.exists()
                    or profile.documents.exists())
        if has_data and not replace:
            raise DemoDataError(f'{profile.display_name} already has data. Choose "replace" to rebuild it, or leave it as it is.')
        self.deadline = clock.monotonic() + getattr(settings, 'DEMO_SEED_MAX_SECONDS', 75)
        with transaction.atomic():
            if has_data:
                self.clear_data(InvestorProfile.objects.filter(pk=profile.pk))
            return self.populate(profile, spec)

    def make_plan(self, profile, spec, plan):
        name, principal, rate, comp, payout, currency, share, min_back, beyond, snaps = plan
        months_back = max(min_back, round(self.years * 12 * share))
        start = add_months(self.today, -months_back)
        if spec.get('since') and share >= 1.0:
            start = spec['since']
            months_back = months_between(start, self.today)
        maturity = add_months(self.today, beyond)
        inv = Investment.objects.create(
            reference=self._reference('inv'), investor=profile, plan_name=name, principal=Decimal(principal), annual_rate=Decimal(rate),
            compounding=comp, payout_mode=payout, currency=currency, start_date=start, maturity_date=maturity,
            status='active' if beyond > 0 else 'matured',
        )
        rows = self.ledger(inv)
        Transaction.objects.bulk_create(rows)  # bulk_create skips signals: history must never email anyone
        inv.wants_snapshots = snaps  # posted after any withdrawals exist, so they are net of them
        return inv

    def make_snapshots(self, inv):
        """Monthly valuations, the way staff would post them: the total value of the position that day,
        AFTER any withdrawals already paid (see the conventions at the top of services.py)."""
        # One fresh copy with its ledger loaded once; every value below is then computed in memory
        # (re-reading per value was ~1,400 queries, far too slow over a network database).
        inv = Investment.objects.prefetch_related('snapshots', 'transactions').get(pk=inv.pk)
        months = months_between(inv.start_date, self.today)
        dates = [d for d in (add_months(inv.start_date, m) for m in range(1, months + 1))
                 if d <= min(self.today, inv.maturity_date)]
        values = [(d, value_on(inv, d)) for d in dates]  # computed first: no snapshots exist yet
        ValuationSnapshot.objects.bulk_create([ValuationSnapshot(investment=inv, date=d, value=v) for d, v in values])

    def ledger(self, inv):
        """Opening deposit, then interest credited each period (or paid out each quarter for income plans)."""
        rows = [Transaction(investment=inv, type='deposit', amount=inv.principal, date=inv.start_date, note='Initial deposit')]
        if inv.payout_mode == 'periodic':
            step, kind, label = 3, 'payout', 'interest payout'
            fixed = (inv.principal * inv.annual_rate / Decimal(100) / 4).quantize(Decimal('0.01'))
        elif inv.compounding == 'simple':
            return rows
        else:
            step = {'annual': 12, 'quarterly': 3, 'monthly': 1}[inv.compounding]
            kind, label, fixed = 'interest_credit', 'interest credited', None
        n, previous = 1, inv.principal
        while True:
            when = add_months(inv.start_date, n * step)
            if when > self.today or when > inv.maturity_date:
                break
            if fixed is not None:
                amount = fixed
            else:
                value = projected_value(inv, when)
                amount, previous = value - previous, value
            rows.append(Transaction(investment=inv, type=kind, amount=amount, date=when, note=f'Period {n} {label}'))
            n += 1
        return rows

    # ── accounts on file ─────────────────────────────────────────────────────────────────────────
    def make_accounts(self, profile, spec):
        specs = {
            'demo': [('Chase checking', 'Chase', 'Demo Investor', '123456789', TEST_ROUTING_JPM, True),
                     ('Savings (pending check)', 'Wells Fargo', 'Demo Investor', '88442266771', TEST_ROUTING_BOA, False)],
            'elena': [('Mercantile savings', 'Bank of America', 'Elena Marsh', '4402198765', TEST_ROUTING_BOA, True)],
            'colin': [('Family current account', 'Chase', 'Colin Firth', '7731048826', TEST_ROUTING_JPM, True)],
        }[spec['accounts']]
        accounts = []
        for label, bank, holder, number, routing, verified in specs:
            acct = PayoutAccount(investor=profile, label=label, bank_name=bank, holder_name=profile.display_name, is_verified=verified,
                                 verified_at=timezone.now() - timedelta(days=90) if verified else None)
            acct.set_numbers(number, routing)
            acct.save()
            accounts.append(acct)
        return accounts

    # ── withdrawals in every status ──────────────────────────────────────────────────────────────
    def make_withdrawals(self, profile, investments, accounts, which):
        by_name = {i.plan_name: i for i in investments}

        def request(inv, amount, status, days_ago, dest, **kw):
            wr = WithdrawalRequest(
                investor=profile, investment=inv, amount=Decimal(amount), currency=inv.currency, status=status,
                destination_type=dest, detected_country='US', submitted_at=timezone.now() - timedelta(days=days_ago), **kw)
            return wr

        def typed(wr, holder, bank, account, routing):
            wr.holder_name, wr.bank_name = holder, bank
            wr.set_typed_details(account, routing)
            wr.destination_label = f'{bank} ••••{account[-4:]}'
            return wr

        def saved(wr, acct):
            wr.payout_account, wr.destination_label = acct, f'{acct.label} ({acct.masked})'
            return wr

        if which == 'demo':
            heritage, quarterly = by_name['Heritage Growth Plan'], by_name['Quarterly Compounder']
            done = typed(request(heritage, '5000', 'completed', 41, 'typed', callback_verified=True,
                                 staff_note='Called investor on file number 14 Sept; confirmed.',
                                 decided_at=timezone.now() - timedelta(days=38)),
                         profile.display_name, 'Chase', '123456789', TEST_ROUTING_JPM)
            self._queue(done, ledger_date=self.today - timedelta(days=38))
            self._queue(request(quarterly, '20000', 'declined', 20, 'offline', destination_label='To be arranged with staff',
                                decline_reason="Early withdrawal of this amount before the plan's maturity date is not permitted "
                                               'under its terms. You may request up to the interest accrued to date.',
                                decided_at=timezone.now() - timedelta(days=18)))
            self._queue(typed(request(heritage, '2500', 'under_review', 4, 'typed'),
                              profile.display_name, 'Wells Fargo', '88442266771', TEST_ROUTING_BOA))
            self._queue(saved(request(quarterly, '1200', 'submitted', 1, 'saved'), accounts[0]))
            self._queue(request(heritage, '800', 'cancelled', 9, 'offline', destination_label='To be arranged with staff',
                                decided_at=timezone.now() - timedelta(days=9)))
        elif which == 'colin':
            growth, income = by_name['Firth Family Growth Plan'], by_name['Cornerstone Income Note']
            self._queue(saved(request(growth, '8000', 'submitted', 2, 'saved'), accounts[0]))
            self._queue(typed(request(income, '3500', 'approved', 7, 'typed', callback_verified=True,
                                      staff_note='Callback done; payment scheduled.'),
                              profile.display_name, 'Wells Fargo', '55120487391', TEST_ROUTING_BOA))
        elif which == 'elena':
            founders, bridge = by_name['Founders Circle Note'], by_name['Short-Term Bridge']
            self._queue(saved(request(founders, '15000', 'completed', 60, 'saved', callback_verified=True,
                                      decided_at=timezone.now() - timedelta(days=55)), accounts[0]),
                        ledger_date=self.today - timedelta(days=55))
            self._queue(typed(request(bridge, '12000', 'approved', 6, 'typed', callback_verified=True,
                                      staff_note='Callback done; payment scheduled.'),
                              profile.display_name, 'Bank of America', '4402198765', TEST_ROUTING_BOA))

    # ── years of earlier withdrawals, paid and declined ─────────────────────────────────────────
    def make_old_withdrawals(self, profile, investments, accounts, scenarios):
        by_name = {i.plan_name: i for i in investments}
        for n, (plan_name, months_ago, percent, outcome, dest, *reason) in enumerate(scenarios):
            inv = by_name.get(plan_name)
            when = add_months(self.today, -months_ago)
            if inv is None or when < add_months(inv.start_date, 2) or when > min(self.today, inv.maturity_date):
                continue  # the plan did not exist yet (short --years-back) or had already ended
            amount = max(Decimal(100), (projected_value(inv, when) * Decimal(str(percent)) / 100 / 100).quantize(Decimal('1')) * 100)
            dest = 'typed' if dest == 'saved' and not accounts else dest
            wr = WithdrawalRequest(
                investor=profile, investment=inv, amount=amount, currency=inv.currency, status=outcome,
                destination_type=dest, detected_country='US', submitted_at=aware(when - timedelta(days=6)),
                decided_at=aware(when), callback_verified=dest != 'saved' and outcome == 'completed',
            )
            if dest == 'saved':
                wr.payout_account = accounts[0]
                wr.destination_label = f'{accounts[0].label} ({accounts[0].masked})'
            elif dest == 'typed':
                bank, routing = TYPED_BANKS[n % len(TYPED_BANKS)]
                account = str(100000000 + 91357 * (n + 3) + 7 * len(profile.display_name))
                wr.holder_name, wr.bank_name = profile.display_name, bank
                wr.set_typed_details(account, routing)
                wr.destination_label = f'{bank} ••••{account[-4:]}'
            else:
                wr.destination_label = 'To be arranged with staff'
            if outcome == 'declined':
                wr.decline_reason = DECLINE_REASONS[reason[0]]
            self._queue(wr, ledger_date=when if outcome == 'completed' else None)

    # ── statements and agreements ────────────────────────────────────────────────────────────────
    def make_documents(self, profile, investments):
        earliest = min(i.start_date for i in investments)
        pending = []  # (Document, pdf bytes): uploaded in parallel below

        def add(title, kind, inv, when, pdf_title, lines):
            pdf = make_pdf(pdf_title, ['DEMONSTRATION DOCUMENT - fictional data, not a real statement.', ''] + lines)
            doc = Document(investor=profile, investment=inv, title=title, kind=kind, uploaded_at=min(aware(when), timezone.now()))
            pending.append((doc, pdf))

        for inv in investments:
            add(f'Investment Agreement - {inv.plan_name}', 'agreement', inv, inv.start_date,
                'Voss & Thorne LLP - Investment Agreement',
                [f'Investor: {profile.display_name}', f'Reference: {inv.reference}', f'Plan: {inv.plan_name}',
                 f'Principal: {inv.principal:,.2f} {inv.currency}', f'Annual rate: {inv.annual_rate}%',
                 f'Interest: {inv.get_compounding_display()}; payout {inv.get_payout_mode_display().lower()}',
                 f'Start date: {inv.start_date:%d %b %Y}', f'Maturity date: {inv.maturity_date:%d %b %Y}'])
        recent = quarter_ends(self.today, 4)
        year_ends = [date(y, 12, 31) for y in range(self.today.year - 7, self.today.year)
                     if date(y, 12, 31) >= earliest and date(y, 12, 31) < recent[0]]
        for ye in year_ends:
            lines = [f'Investor: {profile.display_name}', f'Statement date: {ye:%d %b %Y}', '']
            for inv in investments:
                if inv.start_date <= ye:
                    lines.append(f'{inv.plan_name} ({inv.reference}): {value_on(inv, ye):,.2f} {inv.currency}')
            add(f'{ye.year} Annual Statement', 'statement', None, ye + timedelta(days=14),
                f'Voss & Thorne LLP - Annual Statement {ye.year}', lines)
        for qe in recent:
            if qe < earliest:
                continue
            lines = [f'Investor: {profile.display_name}', f'Statement date: {qe:%d %b %Y}', '']
            for inv in investments:
                if inv.start_date <= qe:
                    lines.append(f'{inv.plan_name} ({inv.reference}): {value_on(inv, qe):,.2f} {inv.currency}')
            q = (qe.month - 1) // 3 + 1
            add(f'Q{q} {qe.year} Statement', 'statement', None, qe + timedelta(days=5),
                f'Voss & Thorne LLP - Quarterly Statement Q{q} {qe.year}', lines)
        _in_parallel(lambda item: item[0].file.save(f'{item[0].kind}.pdf', ContentFile(item[1]), save=False), pending)
        Document.objects.bulk_create([doc for doc, _ in pending])  # bulk_create skips signals, so no investor emails

    def make_notifications(self, profile):
        NotificationLog.objects.bulk_create([
            NotificationLog(investor=profile, kind=kind, success=True, sent_at=timezone.now() - timedelta(days=days))
            for kind, days in (('document', 40), ('valuation', 33), ('transaction', 12))
        ])
