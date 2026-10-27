import os
import random
import string
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .storage import document_storage
from .validators import validate_document


class InvestorProfile(models.Model):
    """Links a (non-staff) auth User to the investor portal."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='investor_profile',
    )
    display_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True, help_text='Untick to block portal access.')
    is_demo = models.BooleanField(
        default=False,
        help_text=('Superusers only. Tick for FICTIONAL demonstration accounts. While ticked: no emails are ever sent to this '
                   'investor, a "demonstration" banner is shown, and superusers may edit and delete everything about the '
                   'account (including withdrawal requests) and open its portal without the password. '
                   'NEVER tick this for a real client.'))
    must_change_password = models.BooleanField(
        default=False, help_text='Set while the investor still has a temporary password. They must choose their own at first sign-in.')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['display_name']

    def __str__(self):
        return self.display_name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.user_id and self.user.is_staff:
            raise ValidationError({'user': 'Staff accounts cannot be investors.'})


class Investment(models.Model):
    COMPOUNDING_CHOICES = [
        ('simple', 'Simple interest'),
        ('annual', 'Compounded annually'),
        ('quarterly', 'Compounded quarterly'),
        ('monthly', 'Compounded monthly'),
    ]
    PAYOUT_CHOICES = [
        ('maturity', 'At maturity'),
        ('periodic', 'Periodic payouts'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('matured', 'Matured'),
        ('closed', 'Closed'),
    ]

    investor = models.ForeignKey(InvestorProfile, on_delete=models.PROTECT, related_name='investments')
    reference = models.CharField(max_length=20, unique=True, blank=True)
    plan_name = models.CharField(max_length=150)
    principal = models.DecimalField(max_digits=16, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, default='USD')
    annual_rate = models.DecimalField(
        max_digits=6, decimal_places=3, validators=[MinValueValidator(Decimal('0'))],
        help_text='Annual rate as a percentage, e.g. 8.5',
    )
    compounding = models.CharField(max_length=10, choices=COMPOUNDING_CHOICES, default='annual')
    payout_mode = models.CharField(max_length=10, choices=PAYOUT_CHOICES, default='maturity')
    start_date = models.DateField()
    maturity_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f'{self.reference} — {self.plan_name}'

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.start_date and self.maturity_date and self.maturity_date <= self.start_date:
            raise ValidationError({'maturity_date': 'Maturity date must be after the start date.'})

    def _generate_reference(self):
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f'INV-{timezone.now().year}-{suffix}'

    def save(self, *args, **kwargs):
        if not self.reference:
            candidate = self._generate_reference()
            while Investment.objects.filter(reference=candidate).exists():
                candidate = self._generate_reference()
            self.reference = candidate
        super().save(*args, **kwargs)


class Transaction(models.Model):
    """Ledger of money actually moved. Source of truth for payouts."""
    TYPE_CHOICES = [
        ('deposit', 'Deposit'),
        ('interest_credit', 'Interest credit'),
        ('payout', 'Payout'),
        ('withdrawal', 'Withdrawal'),
    ]

    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, related_name='transactions')
    type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=16, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    date = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.investment.reference} {self.get_type_display()} {self.amount}'


class ValuationSnapshot(models.Model):
    """Staff-posted value of an investment on a given date (drives the charts)."""
    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, related_name='snapshots')
    date = models.DateField()
    value = models.DecimalField(max_digits=16, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(fields=['investment', 'date'], name='unique_snapshot_per_day'),
        ]

    def __str__(self):
        return f'{self.investment.reference} @ {self.date}: {self.value}'


def _document_path(instance, filename):
    # Random name: the original filename (often containing names or account
    # details) never becomes part of the storage key, and keys can't be guessed.
    ext = os.path.splitext(filename)[1].lower()
    return f'investor_documents/{instance.investor_id}/{uuid.uuid4().hex}{ext}'


class Document(models.Model):
    KIND_CHOICES = [
        ('statement', 'Statement'),
        ('agreement', 'Agreement'),
        ('other', 'Other'),
    ]

    investor = models.ForeignKey(InvestorProfile, on_delete=models.CASCADE, related_name='documents')
    investment = models.ForeignKey(
        Investment, on_delete=models.SET_NULL, null=True, blank=True, related_name='documents',
        help_text='Optional. Must belong to the same investor.',
    )
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='statement')
    file = models.FileField(upload_to=_document_path, storage=document_storage, validators=[validate_document])
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return self.title

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.investment_id and self.investor_id and self.investment.investor_id != self.investor_id:
            raise ValidationError({'investment': 'This investment belongs to a different investor.'})

    @property
    def download_name(self):
        from django.utils.text import slugify
        ext = os.path.splitext(self.file.name)[1].lower()
        return f'{slugify(self.title) or "document"}{ext}'


class NotificationLog(models.Model):
    """Record of portal notification emails; also drives the per-kind cooldown."""
    KIND_CHOICES = [
        ('valuation', 'Valuation posted'),
        ('transaction', 'Transaction recorded'),
        ('document', 'Document added'),
        ('manual', 'Sent by staff'),
    ]

    investor = models.ForeignKey(InvestorProfile, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    success = models.BooleanField(default=False)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-sent_at']
        indexes = [models.Index(fields=['investor', 'kind', 'sent_at'])]

    def __str__(self):
        return f'{self.investor} — {self.get_kind_display()} ({"sent" if self.success else "failed"})'


class PayoutAccount(models.Model):
    """A bank account staff have collected and verified for an investor. Numbers are stored encrypted."""
    investor = models.ForeignKey(InvestorProfile, on_delete=models.CASCADE, related_name='payout_accounts')
    label = models.CharField(max_length=80, help_text='Shown to the investor, e.g. "Chase checking".')
    bank_name = models.CharField(max_length=120)
    holder_name = models.CharField(max_length=150)
    account_number_enc = models.TextField(blank=True, editable=False)
    routing_number_enc = models.TextField(blank=True, editable=False)
    account_last4 = models.CharField(max_length=4, blank=True, editable=False)
    is_verified = models.BooleanField(default=False, help_text='Tick only after staff have verified this account with the investor.')
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['investor__display_name', 'label']

    def __str__(self):
        return f'{self.label} ({self.masked})'

    @property
    def masked(self):
        return f'••••{self.account_last4}' if self.account_last4 else '(no number)'

    def set_numbers(self, account_number, routing_number):
        from . import crypto
        self.account_number_enc = crypto.encrypt(account_number)
        self.routing_number_enc = crypto.encrypt(routing_number)
        self.account_last4 = account_number[-4:]


def _withdrawal_reference():
    return f'WD-{timezone.now().year}-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


class WithdrawalRequest(models.Model):
    """An investor asking staff to pay money out. Nothing moves automatically: staff review it,
    call the investor back, and pay it outside this system, then mark it completed."""
    STATUS_CHOICES = [
        ('submitted', 'Submitted'),
        ('under_review', 'Under review'),
        ('approved', 'Approved'),
        ('completed', 'Completed'),
        ('declined', 'Declined'),
        ('cancelled', 'Cancelled by investor'),
    ]
    OPEN_STATUSES = ('submitted', 'under_review', 'approved')
    TRANSITIONS = {
        'submitted': {'under_review', 'declined', 'cancelled'},
        'under_review': {'approved', 'declined', 'cancelled'},
        'approved': {'completed', 'declined'},
        'completed': set(), 'declined': set(), 'cancelled': set(),
    }
    DESTINATION_CHOICES = [
        ('saved', 'Account on file'),
        ('typed', 'Bank details entered by investor'),
        ('offline', 'Details to be arranged with staff'),
    ]

    reference = models.CharField(max_length=20, unique=True, blank=True)
    investor = models.ForeignKey(InvestorProfile, on_delete=models.PROTECT, related_name='withdrawals')
    investment = models.ForeignKey(Investment, on_delete=models.PROTECT, related_name='withdrawals')
    amount = models.DecimalField(max_digits=16, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3)

    destination_type = models.CharField(max_length=10, choices=DESTINATION_CHOICES)
    payout_account = models.ForeignKey(PayoutAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name='withdrawals')
    destination_label = models.CharField(max_length=200, blank=True, help_text='Masked description captured at submission.')
    holder_name = models.CharField(max_length=150, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    account_number_enc = models.TextField(blank=True, editable=False)
    routing_number_enc = models.TextField(blank=True, editable=False)
    account_last4 = models.CharField(max_length=4, blank=True, editable=False)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='submitted')
    decline_reason = models.TextField(blank=True, help_text='Shown to the investor. State the real reason.')
    staff_note = models.TextField(blank=True, help_text='Internal only. Never shown to the investor.')
    callback_verified = models.BooleanField(
        default=False, help_text='Tick after phoning the investor on the number we already hold and confirming this request.')
    detected_country = models.CharField(max_length=2, blank=True, editable=False)
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)
    decided_at = models.DateTimeField(null=True, blank=True, editable=False)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+', editable=False)
    transaction = models.OneToOneField(Transaction, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='withdrawal_request', editable=False)

    class Meta:
        ordering = ['-submitted_at']
        permissions = [('reveal_bank_details', 'Can reveal full bank details of withdrawal requests and payout accounts')]

    def __str__(self):
        return f'{self.reference} — {self.amount} {self.currency} ({self.get_status_display()})'

    def save(self, *args, **kwargs):
        if not self.reference:
            candidate = _withdrawal_reference()
            while WithdrawalRequest.objects.filter(reference=candidate).exists():
                candidate = _withdrawal_reference()
            self.reference = candidate
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def investor_can_cancel(self):
        return self.status in ('submitted', 'under_review')

    def set_typed_details(self, account_number, routing_number):
        from . import crypto
        self.account_number_enc = crypto.encrypt(account_number)
        self.routing_number_enc = crypto.encrypt(routing_number)
        self.account_last4 = account_number[-4:]
