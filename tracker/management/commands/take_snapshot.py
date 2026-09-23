import asyncio
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Take a snapshot cycle immediately"

    def handle(self, *args, **options):
        from tracker.services.collector import collect_snapshot_cycle
        row_ids = asyncio.run(collect_snapshot_cycle())
        for rid in row_ids:
            self.stdout.write(f"Snapshot #{rid} stored")
        if not row_ids:
            self.stdout.write("No enabled segments — nothing collected")
