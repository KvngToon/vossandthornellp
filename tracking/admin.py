from django.contrib import admin
from django.contrib import messages
from .models import Shipment, ShipmentEvent


class ShipmentEventInline(admin.TabularInline):
    model = ShipmentEvent
    extra = 1
    ordering = ['-timestamp']


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
        'status', 'estimated_delivery', 'created_at',
    ]
    search_fields = [
        'tracking_number', 'sender_name', 'receiver_name',
        'receiver_email', 'origin_city', 'destination_city',
        'origin_country', 'destination_country',
    ]
    list_filter = ['status', 'cargo_type', 'origin_country', 'destination_country']
    readonly_fields = ['tracking_number', 'created_at', 'updated_at']
    inlines = [ShipmentEventInline]
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
