"""CSV import of an investor's REAL historical transactions and valuations.

Pure parse + validate, no writes except in `commit`. Everything is checked
before anything is saved, and `commit` is all-or-nothing.

Columns (header row required, extra columns ignored):
    investment  Investment reference, e.g. INV-2024-AB12CD
    date        YYYY-MM-DD (ISO only, so 03/04/2024 can't be misread)
    type        deposit | interest_credit | payout | withdrawal | valuation
    amount      Positive amount, up to 2 decimals (commas allowed: 1,250.50).
                For `valuation` rows this is the total value on that date, after any
                withdrawals already paid out.
    note        Optional text, max 255 characters
"""
import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import Investment, Transaction, ValuationSnapshot

MAX_ROWS = 2000
MAX_BYTES = 1024 * 1024
REQUIRED = ('investment', 'date', 'type', 'amount')
TRANSACTION_TYPES = {t for t, _ in Transaction.TYPE_CHOICES}
ALL_TYPES = TRANSACTION_TYPES | {'valuation'}
AMOUNT_RE = re.compile(r'^\d{1,14}(\.\d{1,2})?$')

SAMPLE = (
    'investment,date,type,amount,note\n'
    'INV-2024-AB12CD,2024-01-15,deposit,50000.00,Initial deposit\n'
    'INV-2024-AB12CD,2025-01-15,interest_credit,4500.00,Year 1 interest\n'
    'INV-2024-AB12CD,2025-01-31,valuation,54500.00,January statement\n'
)


class ParseResult:
    def __init__(self):
        self.rows = []     # validated dicts, ready to save
        self.errors = []   # (line_number, message)

    @property
    def ok(self):
        return bool(self.rows) and not self.errors


def parse(text, today=None):
    today = today or date.today()
    result = ParseResult()
    text = text.lstrip('﻿')
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.errors.append((1, 'The file is empty.'))
        return result
    reader.fieldnames = [(h or '').strip().lower() for h in reader.fieldnames]
    missing = [c for c in REQUIRED if c not in reader.fieldnames]
    if missing:
        result.errors.append((1, f'Missing required column(s): {", ".join(missing)}.'))
        return result

    raw = []
    for line, row in enumerate(reader, start=2):
        if not any((v or '').strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        if len(raw) >= MAX_ROWS:
            result.errors.append((line, f'Too many rows (maximum {MAX_ROWS} per file).'))
            break
        raw.append((line, {k: (v or '').strip() if isinstance(v, str) else '' for k, v in row.items() if k}))

    refs = {r['investment'] for _, r in raw if r['investment']}
    investments = {i.reference: i for i in Investment.objects.filter(reference__in=refs)}
    existing_tx = set(Transaction.objects.filter(investment__in=investments.values())
                      .values_list('investment__reference', 'type', 'amount', 'date'))
    existing_val = set(ValuationSnapshot.objects.filter(investment__in=investments.values())
                       .values_list('investment__reference', 'date'))
    seen = set()

    for line, r in raw:
        errs = []
        inv = investments.get(r['investment'])
        if not r['investment']:
            errs.append('investment reference is blank')
        elif inv is None:
            errs.append(f'unknown investment "{r["investment"]}"')

        when = None
        try:
            when = datetime.strptime(r['date'], '%Y-%m-%d').date()
            if when > today:
                errs.append('date is in the future')
            elif inv and when < inv.start_date:
                errs.append(f'date is before the investment started ({inv.start_date})')
        except ValueError:
            errs.append(f'date "{r["date"]}" is not YYYY-MM-DD')

        kind = r['type'].lower()
        if kind not in ALL_TYPES:
            errs.append(f'type "{r["type"]}" must be one of: {", ".join(sorted(ALL_TYPES))}')

        amount = None
        cleaned = r['amount'].replace(',', '')
        if not AMOUNT_RE.match(cleaned):
            errs.append(f'amount "{r["amount"]}" must be a number with at most 2 decimals (no currency symbols)')
        else:
            amount = Decimal(cleaned)
            if amount <= 0 and not (kind == 'valuation' and amount == 0):
                errs.append('amount must be greater than zero')

        note = r.get('note', '')
        if len(note) > 255:
            errs.append('note is longer than 255 characters')

        if not errs and inv and when and amount is not None:
            key = (inv.reference, when) if kind == 'valuation' else (inv.reference, kind, amount, when)
            if key in seen:
                errs.append('duplicate of an earlier row in this file')
            elif (kind == 'valuation' and key in existing_val) or (kind != 'valuation' and key in existing_tx):
                errs.append('already recorded for this investment (nothing imported twice)')
            seen.add(key)

        if errs:
            result.errors.append((line, '; '.join(errs)))
        else:
            result.rows.append({'line': line, 'investment': inv, 'date': when, 'type': kind,
                                'amount': amount, 'note': note})
    if not raw and not result.errors:
        result.errors.append((1, 'The file has a header but no data rows.'))
    return result


def commit(rows):
    """Save validated rows atomically. bulk_create skips post_save signals on
    purpose: imported history must never email investors. Returns (tx, valuations)."""
    txs = [Transaction(investment=r['investment'], type=r['type'], amount=r['amount'], date=r['date'], note=r['note'])
           for r in rows if r['type'] != 'valuation']
    vals = [ValuationSnapshot(investment=r['investment'], date=r['date'], value=r['amount'])
            for r in rows if r['type'] == 'valuation']
    with transaction.atomic():
        Transaction.objects.bulk_create(txs)
        ValuationSnapshot.objects.bulk_create(vals)
    return len(txs), len(vals)
