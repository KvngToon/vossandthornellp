"""Rules for withdrawal requests: how much can be requested, valid bank details, and the
status workflow. No money moves here; staff pay outside the system and mark it completed."""
import re
from datetime import date
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Transaction, WithdrawalRequest
from .services import current_value

ZERO = Decimal('0')
MAX_OPEN_REQUESTS_PER_DAY = 5


def available_to_withdraw(investment, today=None):
    """What can still be requested: current value (already net of paid withdrawals) less requests
    still in progress, and, for income plans, less the interest already paid out to the investor
    (the value counts that interest, but the money has already left the plan).

    Deliberately conservative: it never lets an investor request more than is left."""
    today = today or date.today()
    pending = investment.withdrawals.filter(status__in=WithdrawalRequest.OPEN_STATUSES) \
        .aggregate(s=Sum('amount'))['s'] or ZERO
    paid_out_interest = ZERO
    if investment.payout_mode == 'periodic':
        paid_out_interest = investment.transactions.filter(type='payout', date__lte=today) \
            .aggregate(s=Sum('amount'))['s'] or ZERO
    return max(current_value(investment, today) - pending - paid_out_interest, ZERO)


def valid_routing_number(value):
    """US ABA routing number: nine digits with the 3-7-1 checksum."""
    if not re.fullmatch(r'\d{9}', value or ''):
        return False
    d = [int(c) for c in value]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def valid_account_number(value):
    return bool(re.fullmatch(r'\d{4,17}', value or ''))


def transition_error(request_obj, new_status):
    """Why `request_obj` may not move to `new_status`, or None if it may."""
    old = request_obj.status
    if new_status == old:
        return None
    if new_status not in WithdrawalRequest.TRANSITIONS.get(old, set()):
        return (f'A request that is "{request_obj.get_status_display()}" cannot be moved to '
                f'"{dict(WithdrawalRequest.STATUS_CHOICES).get(new_status, new_status)}".')
    if new_status == 'declined' and not request_obj.decline_reason.strip():
        return 'Give the investor the real reason for declining before saving.'
    if new_status in ('approved', 'completed') and request_obj.destination_type != 'saved' \
            and not request_obj.callback_verified:
        return ('Call the investor back on the number we already hold, confirm this request, '
                'then tick "callback verified" before approving or completing it.')
    return None


def apply_status(request_obj, new_status, staff_user):
    """Move to `new_status` (already validated). Completing records the withdrawal on the ledger.

    The ledger row is written with bulk_create so it doesn't also fire the generic
    'transaction recorded' email; the investor gets the withdrawal-update email instead."""
    with db_transaction.atomic():
        request_obj.status = new_status
        request_obj.decided_by = staff_user
        request_obj.decided_at = timezone.now()
        if new_status == 'completed' and request_obj.transaction_id is None:
            tx = Transaction(investment=request_obj.investment, type='withdrawal', amount=request_obj.amount,
                             date=date.today(), note=f'Withdrawal {request_obj.reference}')
            Transaction.objects.bulk_create([tx])
            if tx.pk is None:  # backend can't return ids from bulk_create
                tx = Transaction.objects.filter(investment=request_obj.investment, type='withdrawal',
                                                note=tx.note).latest('id')
            request_obj.transaction = tx
        request_obj.save()
