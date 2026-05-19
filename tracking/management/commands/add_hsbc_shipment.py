from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from tracking.models import Shipment, ShipmentEvent


class Command(BaseCommand):
    help = "Adds the HSBC London → New York shipment to the database."

    def handle(self, *args, **options):
        if Shipment.objects.filter(sender_name='HSBC Bank plc', origin_city='London').exists():
            self.stdout.write(self.style.WARNING('  HSBC shipment already exists — skipping.'))
            return

        now = timezone.now()

        shipment = Shipment(
            sender_name='HSBC Bank plc',
            sender_address='8 Canada Square, Canary Wharf, London E14 5HQ, United Kingdom',
            sender_phone='+44 20 7991 8888',
            receiver_name='HSBC Bank USA, N.A.',
            receiver_address='452 Fifth Avenue, New York, NY 10018, United States',
            receiver_phone='+1 212 525 5000',
            receiver_email='trade.ops@us.hsbc.com',
            origin_city='London',
            origin_country='United Kingdom',
            destination_city='New York',
            destination_country='United States',
            weight=38.50,
            dimensions='60x45x40 cm',
            cargo_type='General',
            status='In Transit',
            estimated_delivery=(now + timedelta(days=3)).date(),
            notes='Inter-branch trade finance documents. Priority handling. Tamper-evident packaging required.',
        )
        shipment.save()

        events = [
            (-4, 'Shipment Booked',         'London, United Kingdom',          'Booking confirmed. Trade finance documentation package sealed and registered. Reference: HSBC-LON-2024-0892.'),
            (-3, 'Picked Up',               'London, United Kingdom',          'Cargo collected from HSBC Canary Wharf by bonded courier. Chain of custody initiated. Tamper seals verified.'),
            (-3, 'Arrived at Origin Hub',   'London Heathrow, United Kingdom', 'Shipment received at air freight terminal. Security screening completed. Allocated to Priority Air service.'),
            (-2, 'Export Customs Cleared',  'London Heathrow, United Kingdom', 'Export declaration approved. No restrictions. Cargo released for loading onto flight BA0117.'),
            (-1, 'Departed Origin Airport', 'London Heathrow, United Kingdom', 'Cargo loaded and departed LHR 09:25 GMT. In-flight tracking active. ETA JFK 12:40 EST.'),
            ( 0, 'In Transit',              'International Airspace',          'Cargo en route over North Atlantic. Flight on schedule. No anomalies reported.'),
        ]

        for offset, status, location, desc in events:
            ShipmentEvent.objects.create(
                shipment=shipment,
                status=status,
                location=location,
                description=desc,
                timestamp=now + timedelta(days=offset, hours=9),
            )

        self.stdout.write(self.style.SUCCESS(f'\n  Tracking Number : {shipment.tracking_number}'))
        self.stdout.write(self.style.SUCCESS(f'  Route           : {shipment.origin_city} → {shipment.destination_city}'))
        self.stdout.write(self.style.SUCCESS(f'  Status          : {shipment.status}'))
        self.stdout.write(self.style.SUCCESS(f'  Events          : {shipment.events.count()}'))
        self.stdout.write(self.style.SUCCESS(f'  ETA             : {shipment.estimated_delivery}\n'))
