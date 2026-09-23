"""Print the reconciliation between our catalog and NAV Fund Services, per row.

    python manage.py navfund_recon
    python manage.py navfund_recon --date 08-09-2026 --segment xlBTC
    python manage.py navfund_recon --flagged-only --threshold 5

Nothing is written anywhere — this is the inspection view of the comparison that
will become the report's extra columns.
"""
from django.core.management.base import BaseCommand


def _fmt(value, places=8, width=18):
    if value is None:
        return " " * (width - 1) + "-"
    return f"{value:>{width}.{places}f}"


class Command(BaseCommand):
    help = "Reconcile our NAV catalog against NAV Fund Services, row by row."

    def add_arguments(self, parser):
        parser.add_argument("--date", metavar="MM-DD-YYYY", default=None,
                            help="NAV report date; default is each fund's last available")
        parser.add_argument("--segment", default=None, help="only this segment, e.g. xlBTC")
        parser.add_argument("--threshold", type=float, default=None,
                            help="flag differences above this USD amount (default 1.0)")
        parser.add_argument("--flagged-only", action="store_true",
                            help="show only rows that breach the threshold")

    def handle(self, *args, **options):
        from navfund import endpoints as ep
        from navfund import mapping, recon
        from navfund.client import NavFundClient, load_credentials_env
        from tracker.services.nav_report import build_report

        load_credentials_env()
        client = NavFundClient()
        threshold = options["threshold"] or recon.DEFAULT_THRESHOLD_USD

        report = build_report()
        funds = {f["FundName"]: f for f in ep.get_fund_list(client)}
        seg_cfg = mapping.load_map()["segments"]

        for seg in report["segments"]:
            segment = seg["segment"]
            if options["segment"] and segment != options["segment"]:
                continue
            if not seg.get("has_data"):
                self.stdout.write(f"{segment}: no snapshot data — skipped")
                continue
            cfg = seg_cfg.get(segment)
            if cfg is None:
                self.stdout.write(f"{segment}: not in account_map.yaml — skipped")
                continue

            fund = funds.get(cfg["fund_name"])
            report_date = options["date"] or ep.nav_date(ep.parse_nav_date(
                fund.get("PortfolioLastAvailableDate")
                or fund.get("FundDailyAccountingLastAvailableDate")))

            # The actual base-asset spot price, not net_usd/net_native — that
            # ratio blends in whatever other assets (ETH/ARBETH gas reserves)
            # the segment also holds, distorting every non-base-asset row.
            base_price = seg.get("price") or (
                seg["net_usd"] / seg["net_native"] if seg.get("net_native") else 1.0)

            result = recon.reconcile_segment(
                segment, report_date, seg.get("sections", []), base_price,
                client=client, threshold_usd=threshold)

            snap = seg.get("timestamp")
            self.stdout.write("")
            self.stdout.write("=" * 132)
            self.stdout.write(
                f"{segment}   NAV date {report_date}   our snapshot "
                f"{snap:%Y-%m-%d %H:%M} UTC   1 {seg.get('base_asset')} = "
                f"${base_price:,.2f}   flag > ${threshold:g}")
            self.stdout.write("=" * 132)
            self.stdout.write(
                f"{'Category':<24}{'Location':<34}{'Item':<14}"
                f"{'our Qty':>18}{'NAV Qty':>18}{'Diff Qty':>18}{'Diff USD':>14}  ")
            self.stdout.write("-" * 132)

            for row in result["rows"]:
                if options["flagged_only"] and not row["flagged"]:
                    continue
                if row["flagged"]:
                    marker = " <<< BREAK"
                elif row["structural"]:
                    marker = f"  (expected: {row['note']})"
                else:
                    marker = ""
                where = "" if row["presence"] == recon.BOTH else f"  [{row['presence']}]"
                self.stdout.write(
                    f"{row['category'][:23]:<24}{row['location'][:33]:<34}"
                    f"{row['asset'][:13]:<14}"
                    f"{_fmt(row['our_qty'])}{_fmt(row['nav_qty'])}{_fmt(row['diff_qty'])}"
                    f"{_fmt(row['diff_usd'], 2, 14)}{where}{marker}")

            self.stdout.write("-" * 132)
            self.stdout.write(
                f"{'TOTAL (USD)':<72}{result['our_total_usd']:>18,.2f}"
                f"{result['nav_total_usd']:>18,.2f}"
                f"{result['diff_total_usd']:>+14,.2f}")
            self.stdout.write(
                f"  rows: {len(result['rows'])}   breaks over ${threshold:g}: "
                f"{result['flagged_count']}   "
                f"of the difference, ${result['structural_usd']:+,.2f} is the "
                f"expected aToken/Aave offset")
            self.stdout.write(
                f"  NAV total (base): {result['nav_total_base']:.8f}   "
                f"(Diff USD column sums to the total above — check it)")
            for row in result["unmapped"]:
                self.stdout.write(
                    f"  unmapped NAV row: {row['nav_account']} / "
                    f"{row['ticker'] or '(none)'} = {row['value_base']:.8f} base")