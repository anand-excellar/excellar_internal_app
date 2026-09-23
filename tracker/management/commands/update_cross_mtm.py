from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Update cross MtM config for an asset (BTC/ETH/USDC)"

    def add_arguments(self, parser):
        parser.add_argument("asset", help="Asset symbol e.g. BTC")
        parser.add_argument("--entry-cost", type=float, required=True)
        parser.add_argument("--position-size", type=float, required=True)

    def handle(self, *args, **options):
        from tracker.models import CrossMtmConfig
        now = timezone.now()
        CrossMtmConfig.objects.filter(
            asset=options["asset"], effective_until__isnull=True
        ).update(effective_until=now)
        CrossMtmConfig.objects.create(
            asset=options["asset"],
            entry_cost=options["entry_cost"],
            position_size=options["position_size"],
            effective_from=now,
        )
        self.stdout.write(
            f"Updated cross MtM for {options['asset']}: "
            f"entry_cost={options['entry_cost']}, position_size={options['position_size']}"
        )
