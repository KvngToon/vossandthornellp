from types import SimpleNamespace

from django import forms
from django.contrib.admin.widgets import AdminSplitDateTime
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import crypto, geo, impersonation, importer, withdrawals
from .demo_data import STYLES, DemoDataError, DemoSeeder
from .passwords import generate_temporary_password
from .emails import send_invite_email, send_withdrawal_update_email
from .forms import InvestorAddForm
from .models import (Document, Investment, InvestorProfile, NotificationLog, PayoutAccount, Transaction,
                     ValuationSnapshot, WithdrawalRequest)
from .notifications import notify


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0


class SnapshotInline(admin.TabularInline):
    model = ValuationSnapshot
    extra = 0

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'value':
            field.help_text = ('Total value of the investment on this date, AFTER any withdrawals already paid out '
                               '(interest included, paid out or not).')
        return field


class DemoDeleteMixin:
    """Superusers may delete DEMO investors/investments together with everything hanging off them,
    including withdrawal requests that the real-money PROTECT rules would otherwise block. Real
    clients keep the protection, superuser or not."""

    def _is_demo(self, obj):
        raise NotImplementedError

    def _purge(self, obj):
        raise NotImplementedError

    def _all_demo(self, request, objs):
        return request.user.is_superuser and all(self._is_demo(o) for o in objs)

    def get_deleted_objects(self, objs, request):
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        if self._all_demo(request, list(objs)):
            protected = []
        return to_delete, model_count, perms_needed, protected

    def delete_model(self, request, obj):
        if self._all_demo(request, [obj]):
            self._purge(obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        objs = list(queryset)
        if self._all_demo(request, objs):
            for obj in objs:
                self._purge(obj)
        super().delete_queryset(request, queryset)


@admin.register(InvestorProfile)
class InvestorProfileAdmin(DemoDeleteMixin, admin.ModelAdmin):
    list_display = ('display_name', 'user', 'email', 'is_demo', 'is_active', 'created_at')
    list_filter = ('is_active', 'is_demo')
    change_list_template = 'admin/investors/investorprofile/change_list.html'
    search_fields = ('display_name', 'user__email', 'user__username')
    readonly_fields = ('user',)
    actions = ['resend_invite', 'send_update_email', 'sign_in_as_demo_investor', 'fill_with_demo_history']
    FILL_PHRASE = 'FILL DEMO HISTORY'

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:  # demo tools are for superusers only
            for name in ('sign_in_as_demo_investor', 'fill_with_demo_history'):
                actions.pop(name, None)
        return actions

    def _is_demo(self, obj):
        return obj.is_demo

    def _purge(self, profile):
        WithdrawalRequest.objects.filter(investor=profile).delete()
        Investment.objects.filter(investor=profile).delete()

    @admin.display(description='Email')
    def email(self, obj):
        return obj.user.email

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')

    # ── Demo data (superusers only) ──────────────────────────────────────────────────────────────
    CREATE_PHRASE, REMOVE_PHRASE = 'CREATE DEMO DATA', 'REMOVE DEMO DATA'

    def get_urls(self):
        custom = [path('demo-data/', self.admin_site.admin_view(self.demo_data_view), name='investors_investorprofile_demo_data')]
        return custom + super().get_urls()

    def _log_demo(self, request, message):
        LogEntry.objects.create(
            user=request.user, content_type=ContentType.objects.get_for_model(InvestorProfile), object_id=None,
            object_repr='Demo data', action_flag=CHANGE, change_message=message)

    def demo_data_view(self, request):
        """Create or remove the FICTIONAL demo accounts. Works in production, so it is deliberately
        hard to trigger by accident: superusers only, a typed confirmation, one-time random passwords."""
        if not request.user.is_superuser:
            raise PermissionDenied
        context = {**self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'Demo data',
                   'create_phrase': self.CREATE_PHRASE, 'remove_phrase': self.REMOVE_PHRASE}
        if request.method == 'POST':
            action, typed = request.POST.get('action'), request.POST.get('confirm', '').strip()
            if action == 'create':
                try:
                    years = int(request.POST.get('years', '6'))
                except ValueError:
                    years = 0
                if typed != self.CREATE_PHRASE:
                    context['error'] = f'Type {self.CREATE_PHRASE} exactly to confirm.'
                elif not 1 <= years <= 10:
                    context['error'] = 'Years back must be between 1 and 10.'
                else:
                    seeder = DemoSeeder(years=years, random_passwords=True)
                    rebuild = request.POST.get('rebuild') == 'on'
                    try:
                        made = seeder.run(rebuild_custom=rebuild)
                    except DemoDataError as exc:
                        context['error'] = str(exc)
                    except ImproperlyConfigured as exc:
                        # e.g. FIELD_ENCRYPTION_KEY missing: the whole attempt rolled back, nothing was created
                        context['error'] = f'Nothing was created. Server setting missing: {exc}'
                    else:
                        self._log_demo(request, f'Created demo data ({made} new investment(s), {years} year(s) back{", rebuilding existing demo accounts" if rebuild else ""}).')
                        context.update(created=True, made=made, credentials=seeder.credentials, elapsed=round(seeder.elapsed, 1))
            elif action == 'remove':
                if typed != self.REMOVE_PHRASE:
                    context['error'] = f'Type {self.REMOVE_PHRASE} exactly to confirm.'
                else:
                    result = DemoSeeder().remove()
                    self._log_demo(request, f'Removed demo data ({result["investors"]} demo investor(s)).')
                    context['removed'] = result['investors']
        context['exists'] = DemoSeeder.exists()
        response = TemplateResponse(request, 'admin/investors/investorprofile/demo_data.html', context)
        response['Cache-Control'] = 'no-store'  # the page can contain one-time passwords
        return response

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs['form'] = InvestorAddForm
            kwargs['fields'] = InvestorAddForm.Meta.fields
        return super().get_form(request, obj, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            fields.append('is_demo')  # only superusers may switch demo mode on or off
        return fields

    def get_fields(self, request, obj=None):
        if obj is None:
            return InvestorAddForm.Meta.fields
        return ('user', 'display_name', 'phone', 'is_active', 'must_change_password', 'is_demo')

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        email = form.cleaned_data['email']
        temp = generate_temporary_password()
        user = get_user_model().objects.create_user(username=form.cleaned_data['username'], email=email, password=temp)
        obj.user = user
        obj.must_change_password = True
        super().save_model(request, obj, form, change)
        self._report_credentials(request, user, obj, temp, created=True)

    def _report_credentials(self, request, user, profile, temp, created):
        """Email the temporary password AND show it to staff once, so it can be passed on another
        way if the email doesn't arrive. It is stored only as a hash and can't be shown again."""
        sent = send_invite_email(user, profile, temp)
        lead = 'Investor created.' if created else f'New temporary password for {profile.display_name}.'
        tail = (f'Emailed to {user.email}.' if sent else
                f'The email to {user.email} could NOT be sent (check RESEND_API_KEY), so give them these details yourself.')
        self.message_user(
            request,
            format_html('{} Username: <strong>{}</strong> &nbsp; Temporary password: <strong style="font-family:monospace">{}</strong>. '
                        'Shown only now; they must change it at first sign-in. {}', lead, user.username, temp, tail),
            messages.SUCCESS if sent else messages.WARNING,
        )

    @admin.action(description='Generate a new temporary password and send it (signs the investor out)')
    def resend_invite(self, request, queryset):
        count = 0
        for profile in queryset.select_related('user'):
            if not profile.is_active:
                continue
            temp = generate_temporary_password()
            profile.user.set_password(temp)  # also invalidates their current sessions
            profile.user.save()
            profile.must_change_password = True
            profile.save(update_fields=['must_change_password'])
            self._report_credentials(request, profile.user, profile, temp, created=False)
            count += 1
        if not count:
            self.message_user(request, 'No active investors were selected.', messages.WARNING)

    @admin.action(description='Open the portal as this demo investor (superusers only)')
    def sign_in_as_demo_investor(self, request, queryset):
        """Superuser convenience: step into a demo investor's portal without knowing its password.
        Demo accounts only, one at a time. The admin session is swapped for the demo investor's, and a
        banner button brings the superuser straight back."""
        if not request.user.is_superuser:
            raise PermissionDenied
        profiles = list(queryset.select_related('user'))
        if len(profiles) != 1:
            self.message_user(request, 'Select exactly one demo investor.', messages.WARNING)
            return None
        profile = profiles[0]
        if not profile.is_demo or not profile.is_active or profile.user.is_staff:
            self.message_user(request, 'Only active demo investors can be opened this way. Real clients never can.', messages.ERROR)
            return None
        self.log_change(request, profile, 'Opened the portal as this demo investor.')
        impersonation.start(request, profile.user)
        return redirect('investors:home')

    @admin.action(description='Fill with multi-year demo history (demo accounts only, superusers)')
    def fill_with_demo_history(self, request, queryset):
        """Give selected DEMO accounts the full six-year history (plans, ledger, valuations, statements,
        paid and declined withdrawals). Real clients are refused outright; an account that already has
        data is skipped unless the superuser explicitly chooses to replace it."""
        if not request.user.is_superuser:
            raise PermissionDenied
        profiles = list(queryset.select_related('user'))
        real = [p.display_name for p in profiles if not p.is_demo or p.user.is_staff]
        if real:
            self.message_user(request, f'Nothing was changed. Not a demo account: {", ".join(real)}. '
                                       'This tool only ever works on demo accounts.', messages.ERROR)
            return None
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'Fill with demo history',
            'rows': [{'profile': p, 'investments': p.investments.count(), 'withdrawals': p.withdrawals.count(),
                      'documents': p.documents.count()} for p in profiles],
            'styles': STYLES, 'phrase': self.FILL_PHRASE, 'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'ids': [p.pk for p in profiles], 'style': request.POST.get('style', 'demo'), 'years': request.POST.get('years', '6'),
        }
        if 'apply' not in request.POST:
            return TemplateResponse(request, 'admin/investors/investorprofile/fill_history.html', context)
        replace, style = request.POST.get('replace') == 'on', request.POST.get('style', 'demo')
        try:
            years = int(request.POST.get('years', '6'))
        except ValueError:
            years = 0
        if request.POST.get('confirm', '').strip() != self.FILL_PHRASE:
            context['error'] = f'Type {self.FILL_PHRASE} exactly to confirm.'
        elif not 1 <= years <= 10:
            context['error'] = 'Years back must be between 1 and 10.'
        elif style not in STYLES:
            context['error'] = 'Choose one of the listed styles.'
        if context.get('error'):
            return TemplateResponse(request, 'admin/investors/investorprofile/fill_history.html', context)
        done, skipped = [], []
        for profile in profiles:
            try:
                made = DemoSeeder(years=years).fill_profile(profile, template=style, replace=replace)
            except DemoDataError as exc:
                skipped.append(str(exc))
                continue
            self.log_change(request, profile, f'Filled with demo history (style {style}, {years} year(s), '
                                              f'{"replaced existing data" if replace else "no existing data"}).')
            done.append(f'{profile.display_name} ({made} plans)')
        if done:
            self.message_user(request, 'Filled: ' + ', '.join(done) + '. Their logins are unchanged.', messages.SUCCESS)
        for text in skipped:
            self.message_user(request, text, messages.WARNING)
        return None

    @admin.action(description='Email selected investors that their portfolio has been updated')
    def send_update_email(self, request, queryset):
        sent = sum(1 for p in queryset.select_related('user') if notify(p, 'manual', respect_cooldown=False))
        self.message_user(request, f'{sent} update email(s) sent.', messages.SUCCESS if sent else messages.WARNING)


@admin.register(Investment)
class InvestmentAdmin(DemoDeleteMixin, admin.ModelAdmin):
    list_display = ('reference', 'investor', 'plan_name', 'principal', 'currency', 'annual_rate', 'status', 'maturity_date')
    list_filter = ('status', 'compounding', 'payout_mode')
    search_fields = ('reference', 'plan_name', 'investor__display_name')
    readonly_fields = ('reference',)
    inlines = [TransactionInline, SnapshotInline]
    change_list_template = 'admin/investors/investment/change_list.html'

    def _is_demo(self, obj):
        return obj.investor.is_demo

    def _purge(self, investment):
        WithdrawalRequest.objects.filter(investment=investment).delete()

    def get_urls(self):
        custom = [path('import-history/', self.admin_site.admin_view(self.import_history_view),
                       name='investors_investment_import_history')]
        return custom + super().get_urls()

    def import_history_view(self, request):
        """Two steps: upload -> preview (nothing saved) -> confirm (all-or-nothing save)."""
        if not (self.has_change_permission(request) and self.has_add_permission(request)):
            raise PermissionDenied
        if request.GET.get('sample'):
            resp = HttpResponse(importer.SAMPLE, content_type='text/csv')
            resp['Content-Disposition'] = 'attachment; filename="investor-history-sample.csv"'
            return resp

        context = {**self.admin_site.each_context(request), 'opts': self.model._meta,
                   'title': 'Import history from CSV', 'max_rows': importer.MAX_ROWS}
        if request.method == 'POST':
            confirming = 'csv_text' in request.POST
            if confirming:
                text = request.POST['csv_text']
            else:
                upload = request.FILES.get('file')
                text = None
                if upload is None:
                    context['upload_error'] = 'Choose a CSV file first.'
                elif upload.size > importer.MAX_BYTES:
                    context['upload_error'] = 'File is too large (maximum 1 MB).'
                else:
                    try:
                        text = upload.read().decode('utf-8-sig')
                    except UnicodeDecodeError:
                        context['upload_error'] = 'The file is not UTF-8 text. Re-save it as "CSV UTF-8".'
            if text is not None and len(text.encode('utf-8')) <= importer.MAX_BYTES:
                result = importer.parse(text)
                if confirming and result.ok:
                    n_tx, n_val = importer.commit(result.rows)
                    for inv in {r['investment'] for r in result.rows}:
                        count = sum(1 for r in result.rows if r['investment'] == inv)
                        self.log_change(request, inv, f'Imported {count} history row(s) from CSV.')
                    self.message_user(request, f'Imported {n_tx} transaction(s) and {n_val} valuation(s). '
                                               'No investor emails were sent.', messages.SUCCESS)
                    return redirect('admin:investors_investment_changelist')
                context.update(result=result, csv_text=text, preview_rows=result.rows[:100],
                               error_rows=result.errors[:100], more_rows=max(len(result.rows) - 100, 0),
                               more_errors=max(len(result.errors) - 100, 0))
        return TemplateResponse(request, 'admin/investors/investment/import_history.html', context)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ('title', 'investor', 'kind', 'uploaded_at')
    list_filter = ('kind',)
    search_fields = ('title', 'investor__display_name')


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('sent_at', 'investor', 'kind', 'success')
    list_filter = ('kind', 'success')
    date_hierarchy = 'sent_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ── Bank details: encrypted at rest, revealed to authorised staff only, every reveal logged ──────────

REVEAL_PERM = 'investors.reveal_bank_details'


class RevealMixin:
    """Adds `<pk>/reveal/`: GET asks for confirmation (shows nothing secret), POST shows the full
    details once and writes an entry to the admin history. Requires the reveal permission."""
    change_form_template = 'admin/investors/reveal_change_form.html'

    def get_urls(self):
        name = f'{self.model._meta.app_label}_{self.model._meta.model_name}_reveal'
        custom = [path('<path:object_id>/reveal/', self.admin_site.admin_view(self.reveal_view), name=name)]
        return custom + super().get_urls()

    def _reveal_name(self):
        return f'admin:{self.model._meta.app_label}_{self.model._meta.model_name}_reveal'

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = dict(extra_context or {})
        if request.user.has_perm(REVEAL_PERM):
            extra_context['reveal_url'] = reverse(self._reveal_name(), args=[object_id])
        return super().change_view(request, object_id, form_url, extra_context)

    def bank_details(self, obj):
        """(holder, bank, account_number, routing_number) or None. Overridden per model."""
        raise NotImplementedError

    def reveal_view(self, request, object_id):
        if not request.user.has_perm(REVEAL_PERM):
            raise PermissionDenied
        obj = self.get_object(request, object_id)
        if obj is None:
            raise PermissionDenied
        context = {**self.admin_site.each_context(request), 'opts': self.model._meta, 'original': obj,
                   'title': 'Reveal bank details'}
        if request.method == 'POST':
            try:
                details = self.bank_details(obj)
            except ValueError as exc:
                details, context['error'] = None, str(exc)
            self.log_change(request, obj, 'Revealed full bank details.')
            context.update(revealed=True, details=details)
        response = TemplateResponse(request, 'admin/investors/reveal_bank_details.html', context)
        response['Cache-Control'] = 'no-store'
        return response


class PayoutAccountForm(forms.ModelForm):
    account_number = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}),
                                     help_text='Leave blank to keep the stored number. Stored encrypted.')
    routing_number = forms.CharField(required=False, help_text='9-digit US routing number. Leave blank to keep the stored one.')

    class Meta:
        model = PayoutAccount
        fields = ('investor', 'label', 'bank_name', 'holder_name', 'account_number', 'routing_number', 'is_verified')

    def clean(self):
        data = super().clean()
        account = (data.get('account_number') or '').replace(' ', '').replace('-', '')
        routing = (data.get('routing_number') or '').replace(' ', '').replace('-', '')
        if not self.instance.pk and not account:
            self.add_error('account_number', 'Required when adding an account.')
        if account and not withdrawals.valid_account_number(account):
            self.add_error('account_number', 'Use 4 to 17 digits.')
        if routing and not withdrawals.valid_routing_number(routing):
            self.add_error('routing_number', 'Not a valid 9-digit US routing number.')
        if not self.instance.pk and not routing:
            self.add_error('routing_number', 'Required when adding an account.')
        data['account_number'], data['routing_number'] = account, routing
        return data


@admin.register(PayoutAccount)
class PayoutAccountAdmin(RevealMixin, admin.ModelAdmin):
    form = PayoutAccountForm
    list_display = ('label', 'investor', 'bank_name', 'masked', 'is_verified')
    list_filter = ('is_verified',)
    search_fields = ('label', 'investor__display_name', 'bank_name')

    @admin.display(description='Account')
    def masked(self, obj):
        return obj.masked

    def save_model(self, request, obj, form, change):
        account, routing = form.cleaned_data['account_number'], form.cleaned_data['routing_number']
        if account or routing:
            obj.set_numbers(account or crypto.decrypt(obj.account_number_enc),
                            routing or crypto.decrypt(obj.routing_number_enc))
        if obj.is_verified and not obj.verified_at:
            obj.verified_at = timezone.now()
        elif not obj.is_verified:
            obj.verified_at = None
        super().save_model(request, obj, form, change)

    def bank_details(self, obj):
        return obj.holder_name, obj.bank_name, crypto.decrypt(obj.account_number_enc), crypto.decrypt(obj.routing_number_enc)


class DemoWithdrawalForm(forms.ModelForm):
    """Superusers editing a DEMO withdrawal request: every field is free and none of the real-client
    workflow rules (allowed transitions, call-back, decline reason) apply."""
    # Named differently from the (non-editable) model fields they set; save_model copies them across.
    edit_submitted_at = forms.SplitDateTimeField(widget=AdminSplitDateTime, label='Submitted at')
    edit_decided_at = forms.SplitDateTimeField(widget=AdminSplitDateTime, required=False, label='Decided at')
    edit_detected_country = forms.CharField(max_length=2, required=False, label='Detected country')
    account_number = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'off'}),
                                     help_text='Leave blank to keep the stored number. Stored encrypted.')
    routing_number = forms.CharField(required=False, help_text='Leave blank to keep the stored number.')

    class Meta:
        model = WithdrawalRequest
        fields = ('status', 'amount', 'currency', 'destination_type', 'destination_label', 'holder_name', 'bank_name',
                  'callback_verified', 'decline_reason', 'staff_note')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            for name in ('submitted_at', 'decided_at', 'detected_country'):
                self.fields[f'edit_{name}'].initial = getattr(self.instance, name)

    def clean(self):
        data = super().clean()
        account = (data.get('account_number') or '').replace(' ', '').replace('-', '')
        routing = (data.get('routing_number') or '').replace(' ', '').replace('-', '')
        if account and not withdrawals.valid_account_number(account):
            self.add_error('account_number', 'Use 4 to 17 digits.')
        if routing and not withdrawals.valid_routing_number(routing):
            self.add_error('routing_number', 'Not a valid 9-digit US routing number.')
        data['account_number'], data['routing_number'] = account, routing
        return data


class WithdrawalStaffForm(forms.ModelForm):
    class Meta:
        model = WithdrawalRequest
        fields = ('status', 'callback_verified', 'decline_reason', 'staff_note')

    def clean(self):
        data = super().clean()
        inst = self.instance  # still holds the stored values here; the form's values are applied afterwards
        shim = SimpleNamespace(
            status=inst.status, get_status_display=inst.get_status_display, destination_type=inst.destination_type,
            decline_reason=data.get('decline_reason', ''), callback_verified=data.get('callback_verified', False),
        )
        error = withdrawals.transition_error(shim, data.get('status', inst.status))
        if error:
            raise forms.ValidationError(error)
        return data


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(RevealMixin, admin.ModelAdmin):
    form = WithdrawalStaffForm
    change_list_template = 'admin/investors/withdrawalrequest/change_list.html'
    list_display = ('reference', 'investor', 'amount_display', 'destination_label', 'status',
                    'callback_verified', 'detected_country', 'submitted_at')
    list_filter = ('status', 'destination_type', 'callback_verified')
    search_fields = ('reference', 'investor__display_name')
    date_hierarchy = 'submitted_at'
    fieldsets = (
        ('Request', {'fields': ('reference', 'investor', 'investment', 'amount_display', 'submitted_at', 'detected_country')}),
        ('Destination', {'fields': ('destination_type', 'destination_label', 'holder_name', 'bank_name', 'masked_account')}),
        ('Review', {'fields': ('status', 'callback_verified', 'decline_reason', 'staff_note', 'decided_by', 'decided_at', 'transaction')}),
    )
    readonly_fields = ('reference', 'investor', 'investment', 'amount_display', 'submitted_at', 'detected_country',
                       'destination_type', 'destination_label', 'holder_name', 'bank_name', 'masked_account',
                       'decided_by', 'decided_at', 'transaction')

    # ── superusers have full control of DEMO requests; real requests keep every rule ───────────
    actions = ['delete_demo_requests']

    def _demo_super(self, request, obj):
        return bool(obj is not None and obj.pk and request.user.is_superuser and obj.investor.is_demo)

    def get_form(self, request, obj=None, **kwargs):
        if self._demo_super(request, obj):
            kwargs['form'] = DemoWithdrawalForm
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if self._demo_super(request, obj):
            return (
                ('Request (demo: everything is editable)', {'fields': (
                    'reference', 'investor', 'investment', 'status', 'amount', 'currency', 'edit_submitted_at', 'edit_detected_country')}),
                ('Destination', {'fields': ('destination_type', 'destination_label', 'holder_name', 'bank_name',
                                            'masked_account', 'account_number', 'routing_number')}),
                ('Review', {'fields': ('callback_verified', 'decline_reason', 'staff_note', 'edit_decided_at')}),
            )
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if self._demo_super(request, obj):
            return ('reference', 'investor', 'investment', 'masked_account')
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._demo_super(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop('delete_demo_requests', None)
        return actions

    @admin.action(description='Delete selected DEMO withdrawal requests (superusers only)')
    def delete_demo_requests(self, request, queryset):
        if not request.user.is_superuser:
            raise PermissionDenied
        demo = queryset.filter(investor__is_demo=True)
        kept, removed = queryset.count() - demo.count(), demo.count()
        demo.delete()
        self.message_user(request, f'Deleted {removed} demo request(s).' + (f' {kept} real request(s) were left untouched.' if kept else ''),
                          messages.SUCCESS if removed else messages.WARNING)

    @admin.display(description='Amount')
    def amount_display(self, obj):
        return f'{obj.amount:,.2f} {obj.currency}'

    @admin.display(description='Account (masked)')
    def masked_account(self, obj):
        if obj.destination_type == 'saved' and obj.payout_account:
            return obj.payout_account.masked
        return f'••••{obj.account_last4}' if obj.account_last4 else '—'

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('investor', 'investment')

    def save_model(self, request, obj, form, change):
        if self._demo_super(request, obj):
            data = form.cleaned_data
            obj.submitted_at, obj.decided_at = data['edit_submitted_at'], data.get('edit_decided_at')
            obj.detected_country = (data.get('edit_detected_country') or '').upper()
            account, routing = data.get('account_number'), data.get('routing_number')
            if account or routing:
                obj.set_typed_details(account or crypto.decrypt(obj.account_number_enc),
                                      routing or crypto.decrypt(obj.routing_number_enc))
            return super().save_model(request, obj, form, change)  # plain save: no workflow, no emails
        if 'status' in form.changed_data:
            new_status = obj.status
            with transaction.atomic():
                withdrawals.apply_status(obj, new_status, request.user)
            profile = obj.investor
            if not profile.is_demo:
                transaction.on_commit(lambda: _notify_update(profile, obj))
        else:
            super().save_model(request, obj, form, change)

    def bank_details(self, obj):
        if obj.destination_type == 'saved' and obj.payout_account:
            acct = obj.payout_account
            return acct.holder_name, acct.bank_name, crypto.decrypt(acct.account_number_enc), crypto.decrypt(acct.routing_number_enc)
        if obj.destination_type == 'typed':
            return obj.holder_name, obj.bank_name, crypto.decrypt(obj.account_number_enc), crypto.decrypt(obj.routing_number_enc)
        return None

    def get_urls(self):
        custom = [path('my-location/', self.admin_site.admin_view(self.my_location_view),
                       name='investors_withdrawalrequest_my_location')]
        return custom + super().get_urls()

    def my_location_view(self, request):
        """Shows how the app sees THIS request, to confirm Cloudflare is wired up correctly."""
        from django.conf import settings
        supplied = request.META.get(geo.SECRET_HEADER, '')
        allowed, country = geo.withdrawals_allowed(request)
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'Check my detected location',
            'country': country, 'allowed': allowed,
            'raw_country': request.META.get(geo.COUNTRY_HEADER, ''),
            'secret_configured': bool(settings.CF_ORIGIN_SECRET),
            'secret_sent': bool(supplied),
            'debug_override': settings.DEBUG and bool(settings.DEV_COUNTRY),
        }
        return TemplateResponse(request, 'admin/investors/withdrawalrequest/my_location.html', context)


def _notify_update(profile, wr):
    try:
        send_withdrawal_update_email(profile, wr)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Withdrawal update email failed')
