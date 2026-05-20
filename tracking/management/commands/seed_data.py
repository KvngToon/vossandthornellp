import random
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from tracking.models import Shipment, ShipmentEvent


# ── Sample data pools ─────────────────────────────────────────────────────────

SENDERS = [
    {"name": "Hartmann Industrial GmbH",  "address": "Industriestrasse 44, 60325 Frankfurt, Germany",      "phone": "+49 69 1234 5678"},
    {"name": "Nguyen Logistics Co.",       "address": "12 Tan Binh Blvd, Ho Chi Minh City, Vietnam",        "phone": "+84 28 3850 1234"},
    {"name": "Alvarez & Torres Exports",  "address": "Av. Insurgentes Sur 1457, Mexico City, Mexico",       "phone": "+52 55 5555 7890"},
    {"name": "Petrov Commodities Ltd.",   "address": "Nevsky Prospekt 28, St. Petersburg, Russia",          "phone": "+7 812 310 0099"},
    {"name": "Chen Precision Mfg.",       "address": "No. 88 Zhongshan Road, Shenzhen 518000, China",       "phone": "+86 755 2896 3300"},
    {"name": "Okafor Energy Solutions",   "address": "14 Victoria Island, Lagos, Nigeria",                   "phone": "+234 1 461 8800"},
    {"name": "Yamamoto Electronics K.K.", "address": "3-1-1 Akihabara, Chiyoda, Tokyo 101-0021, Japan",     "phone": "+81 3 3254 7700"},
    {"name": "Sterling & Baird Corp.",    "address": "350 Fifth Ave, Suite 4100, New York, NY 10118, USA",   "phone": "+1 212 555 0190"},
    {"name": "Lindqvist AB",              "address": "Storgatan 12, 111 24 Stockholm, Sweden",               "phone": "+46 8 123 456 78"},
    {"name": "Khalid Brothers Trading",   "address": "Al Quoz Industrial Area, Dubai, UAE",                  "phone": "+971 4 338 8800"},
]

RECEIVERS = [
    {"name": "Blackwood Capital Ltd.",    "address": "30 St Mary Axe, London EC3A 8EP, UK",                 "phone": "+44 20 7946 0011", "email": "ops@blackwoodcap.com"},
    {"name": "Pacific Rim Distributors", "address": "888 Collins St, Melbourne VIC 3000, Australia",         "phone": "+61 3 9001 2345",  "email": "freight@pacrim.au"},
    {"name": "Rousseau & Fils SARL",     "address": "15 Rue de Rivoli, 75001 Paris, France",                 "phone": "+33 1 4296 3300",  "email": "import@rousseau.fr"},
    {"name": "Atlas Pharma AG",          "address": "Elisabethenstrasse 23, 4051 Basel, Switzerland",        "phone": "+41 61 260 5500",  "email": "logistics@atlaspharma.ch"},
    {"name": "Meridian Foods Inc.",       "address": "200 Bay St, Toronto, ON M5J 2J1, Canada",              "phone": "+1 416 555 0177",  "email": "supply@meridianfoods.ca"},
    {"name": "Helios Auto Parts S.A.",   "address": "Leoforos Kifisias 44, 151 25 Athens, Greece",           "phone": "+30 210 614 5500",  "email": "parts@heliosautosa.gr"},
    {"name": "Maersk West Africa",        "address": "Independence Ave, Accra, Ghana",                        "phone": "+233 30 266 7700",  "email": "receive@maerskwa.com"},
    {"name": "SingaPure Chemicals Pte.", "address": "1 Harbourfront Ave, #18-01, Singapore 098632",          "phone": "+65 6508 8888",    "email": "chem@singapure.sg"},
    {"name": "Volkov Steel Works",        "address": "Prospekt Lenina 77, Yekaterinburg 620026, Russia",      "phone": "+7 343 356 8800",  "email": "procurement@volkovsteel.ru"},
    {"name": "Cortez Global Trade",       "address": "Calle 72 No. 10-07, Bogotá, Colombia",                  "phone": "+57 1 345 6789",   "email": "trade@cortezglobal.co"},
]

ROUTES = [
    {"origin_city": "Frankfurt",    "origin_country": "Germany",      "dest_city": "London",       "dest_country": "United Kingdom"},
    {"origin_city": "Shenzhen",     "origin_country": "China",         "dest_city": "Melbourne",    "dest_country": "Australia"},
    {"origin_city": "Mexico City",  "origin_country": "Mexico",        "dest_city": "Paris",        "dest_country": "France"},
    {"origin_city": "Tokyo",        "origin_country": "Japan",         "dest_city": "Basel",        "dest_country": "Switzerland"},
    {"origin_city": "New York",     "origin_country": "United States", "dest_city": "Toronto",      "dest_country": "Canada"},
    {"origin_city": "Lagos",        "origin_country": "Nigeria",       "dest_city": "Athens",       "dest_country": "Greece"},
    {"origin_city": "Dubai",        "origin_country": "UAE",           "dest_city": "Accra",        "dest_country": "Ghana"},
    {"origin_city": "St. Petersburg","origin_country": "Russia",       "dest_city": "Singapore",    "dest_country": "Singapore"},
    {"origin_city": "Stockholm",    "origin_country": "Sweden",        "dest_city": "Yekaterinburg","dest_country": "Russia"},
    {"origin_city": "Ho Chi Minh City","origin_country": "Vietnam",    "dest_city": "Bogotá",       "dest_country": "Colombia"},
]

# Keyed by status — a realistic sequence of events leading to that state
EVENT_SEQUENCES = {

    "Delivered": [
        {"offset_days": -12, "status": "Shipment Booked",         "desc": "Cargo registered and booking confirmed. Documentation package issued."},
        {"offset_days": -11, "status": "Picked Up",               "desc": "Cargo collected from sender premises. Weight and dimensions verified on-site."},
        {"offset_days": -10, "status": "Arrived at Origin Hub",   "desc": "Shipment received at origin sorting facility. Pallets consolidated for export."},
        {"offset_days": -9,  "status": "Export Customs Cleared",  "desc": "Export declaration approved by customs authority. Cargo released for loading."},
        {"offset_days": -8,  "status": "Loaded on Vessel",        "desc": "Cargo loaded onto vessel. Bill of lading issued. Estimated transit: 7 days."},
        {"offset_days": -4,  "status": "In Transit — Mid Ocean",  "desc": "Vessel en route. Position confirmed via AIS tracking. No anomalies reported."},
        {"offset_days": -1,  "status": "Arrived at Destination Port","desc": "Vessel docked. Cargo offloaded and transferred to bonded warehouse."},
        {"offset_days":  0,  "status": "Import Customs Cleared",  "desc": "Import duty paid and customs inspection passed. Cargo released to consignee."},
        {"offset_days":  0,  "status": "Out for Delivery",        "desc": "Final-mile carrier assigned. Delivery vehicle departed depot at 07:40 local time."},
        {"offset_days":  0,  "status": "Delivered",               "desc": "Shipment delivered and signed for by authorised representative. POD archived."},
    ],

    "In Transit": [
        {"offset_days": -6, "status": "Shipment Booked",        "desc": "Booking confirmed. Air waybill generated. Dangerous goods declaration submitted."},
        {"offset_days": -5, "status": "Picked Up",              "desc": "Cargo collected by bonded courier. Temperature logger activated."},
        {"offset_days": -4, "status": "Arrived at Origin Hub",  "desc": "Cargo received at air freight terminal. Security X-ray screening completed."},
        {"offset_days": -3, "status": "Export Customs Cleared", "desc": "Export clearance granted. Cargo allocated to flight schedule."},
        {"offset_days": -2, "status": "Departed Origin Airport","desc": "Cargo loaded and departed. In-flight position tracking active."},
        {"offset_days": -1, "status": "Transiting Hub",         "desc": "Cargo transferred at intermediate hub. Connecting flight confirmed. ETA updated."},
    ],

    "Out for Delivery": [
        {"offset_days": -4, "status": "Shipment Booked",           "desc": "Express booking confirmed. Priority lane assigned."},
        {"offset_days": -3, "status": "Picked Up",                 "desc": "Cargo collected. Chain of custody initiated."},
        {"offset_days": -2, "status": "Export Customs Cleared",    "desc": "Pre-clearance approved under AEO fast-track agreement."},
        {"offset_days": -1, "status": "Arrived at Destination Hub","desc": "Cargo received at final distribution centre. Delivery slot allocated."},
        {"offset_days":  0, "status": "Out for Delivery",          "desc": "Delivery vehicle en route to consignee address. Estimated arrival: 10:00–14:00."},
    ],

    "Pending": [
        {"offset_days":  0, "status": "Shipment Booked",  "desc": "Booking confirmed. Awaiting cargo collection from sender premises."},
        {"offset_days":  0, "status": "Documents Issued", "desc": "Shipping instructions received. House bill of lading draft prepared for review."},
    ],

    "On Hold": [
        {"offset_days": -8, "status": "Shipment Booked",          "desc": "Booking confirmed and documentation package issued."},
        {"offset_days": -7, "status": "Picked Up",                "desc": "Cargo collected and received at origin facility."},
        {"offset_days": -6, "status": "Export Customs Cleared",   "desc": "Export clearance obtained without issue."},
        {"offset_days": -5, "status": "Departed Origin",          "desc": "Cargo loaded and in transit."},
        {"offset_days": -3, "status": "Arrived at Transit Hub",   "desc": "Cargo arrived at interim hub and offloaded for inspection."},
        {"offset_days": -2, "status": "Customs Hold — Review",    "desc": "Import authority has placed cargo on hold for enhanced documentary review. Additional certificates requested."},
        {"offset_days": -1, "status": "On Hold — Awaiting Docs",  "desc": "Shipment suspended pending receipt of phytosanitary certificate from exporter. Operations team notified."},
    ],

    "Exception": [
        {"offset_days": -10, "status": "Shipment Booked",          "desc": "Booking confirmed. Hazardous cargo declaration filed per IATA regulations."},
        {"offset_days": -9,  "status": "Picked Up",                "desc": "Cargo collected. Hazmat placards affixed. UN numbers verified."},
        {"offset_days": -8,  "status": "Export Customs Cleared",   "desc": "Export licence granted by competent authority."},
        {"offset_days": -7,  "status": "In Transit",               "desc": "Cargo en route via authorised hazmat carrier."},
        {"offset_days": -5,  "status": "Arrived at Transit Hub",   "desc": "Shipment received at intermediate hub. Awaiting connecting service."},
        {"offset_days": -3,  "status": "Routing Disruption",       "desc": "Scheduled air service cancelled due to regulatory restriction at transit country. Cargo rerouted via alternative hub."},
        {"offset_days": -2,  "status": "Exception — Delay",        "desc": "Rerouting adds estimated 4-day delay. Consignee notified. New ETA issued. Monitoring heightened."},
        {"offset_days": -1,  "status": "Exception — Under Review", "desc": "Cargo under review by destination customs authority. MSDS and DG manifest submitted. Awaiting clearance decision."},
    ],
}

CARGO_TYPES  = ["General", "Fragile", "Perishable", "Hazardous", "Electronics"]
CARGO_DIMS   = ["120x80x100 cm", "60x40x40 cm", "220x100x80 cm", "45x30x25 cm", "80x60x60 cm",
                 "180x120x90 cm", "50x50x50 cm", "100x75x60 cm", "30x20x15 cm", "150x90x70 cm"]
CARGO_NOTES  = [
    "Handle with care. Fragile instruments enclosed.",
    "Temperature-sensitive. Maintain 2–8°C throughout transit.",
    "Client ref: PROJ-2024-007. Priority clearance pre-arranged.",
    "Insured cargo. Photo documentation required at each handover.",
    "Dual-use goods — export licence attached.",
    "",  # blank intentionally
    "Receiver must be present to sign. No redirection permitted.",
    "Pallet dimensions non-standard. Forklift required for unloading.",
    "",
    "Hazmat class 9 — lithium batteries. Quantity exemption applies.",
]

WEIGHTS = [210.50, 45.00, 875.25, 12.80, 320.00, 1200.00, 8.50, 540.75, 67.20, 95.00]


class Command(BaseCommand):
    help = "Seeds the database with 10 realistic sample shipments and their timeline events."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete all existing shipments before seeding.",
        )

    def handle(self, *args, **options):
        if options["flush"]:
            count = Shipment.objects.count()
            Shipment.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"  Flushed {count} existing shipment(s)."))

        now = timezone.now()

        statuses = [
            "Delivered",
            "Delivered",
            "In Transit",
            "In Transit",
            "Out for Delivery",
            "Pending",
            "On Hold",
            "Exception",
            "Delivered",
            "In Transit",
        ]

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("  ┌─────────────────────────────────────────────────────────────┐"))
        self.stdout.write(self.style.HTTP_INFO("  │        VOSS & THORNE LLP — SAMPLE DATA SEEDER               │"))
        self.stdout.write(self.style.HTTP_INFO("  └─────────────────────────────────────────────────────────────┘"))
        self.stdout.write("")

        # Skip if data already exists (safe for repeated deploys)
        if not options["flush"] and Shipment.objects.exists():
            existing = Shipment.objects.values_list("tracking_number", flat=True).order_by("created_at")
            self.stdout.write(self.style.WARNING("  ⚠  Shipments already exist — skipping seed. Pass --flush to re-seed."))
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("  ┌─────────────────────────────────────────────────────────────┐"))
            self.stdout.write(self.style.HTTP_INFO("  │  EXISTING TRACKING NUMBERS                                  │"))
            self.stdout.write(self.style.HTTP_INFO("  ├─────────────────────────────────────────────────────────────┤"))
            for tn in existing:
                self.stdout.write(self.style.HTTP_INFO(f"  │  {tn:<57}│"))
            self.stdout.write(self.style.HTTP_INFO("  └─────────────────────────────────────────────────────────────┘"))
            self.stdout.write("")
            return

        tracking_numbers = []

        for i, status in enumerate(statuses):
            sender   = SENDERS[i]
            receiver = RECEIVERS[i]
            route    = ROUTES[i]
            seq      = EVENT_SEQUENCES[status]

            # Estimated delivery: future for pending/in-transit, past for delivered
            if status == "Delivered":
                eta = (now + timedelta(days=random.randint(-2, 0))).date()
            elif status in ("In Transit", "Out for Delivery"):
                eta = (now + timedelta(days=random.randint(2, 6))).date()
            elif status == "Pending":
                eta = (now + timedelta(days=random.randint(10, 18))).date()
            else:
                eta = None

            shipment = Shipment(
                sender_name=sender["name"],
                sender_address=sender["address"],
                sender_phone=sender["phone"],
                receiver_name=receiver["name"],
                receiver_address=receiver["address"],
                receiver_phone=receiver["phone"],
                receiver_email=receiver["email"],
                origin_city=route["origin_city"],
                origin_country=route["origin_country"],
                destination_city=route["dest_city"],
                destination_country=route["dest_country"],
                weight=WEIGHTS[i],
                dimensions=CARGO_DIMS[i],
                cargo_type=CARGO_TYPES[i % len(CARGO_TYPES)],
                status=status,
                estimated_delivery=eta,
                notes=CARGO_NOTES[i],
            )
            shipment.save()
            tracking_numbers.append(shipment.tracking_number)

            # Build timeline events for this shipment
            events = []
            for ev in seq:
                offset_h = random.randint(6, 22)
                ts = now + timedelta(days=ev["offset_days"], hours=offset_h)

                # Derive a meaningful location from the route
                if "Origin" in ev["status"] or "Booked" in ev["status"] or "Picked" in ev["status"]:
                    location = f"{route['origin_city']}, {route['origin_country']}"
                elif "Destination" in ev["status"] or "Delivered" in ev["status"] or "Out for Delivery" in ev["status"] or "Import" in ev["status"]:
                    location = f"{route['dest_city']}, {route['dest_country']}"
                elif "Mid Ocean" in ev["status"] or "In Transit" in ev["status"] or "Transit" in ev["status"]:
                    location = "International Waters / Airspace"
                elif "Hub" in ev["status"]:
                    # Use a plausible intermediate hub city
                    hubs = ["Singapore", "Dubai, UAE", "Amsterdam, Netherlands", "Hong Kong", "Frankfurt, Germany"]
                    location = random.choice(hubs)
                else:
                    location = f"{route['origin_city']}, {route['origin_country']}"

                events.append(ShipmentEvent(
                    shipment=shipment,
                    location=location,
                    status=ev["status"],
                    description=ev["desc"],
                    timestamp=ts,
                ))

            ShipmentEvent.objects.bulk_create(events)

            status_color = {
                "Delivered":       self.style.SUCCESS,
                "In Transit":      self.style.HTTP_INFO,
                "Out for Delivery":self.style.WARNING,
                "Pending":         self.style.HTTP_NOT_MODIFIED,
                "On Hold":         self.style.WARNING,
                "Exception":       self.style.ERROR,
            }.get(status, self.style.SUCCESS)

            self.stdout.write(
                f"  [{i+1:02d}] {self.style.HTTP_INFO(shipment.tracking_number)}"
                f"  {status_color(f'{status:<20}')}"
                f"  {route['origin_city']} → {route['dest_city']}"
                f"  ({len(seq)} events)"
            )

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("  ┌─────────────────────────────────────────────────────────────┐"))
        self.stdout.write(self.style.HTTP_INFO("  │  TRACKING NUMBERS FOR IMMEDIATE TESTING                     │"))
        self.stdout.write(self.style.HTTP_INFO("  ├─────────────────────────────────────────────────────────────┤"))
        for tn in tracking_numbers:
            self.stdout.write(self.style.HTTP_INFO(f"  │  {tn:<57}│"))
        self.stdout.write(self.style.HTTP_INFO("  └─────────────────────────────────────────────────────────────┘"))
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"  ✓  Created {len(tracking_numbers)} shipments with full event timelines."))
        self.stdout.write("")
