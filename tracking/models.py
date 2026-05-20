import random
import string
from django.db import models
from django.utils import timezone


class Shipment(models.Model):
    CARGO_TYPE_CHOICES = [
        ('General', 'General'),
        ('Fragile', 'Fragile'),
        ('Perishable', 'Perishable'),
        ('Hazardous', 'Hazardous'),
        ('Electronics', 'Electronics'),
    ]

    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('In Transit', 'In Transit'),
        ('Out for Delivery', 'Out for Delivery'),
        ('Delivered', 'Delivered'),
        ('On Hold', 'On Hold'),
        ('Exception', 'Exception'),
    ]

    tracking_number = models.CharField(max_length=20, unique=True, blank=True)

    # Sender
    sender_name = models.CharField(max_length=200)
    sender_address = models.CharField(max_length=500)
    sender_phone = models.CharField(max_length=30)

    # Receiver
    receiver_name = models.CharField(max_length=200)
    receiver_address = models.CharField(max_length=500)
    receiver_phone = models.CharField(max_length=30)
    receiver_email = models.CharField(max_length=254)

    # Route
    origin_city = models.CharField(max_length=100)
    origin_country = models.CharField(max_length=100)
    destination_city = models.CharField(max_length=100)
    destination_country = models.CharField(max_length=100)

    # Package details
    weight = models.DecimalField(max_digits=10, decimal_places=2, help_text='Weight in kg')
    dimensions = models.CharField(max_length=100, help_text='e.g. 40x30x20 cm')
    cargo_type = models.CharField(max_length=20, choices=CARGO_TYPE_CHOICES, default='General')

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    estimated_delivery = models.DateField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_status = self.status

    def __str__(self):
        return f'{self.tracking_number} — {self.sender_name} → {self.receiver_name}'

    def _generate_tracking_number(self):
        year = timezone.now().year
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f'VT-{year}-{suffix}'

    def save(self, *args, skip_email=False, **kwargs):
        is_new = not self.pk
        old_status = self._original_status

        if not self.tracking_number:
            candidate = self._generate_tracking_number()
            while Shipment.objects.filter(tracking_number=candidate).exists():
                candidate = self._generate_tracking_number()
            self.tracking_number = candidate

        super().save(*args, **kwargs)

        if not skip_email:
            try:
                from tracking.emails import send_shipment_created_email, send_status_update_email
                if is_new and self.receiver_email:
                    send_shipment_created_email(self)
                elif not is_new and self.status != old_status and self.receiver_email:
                    send_status_update_email(self, old_status)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error('Email dispatch error for %s: %s', self.tracking_number, exc)

        self._original_status = self.status


class ShipmentEvent(models.Model):
    shipment = models.ForeignKey(
        Shipment, on_delete=models.CASCADE, related_name='events'
    )
    location = models.CharField(max_length=200)
    status = models.CharField(max_length=100)
    description = models.TextField()
    timestamp = models.DateTimeField()

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.shipment.tracking_number} | {self.status} @ {self.location}'
