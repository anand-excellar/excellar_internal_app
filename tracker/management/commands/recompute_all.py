from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = "Recompute all daily PnL rows from existing snapshots"

    def handle(self, *args, **options):
        from tracker.models import Snapshot
        from tracker.services.collector import finalize_daily

        from tracker.config import load_tokens
        for segment, token in load_tokens().items():
            wallet_cfg = token.to_dict()
            if not wallet_cfg.get("enabled", False):
                continue
            dates = (
                Snapshot.objects
                .filter(segment=segment)
                .dates("timestamp", "day")
            )
            for d in dates:
                date_str = d.isoformat()
                finalize_daily(segment, date_str)
                self.stdout.write(f"Recomputed {segment} for {date_str}")
