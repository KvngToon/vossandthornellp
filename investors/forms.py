import re
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm

from . import throttle
from .emails import send_password_reset_email
from .models import InvestorProfile, WithdrawalRequest
from .withdrawals import available_to_withdraw, valid_account_number, valid_routing_number


def is_portal_user(user):
    profile = getattr(user, 'investor_profile', None)
    return bool(profile and profile.is_active and not user.is_staff)


class InvestorAuthenticationForm(AuthenticationForm):
    """Username login restricted to active, non-staff investors. Wrong password,
    unknown user and not-an-investor all give the same error, so accounts can't
    be probed. Usernames are stored lowercase and matched case-insensitively."""

    username = forms.CharField(
        label='Username', max_length=150,
        widget=forms.TextInput(attrs={'autofocus': True, 'autocomplete': 'username',
                                      'autocapitalize': 'none', 'spellcheck': 'false'}),
    )

    def clean_username(self):
        return self.cleaned_data['username'].strip().lower()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not is_portal_user(user):
            raise self.get_invalid_login_error()


class InvestorPasswordResetForm(PasswordResetForm):
    """Only active investors get a reset email; everyone else silently gets nothing."""

    def get_users(self, email):
        User = get_user_model()
        users = User.objects.filter(email__iexact=email, is_active=True, is_staff=False,
                                    investor_profile__is_active=True)
        return (u for u in users if u.has_usable_password())

    def save(self, *args, **kwargs):
        email = self.cleaned_data['email']
        if not throttle.allow_reset_email(email):
            return  # over the hourly limit; respond identically so nothing is revealed
        for user in self.get_users(email):
            send_password_reset_email(user)


USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{2,29}$')


class InvestorAddForm(forms.ModelForm):
    """Admin 'add investor' form: creates the login user alongside the profile."""
    username = forms.CharField(
        max_length=30,
        help_text='3-30 characters: letters, numbers, dot, dash or underscore. Stored in lowercase.',
    )
    email = forms.EmailField(help_text='Where the invite, password resets and notifications are sent.')

    class Meta:
        model = InvestorProfile
        fields = ('display_name', 'username', 'email', 'phone', 'is_active')

    def clean_username(self):
        username = self.cleaned_data['username'].strip().lower()
        if not USERNAME_RE.match(username):
            raise forms.ValidationError(
                'Use 3-30 characters: lowercase letters, numbers, dot, dash or underscore, starting with a letter or number.')
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('A user with this email already exists.')
        return email


class WithdrawalForm(forms.Form):
    """Investor-facing withdrawal request. Password re-entry is checked in the view (it needs the
    login throttle). Bank numbers are validated here and encrypted by the view before saving."""
    investment = forms.ModelChoiceField(queryset=None, empty_label='Choose an investment')
    amount = forms.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal('0.01'))
    destination = forms.ChoiceField(choices=WithdrawalRequest.DESTINATION_CHOICES, widget=forms.RadioSelect)
    saved_account = forms.ModelChoiceField(queryset=None, required=False, empty_label='Choose an account')
    holder_name = forms.CharField(max_length=150, required=False, label='Account holder name')
    bank_name = forms.CharField(max_length=120, required=False)
    account_number = forms.CharField(max_length=24, required=False, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}))
    routing_number = forms.CharField(max_length=16, required=False, label='Routing number (ABA)')
    password = forms.CharField(widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}), label='Your password')
    confirm = forms.BooleanField(label='I confirm that I am making this request myself and the details are correct.')

    def __init__(self, investor, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.investor = investor
        self.fields['investment'].queryset = investor.investments.filter(status__in=('active', 'matured'))
        accounts = investor.payout_accounts.filter(is_verified=True)
        self.fields['saved_account'].queryset = accounts
        if not accounts.exists():
            self.fields['destination'].choices = [c for c in WithdrawalRequest.DESTINATION_CHOICES if c[0] != 'saved']

    @staticmethod
    def _digits(value):
        return re.sub(r'[\s-]', '', value or '')

    def clean(self):
        data = super().clean()
        inv, amount, dest = data.get('investment'), data.get('amount'), data.get('destination')
        if inv and amount is not None:
            available = available_to_withdraw(inv)
            if amount > available:
                self.add_error('amount', f'The most you can request from this investment right now is {available:,.2f} {inv.currency}.')
        if dest == 'saved' and not data.get('saved_account'):
            self.add_error('saved_account', 'Choose one of the accounts we have on file.')
        if dest == 'typed':
            for f in ('holder_name', 'bank_name'):
                if not (data.get(f) or '').strip():
                    self.add_error(f, 'This field is required.')
            account, routing = self._digits(data.get('account_number')), self._digits(data.get('routing_number'))
            if not valid_account_number(account):
                self.add_error('account_number', 'Enter the account number using 4 to 17 digits.')
            if not valid_routing_number(routing):
                self.add_error('routing_number', 'That is not a valid 9-digit US routing number.')
            data['account_number'], data['routing_number'] = account, routing
        return data
