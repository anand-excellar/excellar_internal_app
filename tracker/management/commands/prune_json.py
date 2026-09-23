from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = "Remove raw DeBank JSON from snapshots older than N days"

    def add_arguments(self, parser):
        parser.add_argument("--older-than", type=int, default=30,
                            help="Prune snapshots older than this many days")

    def handle(self, *args, **options):
        from tracker.models import Snapshot
        cutoff = timezone.now() - timedelta(days=options["older_than"])
        count = Snapshot.objects.filter(
            timestamp__lt=cutoff,
            raw_debank_json__isnull=False,
        ).update(raw_debank_json=None)
        self.stdout.write(
            f"Pruned raw JSON from {count} snapshots older than {options['older_than']} days"
        )
