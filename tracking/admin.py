from django.contrib import admin
from .models import Shipment, ShipmentEvent


class ShipmentEventInline(admin.TabularInline):
    model = ShipmentEvent
    extra = 1
    ordering = ['-timestamp']


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
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
