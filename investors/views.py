from django.contrib.auth.views import LoginView
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import PasswordResetConfirmView
from django.db import transaction
from django.http import FileResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from . import emails, geo, impersonation, services, storage, throttle, withdrawals
from .decorators import investor_required
from .forms import InvestorAuthenticationForm, WithdrawalForm, is_portal_user
from .models import InvestorProfile, WithdrawalRequest


class InvestorLoginView(LoginView):
    template_name = 'investors/auth/login.html'
    authentication_form = InvestorAuthenticationForm
    redirect_authenticated_user = False

    def dispatch(self, request, *args, **kwargs):
        # Only skip the form for a real investor; a signed-in staff member
        # should still be able to see the login page.
        if request.user.is_authenticated and is_portal_user(request.user):
            return redirect('investors:home')
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        username = request.POST.get('username', '')
        if throttle.is_throttled(request, username):
            form = self.get_form()
            form.add_error(None, 'Too many failed attempts. Please wait 15 minutes and try again.')
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        throttle.clear(self.request, form.cleaned_data['username'])
        return super().form_valid(form)

    def form_invalid(self, form):
        throttle.record_failure(self.request, self.request.POST.get('username', ''))
        return super().form_invalid(form)

    def get_success_url(self):
        return self.get_redirect_url() or '/investors/'


def _chart_payload(series):
    """Turn {'actual': [(date, Decimal)], 'projected': [...]} into Chart.js arrays.

    Both datasets share one label axis; the gaps are None so each line only
    draws where it has data. Floats are used here, and only here, because the
    chart is display-only; all calculation stays in Decimal in services.py.
    """
    actual, projected = series['actual'], series['projected']
    labels = [d.isoformat() for d, _ in actual]
    labels += [d.isoformat() for d, _ in projected[1:]]
    a = [float(v) for _, v in actual] + [None] * max(len(projected) - 1, 0)
    p = [None] * max(len(actual) - 1, 0)
    if projected:
        p += [float(v) for _, v in projected]
    return {'labels': labels, 'actual': a, 'projected': p}


def _pick_currency(request, by_currency):
    codes = [c['currency'] for c in by_currency]
    wanted = request.GET.get('currency', '')
    if wanted in codes:
        return wanted
    # Default to the currency with the most money invested.
    return max(by_currency, key=lambda c: c['invested'])['currency'] if by_currency else None


@investor_required
def home(request):
    summary = services.portfolio_summary(request.investor)
    currency = _pick_currency(request, summary['by_currency'])
    totals = next((c for c in summary['by_currency'] if c['currency'] == currency), None)
    chart = None
    if currency:
        chart = _chart_payload(services.portfolio_series(request.investor, currency))
    rows = [r for r in summary['rows'] if r['investment'].currency == currency]
    return render(request, 'investors/home.html', {
        'investor': request.investor,
        'nav': 'dashboard',
        'currency': currency,
        'currencies': [c['currency'] for c in summary['by_currency']],
        'totals': totals,
        'rows': rows,
        'chart': chart,
    })


@investor_required
def investment_detail(request, reference):
    # Scoped through the investor's own relation: someone else's reference is a 404.
    inv = get_object_or_404(
        request.investor.investments.prefetch_related('snapshots', 'transactions'),
        reference=reference,
    )
    today = date.today()
    value = services.current_value(inv, today)
    withdrawn = services.withdrawn_total(inv, today)
    gain = value + withdrawn - inv.principal
    total_days = (inv.maturity_date - inv.start_date).days
    elapsed_days = (min(today, inv.maturity_date) - inv.start_date).days
    progress = max(0, min(100, round(elapsed_days / total_days * 100))) if total_days else 100
    return render(request, 'investors/investment.html', {
        'investor': request.investor,
        'nav': 'dashboard',
        'inv': inv,
        'value': value,
        'withdrawn': withdrawn,
        'gain': gain,
        'return_pct': services.pct(gain, inv.principal),
        'progress': progress,
        'days_left': max((inv.maturity_date - today).days, 0),
        'chart': _chart_payload(services.growth_series(inv, today)),
        'transactions': inv.transactions.all(),
        'documents': inv.documents.all(),
    })


@investor_required
def performance(request):
    summary = services.portfolio_summary(request.investor)
    currency = _pick_currency(request, summary['by_currency'])
    rows = services.yearly_returns(request.investor, currency=currency) if currency else []
    chart = {
        'labels': [str(r['year']) for r in rows],
        'gain': [float(r['gain']) for r in rows],
    }
    any_withdrawn = any(r['withdrawn'] for r in rows)
    return render(request, 'investors/performance.html', {
        'investor': request.investor,
        'nav': 'performance',
        'currency': currency,
        'currencies': [c['currency'] for c in summary['by_currency']],
        'rows': rows,
        'chart': chart,
        'any_withdrawn': any_withdrawn,
    })


@investor_required
def documents(request):
    return render(request, 'investors/documents.html', {
        'investor': request.investor,
        'nav': 'documents',
        'documents': request.investor.documents.select_related('investment'),
    })


@investor_required
@never_cache
def document_download(request, pk):
    # Ownership check: only through the signed-in investor's own relation.
    doc = get_object_or_404(request.investor.documents, pk=pk)
    if storage.using_supabase():
        url = doc.file.storage.url(
            doc.file.name,
            parameters={'ResponseContentDisposition': f'attachment; filename="{doc.download_name}"'},
            expire=storage.SIGNED_URL_SECONDS,
        )
        return HttpResponseRedirect(url)
    return FileResponse(doc.file.open('rb'), as_attachment=True, filename=doc.download_name)


def _safely(fn, *args):
    """Run a notification; a mail failure must never undo or hide a saved request."""
    try:
        fn(*args)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Withdrawal notification failed')


@investor_required
def withdrawals_list(request):
    allowed, _, _ = geo.withdrawal_location(request, request.investor)
    return render(request, 'investors/withdrawals.html', {
        'investor': request.investor,
        'nav': 'withdrawals',
        'requests': request.investor.withdrawals.select_related('investment'),
        'allowed': allowed,
        'blocked_message': geo.blocked_message(request.investor),
    })


@investor_required
def withdrawal_new(request):
    allowed, country, overridden = geo.withdrawal_location(request, request.investor)
    base = {'investor': request.investor, 'nav': 'withdrawals', 'blocked_message': geo.blocked_message(request.investor)}
    if not allowed:
        # Same message whether the country is known-and-foreign or could not be determined.
        return render(request, 'investors/withdrawal_new.html', {**base, 'allowed': False},
                      status=403 if request.method == 'POST' else 200)

    form = WithdrawalForm(request.investor, request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            user = request.user
            recent = request.investor.withdrawals.filter(
                submitted_at__gte=timezone.now() - timedelta(days=1)).count()
            if recent >= withdrawals.MAX_OPEN_REQUESTS_PER_DAY:
                form.add_error(None, 'You have submitted several requests today. Please wait before submitting another.')
            elif throttle.is_throttled(request, user.username):
                form.add_error('password', 'Too many failed attempts. Please wait 15 minutes and try again.')
            elif not user.check_password(form.cleaned_data['password']):
                throttle.record_failure(request, user.username)
                form.add_error('password', 'That password is not correct.')
            else:
                throttle.clear(request, user.username)
                wr = _create_withdrawal(request, form.cleaned_data, country, overridden)
                messages.success(request, f'Request {wr.reference} submitted. We will call you on the number we hold before anything is paid.')
                return redirect('investors:withdrawal_detail', pk=wr.pk)
    return render(request, 'investors/withdrawal_new.html', {
        **base, 'allowed': True, 'form': form, 'has_saved': form.fields['saved_account'].queryset.exists()})


def _create_withdrawal(request, data, country, overridden=False):
    inv, dest = data['investment'], data['destination']
    wr = WithdrawalRequest(
        investor=request.investor, investment=inv, amount=data['amount'], currency=inv.currency,
        destination_type=dest, detected_country=country or '', location_override=overridden,
    )
    if dest == 'saved':
        acct = data['saved_account']
        wr.payout_account = acct
        wr.destination_label = f'{acct.label} ({acct.masked})'
    elif dest == 'typed':
        wr.holder_name, wr.bank_name = data['holder_name'].strip(), data['bank_name'].strip()
        wr.set_typed_details(data['account_number'], data['routing_number'])
        wr.destination_label = f'{wr.bank_name} ••••{wr.account_last4}'
    else:
        wr.destination_label = 'To be arranged with staff'
    wr.save()
    if not request.investor.is_demo:  # demo accounts never email the investor or alert real staff
        transaction.on_commit(lambda: (
            _safely(emails.send_withdrawal_submitted_email, request.investor, wr),
            _safely(emails.send_withdrawal_staff_alert, wr),
        ))
    return wr


@investor_required
def withdrawal_detail(request, pk):
    wr = get_object_or_404(request.investor.withdrawals.select_related('investment'), pk=pk)
    return render(request, 'investors/withdrawal_detail.html', {
        'investor': request.investor, 'nav': 'withdrawals', 'wr': wr,
    })


@investor_required
def withdrawal_cancel(request, pk):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    wr = get_object_or_404(request.investor.withdrawals, pk=pk)
    error = withdrawals.transition_error(wr, 'cancelled')
    if error or not wr.investor_can_cancel:
        messages.error(request, 'This request can no longer be cancelled online. Please contact us.')
    else:
        withdrawals.apply_status(wr, 'cancelled', None)
        messages.success(request, f'Request {wr.reference} was cancelled.')
    return redirect('investors:withdrawal_detail', pk=wr.pk)


class InvestorPasswordResetConfirmView(PasswordResetConfirmView):
    """Choosing a password through a reset link also ends the temporary-password state."""

    def form_valid(self, form):
        response = super().form_valid(form)
        InvestorProfile.objects.filter(user=form.user).update(must_change_password=False)
        return response


@investor_required(allow_pending_password_change=True)
def change_password(request):
    """Voluntary password change, and the forced one on first sign-in with a temporary password."""
    profile, user = request.investor, request.user
    form = PasswordChangeForm(user, request.POST or None)
    form.fields['old_password'].label = 'Temporary password' if profile.must_change_password else 'Current password'
    if request.method == 'POST':
        if throttle.is_throttled(request, user.username):
            form.add_error('old_password', 'Too many failed attempts. Please wait 15 minutes and try again.')
        elif form.is_valid():
            if form.cleaned_data['new_password1'] == form.cleaned_data['old_password']:
                form.add_error('new_password1', 'Your new password must be different from the current one.')
            else:
                form.save()
                update_session_auth_hash(request, user)  # keep this session signed in
                throttle.clear(request, user.username)
                if profile.must_change_password:
                    profile.must_change_password = False
                    profile.save(update_fields=['must_change_password'])
                messages.success(request, 'Your password has been changed.')
                return redirect('investors:home')
        elif 'old_password' in form.errors:
            throttle.record_failure(request, user.username)
    return render(request, 'investors/change_password.html', {
        'investor': profile, 'nav': 'password', 'form': form, 'forced': profile.must_change_password,
    })


def return_to_admin(request):
    """POST from the demo banner: leave the demo investor's portal and sign back in as the superuser."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    admin_user = impersonation.stop(request)
    if admin_user is None:
        return redirect('investors:login')
    return redirect('admin:investors_investorprofile_changelist')
