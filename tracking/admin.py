from django import forms
from django.contrib import admin
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import EmailMessage, Shipment, ShipmentEvent


class ShipmentEventInline(admin.TabularInline):
    model = ShipmentEvent
    extra = 1
    ordering = ['-timestamp']


class EmailMessageInline(admin.TabularInline):
    model = EmailMessage
    extra = 0
    fields = ['direction', 'from_email', 'to_email', 'subject', 'is_read', 'created_at']
    readonly_fields = ['direction', 'from_email', 'to_email', 'subject', 'created_at']
    ordering = ['-created_at']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class StaffReplyForm(forms.Form):
    to_email = forms.EmailField(label='To')
    subject = forms.CharField(max_length=500)
    body = forms.CharField(widget=forms.Textarea(attrs={'rows': 10}))


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    actions = ['action_send_confirmation_email', 'action_send_status_email']

    # ── Email: capture pre-save status before anything changes ────
    def save_model(self, request, obj, form, change):
        if change:
            try:
                obj._pre_save_status = Shipment.objects.values_list(
                    'status', flat=True).get(pk=obj.pk)
            except Shipment.DoesNotExist:
                obj._pre_save_status = obj.status
        else:
            obj._pre_save_status = None   # new shipment
        super().save_model(request, obj, form, change)

    # ── Email: fires AFTER all inline events are saved ─────────────
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        old_status = getattr(obj, '_pre_save_status', None)
        try:
            from tracking.emails import send_shipment_created_email, send_status_update_email
            if old_status is None and obj.receiver_email:
                # Brand-new shipment
                send_shipment_created_email(obj)
            elif old_status is not None and obj.status != old_status and obj.receiver_email:
                # Status changed — email fires after latest event is already saved
                send_status_update_email(obj, old_status)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error('Admin email dispatch error for %s: %s', obj.tracking_number, exc)

    def get_queryset(self, request):
        from django.db.models import Count, Q
        return super().get_queryset(request).annotate(
            unread_count=Count('email_thread', filter=Q(email_thread__direction='inbound', email_thread__is_read=False))
        )

    def unread_badge(self, obj):
        if not obj.unread_count:
            return '—'
        url = reverse('admin:tracking_inbox_shipment', args=[obj.pk])
        return format_html(
            '<a href="{}" style="color:#e84545;font-weight:bold;">{} new</a>', url, obj.unread_count
        )
    unread_badge.short_description = 'Client replies'
    unread_badge.admin_order_field = 'unread_count'

    def thread_link(self, obj):
        if not obj.pk:
            return '—'
        url = reverse('admin:tracking_inbox_shipment', args=[obj.pk])
        count = obj.email_thread.count()
        unread = obj.email_thread.filter(direction='inbound', is_read=False).count()
        label = f'Open conversation ({count} message{"s" if count != 1 else ""})'
        if unread:
            label += f' — {unread} unread'
        return format_html('<a href="{}">{}</a>', url, label)
    thread_link.short_description = 'Client conversation'

    # ── Manual bulk actions ────────────────────────────────────────
    def action_send_confirmation_email(self, request, queryset):
        from tracking.emails import send_shipment_created_email
        sent, skipped = 0, 0
        for shipment in queryset:
            if shipment.receiver_email:
                send_shipment_created_email(shipment)
                sent += 1
            else:
                skipped += 1
        self.message_user(request, f'Confirmation email sent to {sent} receiver(s).', messages.SUCCESS)
        if skipped:
            self.message_user(request, f'{skipped} shipment(s) skipped — no receiver email.', messages.WARNING)
    action_send_confirmation_email.short_description = 'Send booking confirmation email to receiver'

    def action_send_status_email(self, request, queryset):
        from tracking.emails import send_status_update_email
        sent, skipped = 0, 0
        for shipment in queryset:
            if shipment.receiver_email:
                send_status_update_email(shipment, shipment.status)
                sent += 1
            else:
                skipped += 1
        self.message_user(request, f'Status update email sent to {sent} receiver(s).', messages.SUCCESS)
        if skipped:
            self.message_user(request, f'{skipped} shipment(s) skipped — no receiver email.', messages.WARNING)
    action_send_status_email.short_description = 'Send current status update email to receiver'

    list_display = [
        'tracking_number', 'sender_name', 'receiver_name',
        'origin_city', 'destination_city', 'cargo_type',
        'status', 'estimated_delivery', 'created_at', 'unread_badge',
    ]
    search_fields = [
        'tracking_number', 'sender_name', 'receiver_name',
        'receiver_email', 'origin_city', 'destination_city',
        'origin_country', 'destination_country',
    ]
    list_filter = ['status', 'cargo_type', 'origin_country', 'destination_country']
    readonly_fields = ['tracking_number', 'created_at', 'updated_at', 'thread_link']
    inlines = [ShipmentEventInline, EmailMessageInline]
    fieldsets = (
        ('Tracking', {
            'fields': ('tracking_number', 'status', 'estimated_delivery', 'notes'),
        }),
        ('Sender', {
            'fields': ('sender_name', 'sender_address', 'sender_phone'),
        }),
        ('Receiver', {
            'fields': ('receiver_name', 'receiver_address', 'receiver_phone', 'receiver_email'),
        }),
        ('Route', {
            'fields': ('origin_city', 'origin_country', 'destination_city', 'destination_country'),
        }),
        ('Package', {
            'fields': ('cargo_type', 'weight', 'dimensions'),
        }),
        ('Client Conversation', {
            'fields': ('thread_link',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(ShipmentEvent)
class ShipmentEventAdmin(admin.ModelAdmin):
    list_display = ['shipment', 'status', 'location', 'timestamp']
    search_fields = ['shipment__tracking_number', 'status', 'location', 'description']
    list_filter = ['status']
    ordering = ['-timestamp']


def _address_conversation_filter(address):
    """A message with no shipment 'belongs' to a counterparty address either
    as the sender (inbound) or the recipient (outbound). Restricted to
    shipment=None so a message already grouped under a shipment thread never
    also shows up here — otherwise a client with an open shipment thread AND
    a stray general enquiry would see the shipment message duplicated into
    the general conversation too."""
    from django.db.models import Q
    return (Q(shipment=None) &
            (Q(direction='inbound', from_email__iexact=address) |
             Q(direction='outbound', to_email__iexact=address)))


@admin.register(EmailMessage)
class EmailMessageAdmin(admin.ModelAdmin):
    # NOTE: the changelist below is intentionally replaced by the Inbox
    # (see changelist_view) so list_display/list_filter/search_fields never
    # actually render — this ModelAdmin only exists to host the Inbox URLs
    # and the read-only change_view for an individual message.
    readonly_fields = [
        'shipment', 'direction', 'from_email', 'from_name', 'to_email',
        'subject', 'text_body', 'html_body', 'message_id', 'in_reply_to', 'references', 'created_at',
    ]
    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False

    # ── Inbox: one row per shipment thread, plus one row per unmatched
    # sender address — see _address_conversation_filter for why those two
    # kinds of "conversation" don't collapse into each other. ──
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('inbox/', self.admin_site.admin_view(self.inbox_view), name='tracking_inbox'),
            path('inbox/backfill/', self.admin_site.admin_view(self.backfill_view), name='tracking_inbox_backfill'),
            path('inbox/shipment/<int:shipment_id>/', self.admin_site.admin_view(self.shipment_conversation_view), name='tracking_inbox_shipment'),
            path('inbox/address/<str:address>/', self.admin_site.admin_view(self.address_conversation_view), name='tracking_inbox_address'),
            path('inbox/address/<str:address>/link/', self.admin_site.admin_view(self.link_shipment_view), name='tracking_inbox_link_shipment'),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse('admin:tracking_inbox'))

    def backfill_view(self, request):
        if request.method != 'POST':
            return redirect(reverse('admin:tracking_inbox'))

        from tracking.webhooks import backfill_all_inbound_content
        updated, checked = backfill_all_inbound_content()
        if checked == 0:
            self.message_user(request, 'Nothing to backfill — every message already has content.', messages.INFO)
        elif updated == checked:
            self.message_user(request, f'Recovered content for all {updated} message(s).', messages.SUCCESS)
        elif updated:
            self.message_user(
                request,
                f'Recovered {updated} of {checked} empty message(s). '
                f'The rest weren\'t found in Resend\'s received-email log (may have expired or predate the integration).',
                messages.WARNING,
            )
        else:
            self.message_user(
                request,
                f'Could not recover any of the {checked} empty message(s) — check RESEND_API_KEY and the logs.',
                messages.ERROR,
            )
        return redirect(reverse('admin:tracking_inbox'))

    def inbox_view(self, request):
        from django.db.models import Case, CharField, Count, F, Max, Q, When

        query = request.GET.get('q', '').strip()
        only_unread = request.GET.get('filter') == 'unread'

        # Shipment-scoped conversations: every message tied to a shipment,
        # grouped by that shipment regardless of who sent/received it.
        shipment_groups = (
            EmailMessage.objects.exclude(shipment=None)
            .values('shipment')
            .annotate(
                last_at=Max('created_at'),
                unread=Count('id', filter=Q(direction='inbound', is_read=False)),
                message_count=Count('id'),
            )
        )
        shipment_ids = [g['shipment'] for g in shipment_groups]
        shipments_by_id = Shipment.objects.in_bulk(shipment_ids)
        last_by_shipment = {
            m.shipment_id: m
            for m in EmailMessage.objects.filter(shipment_id__in=shipment_ids).order_by('created_at')
        }

        rows = []
        for g in shipment_groups:
            shipment = shipments_by_id.get(g['shipment'])
            if not shipment:
                continue
            last_message = last_by_shipment.get(shipment.pk)
            rows.append({
                'kind': 'shipment',
                'url': reverse('admin:tracking_inbox_shipment', args=[shipment.pk]),
                'title': shipment.receiver_name,
                'tag': shipment.tracking_number,
                'subject': last_message.subject if last_message else '',
                'snippet': (last_message.text_body or '')[:140] if last_message else '',
                'last_at': g['last_at'],
                'unread': g['unread'],
                'message_count': g['message_count'],
            })

        # Address-scoped conversations: messages with no shipment at all,
        # grouped by the other party's email address.
        address_qs = EmailMessage.objects.filter(shipment=None).annotate(
            counterparty=Case(
                When(direction='inbound', then=F('from_email')),
                default=F('to_email'),
                output_field=CharField(),
            )
        )
        address_groups = (
            address_qs.values('counterparty')
            .annotate(
                last_at=Max('created_at'),
                unread=Count('id', filter=Q(direction='inbound', is_read=False)),
                message_count=Count('id'),
            )
        )
        last_by_address = {}
        for m in address_qs.order_by('created_at'):
            last_by_address[m.counterparty] = m

        for g in address_groups:
            address = g['counterparty']
            if not address:
                continue
            last_message = last_by_address.get(address)
            rows.append({
                'kind': 'address',
                'url': reverse('admin:tracking_inbox_address', args=[address]),
                'title': (last_message.from_name if last_message and last_message.direction == 'inbound' else '') or address,
                'tag': '',
                'subject': last_message.subject if last_message else '',
                'snippet': (last_message.text_body or '')[:140] if last_message else '',
                'last_at': g['last_at'],
                'unread': g['unread'],
                'message_count': g['message_count'],
            })

        if only_unread:
            rows = [r for r in rows if r['unread']]

        if query:
            q_lower = query.lower()
            rows = [
                r for r in rows
                if q_lower in r['title'].lower()
                or q_lower in r['tag'].lower()
                or q_lower in r['subject'].lower()
                or q_lower in r['snippet'].lower()
            ]

        rows.sort(key=lambda r: r['last_at'], reverse=True)
        total_unread = sum(1 for g in shipment_groups if g['unread']) + sum(1 for g in address_groups if g['unread'])
        empty_count = EmailMessage.objects.filter(direction='inbound', text_body='', html_body='').count()

        context = {
            **self.admin_site.each_context(request),
            'title': 'Inbox',
            'rows': rows,
            'query': query,
            'only_unread': only_unread,
            'total_unread': total_unread,
            'empty_count': empty_count,
            'opts': self.model._meta,
        }
        return render(request, 'admin/tracking/inbox.html', context)

    def _send_reply_and_render(self, request, thread_qs, shipment, address, view_name, view_args):
        last_message = thread_qs.last()

        initial = {
            'to_email': address or (shipment.receiver_email if shipment else ''),
            'subject': f'Re: {last_message.subject}' if last_message and last_message.subject else 'Re: your message',
        }

        if request.method == 'POST':
            form = StaffReplyForm(request.POST, initial=initial)
            if form.is_valid():
                references = ' '.join(m.message_id for m in thread_qs if m.message_id) or None
                in_reply_to = last_message.message_id if last_message else None

                from tracking.emails import send_staff_reply_email
                result = send_staff_reply_email(
                    form.cleaned_data['to_email'],
                    form.cleaned_data['subject'],
                    form.cleaned_data['body'],
                    in_reply_to=in_reply_to,
                    references=references,
                    shipment=shipment,
                )
                if result is not None:
                    self.message_user(request, 'Reply sent.', messages.SUCCESS)
                else:
                    self.message_user(request, 'Failed to send reply — check the logs.', messages.ERROR)
                return redirect(reverse(view_name, args=view_args))
        else:
            form = StaffReplyForm(initial=initial)

        thread_qs.filter(direction='inbound', is_read=False).update(is_read=True)

        context = {
            **self.admin_site.each_context(request),
            'title': f'{shipment.tracking_number}' if shipment else address,
            'address': address or (shipment.receiver_email if shipment else ''),
            'shipment': shipment,
            'form': form,
            'thread': thread_qs,
            'opts': self.model._meta,
        }
        return render(request, 'admin/tracking/inbox_conversation.html', context)

    def shipment_conversation_view(self, request, shipment_id):
        shipment = get_object_or_404(Shipment, pk=shipment_id)
        thread_qs = shipment.email_thread.order_by('created_at')
        return self._send_reply_and_render(
            request, thread_qs, shipment, None,
            'admin:tracking_inbox_shipment', [shipment.pk],
        )

    def address_conversation_view(self, request, address):
        thread_qs = EmailMessage.objects.filter(_address_conversation_filter(address)).order_by('created_at')
        return self._send_reply_and_render(
            request, thread_qs, None, address,
            'admin:tracking_inbox_address', [address],
        )

    def link_shipment_view(self, request, address):
        if request.method != 'POST':
            return redirect(reverse('admin:tracking_inbox_address', args=[address]))

        tracking_number = request.POST.get('tracking_number', '').strip()
        shipment = Shipment.objects.filter(tracking_number__iexact=tracking_number).first()
        if not shipment:
            self.message_user(request, f'No shipment found with tracking number "{tracking_number}".', messages.ERROR)
            return redirect(reverse('admin:tracking_inbox_address', args=[address]))

        updated = EmailMessage.objects.filter(_address_conversation_filter(address)).update(shipment=shipment)
        self.message_user(request, f'Linked {updated} message(s) to {shipment.tracking_number}.', messages.SUCCESS)
        return redirect(reverse('admin:tracking_inbox_shipment', args=[shipment.pk]))
