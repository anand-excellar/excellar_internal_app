from django.core.management.base import BaseCommand
from django.conf import settings
from datetime import datetime, timezone, timedelta


class Command(BaseCommand):
    help = "Insert an initial reference snapshot from known values"

    def add_arguments(self, parser):
        parser.add_argument("--segment", required=True)
        parser.add_argument("--date", default=None,
                            help="Reference date ISO format YYYY-MM-DD (default: yesterday)")
        parser.add_argument("--btc-price", type=float, default=0.0)
        parser.add_argument("--eth-price", type=float, default=0.0)
        parser.add_argument("--aave-supply-amount", type=float, default=0.0)
        parser.add_argument("--aave-supply-usd", type=float, default=0.0)
        parser.add_argument("--aave-borrow-usdc", type=float, default=0.0)
        parser.add_argument("--aave-health-rate", type=float, default=0.0)
        parser.add_argument("--aave-net-usd", type=float, default=0.0)
        parser.add_argument("--price-diff", type=float, default=0.0)
        parser.add_argument("--cross-mtm-entry-cost", type=float, default=0.0)
        parser.add_argument("--cross-mtm-position-size", type=float, default=0.0)
        parser.add_argument("--susds-eth-balance", type=float, default=0.0)
        parser.add_argument("--susds-arbi-balance", type=float, default=0.0)
        parser.add_argument("--portfolio-value", type=float, default=0.0)

    def handle(self, *args, **options):
        from tracker.models import Snapshot
        from tracker.calculator.pnl import compute_cross_mtm
        from tracker.config import load_tokens

        segment = options["segment"]
        tokens = load_tokens(settings)
        if segment not in tokens:
            self.stderr.write(f"Segment '{segment}' not found in config.yaml")
            return

        if options["date"]:
            from datetime import date as dt
            ref_date = dt.fromisoformat(options["date"])
            ref_dt = datetime(ref_date.year, ref_date.month, ref_date.day,
                              23, 59, 0, tzinfo=timezone.utc)
            timestamp = ref_dt
        else:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            timestamp = yesterday.replace(hour=23, minute=59, second=0, microsecond=0)

        cross_mtm_value = compute_cross_mtm(
            options["price_diff"],
            options["cross_mtm_entry_cost"],
            options["cross_mtm_position_size"],
        )

        wallet_cfg = tokens[segment].to_dict()

        snap = Snapshot.objects.create(
            timestamp=timestamp,
            segment=segment,
            wallet_address=wallet_cfg.get("address", ""),
            btc_price=options["btc_price"],
            eth_price=options["eth_price"],
            aave_supply_amount=options["aave_supply_amount"],
            aave_supply_usd=options["aave_supply_usd"],
            aave_borrow_usdc=options["aave_borrow_usdc"],
            aave_health_rate=options["aave_health_rate"],
            aave_net_usd=options["aave_net_usd"],
            susds_eth_balance=options["susds_eth_balance"] or None,
            susds_arbi_balance=options["susds_arbi_balance"] or None,
            price_diff_hl_bn=options["price_diff"],
            cross_mtm_value=cross_mtm_value,
            pnl_usdc=0.0,
            pnl_native=0.0,
            pnl_pct=0.0,
            portfolio_value=options["portfolio_value"],
            annualized_return=0.0,
            scrape_success=False,
        )
        self.stdout.write(f"Reference snapshot #{snap.pk} inserted for {segment} at {timestamp}")
