from django.core.management.base import BaseCommand

from catalog.models import Dashboard

# The dashboards this project serves. Both are internal Django apps, so there is
# no upstream to configure and nothing separate to start.
DEFAULTS = [
    {
        "slug": "nav",
        "name": "NAV Tracker",
        "description": "Daily NAV across custody, Aave and exchange equity, per XLtoken.",
        "icon": "\N{CHART WITH UPWARDS TREND}",
        "kind": Dashboard.INTERNAL,
        "order": 10,
    },
    {
        "slug": "callspread",
        "name": "Covered Call Spread Risk Monitor",
        "description": "Live greeks, coverage and delta monitor for the CC covered call-spread book.",
        "icon": "\N{TRIANGULAR RULER}",
        "kind": Dashboard.INTERNAL,
        "order": 20,
    },
]


class Command(BaseCommand):
    help = "Create or refresh the built-in dashboard catalog entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Reset name/description/kind on rows that already exist.",
        )

    def handle(self, *args, **options):
        for spec in DEFAULTS:
            slug = spec["slug"]
            fields = {k: v for k, v in spec.items() if k != "slug"}
            dash, created = Dashboard.objects.get_or_create(slug=slug, defaults=fields)
            if created:
                self.stdout.write(self.style.SUCCESS(f"created  {slug}  ({dash.kind})"))
            elif options["overwrite"]:
                for key, value in fields.items():
                    setattr(dash, key, value)
                # Moving a dashboard in-house makes its old upstream meaningless.
                if dash.kind == Dashboard.INTERNAL:
                    dash.upstream = ""
                dash.save()
                self.stdout.write(self.style.WARNING(f"updated  {slug}  ({dash.kind})"))
            else:
                self.stdout.write(f"exists   {slug}  ({dash.kind})")