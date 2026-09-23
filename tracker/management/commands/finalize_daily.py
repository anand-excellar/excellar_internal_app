from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Finalize daily PnL for a given date (YYYY-MM-DD)"

    def add_arguments(self, parser):
        parser.add_argument("date", help="ISO date e.g. 2026-04-14")

    def handle(self, *args, **options):
        from tracker.services.collector import finalize_daily_for_date
        finalize_daily_for_date(options["date"])
        self.stdout.write(f"Finalized daily PnL for {options['date']}")
