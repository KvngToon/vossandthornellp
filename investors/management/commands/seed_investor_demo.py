from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from investors.demo_data import INVESTORS, PASSWORD, DemoDataError, DemoSeeder


class Command(BaseCommand):
    help = (
        'Create FAKE demo investors with a rich, realistic history for exploring the portal locally. '
        'Only touches the demo accounts. Refuses unless DEBUG=True and the DB is local SQLite. '
        '(Production uses the superuser-only "Demo data" button in the admin instead.)'
    )

    def add_arguments(self, parser):
        parser.add_argument('--years-back', type=int, default=6,
                            help='How far back the oldest demo investment started (1-10 years, default 6).')
        parser.add_argument('--reset', action='store_true',
                            help="Delete the demo accounts' data first and regenerate it.")
        parser.add_argument('--force', action='store_true', help='Allow running with DEBUG off (still SQLite only).')

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts['force']:
            raise CommandError('Refusing to seed demo data with DEBUG off. Run with DEBUG=True locally.')
        engine = settings.DATABASES['default']['ENGINE']
        if 'sqlite' not in engine:
            raise CommandError(
                f'Refusing to seed demo data into a non-SQLite database ({engine}). '
                'Is DATABASE_URL set to a real database? Unset it to use local SQLite.')
        years = opts['years_back']
        if not 1 <= years <= 10:
            raise CommandError('--years-back must be between 1 and 10.')

        seeder = DemoSeeder(years=years)
        try:
            made = seeder.run(reset=opts['reset'])
        except DemoDataError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f'Demo data ready ({made} investment(s) created; oldest starts about {years} year(s) back). All of it is fake.'))
        self.stdout.write('')
        self.stdout.write(f'  Sign in at /investors/login/ (all passwords: {PASSWORD} unless noted)')
        for username, name, password, note in seeder.credentials:
            extra = f'  [password: {password}]' if password != PASSWORD else ''
            self.stdout.write(f'    {username:<12} {name:<18} {note}{extra}')
        if made == 0 and not opts['reset']:
            self.stdout.write('  (Nothing new: demo data already exists. Use --reset to rebuild it.)')
