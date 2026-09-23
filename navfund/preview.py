"""Print NAV's numbers in our Detail-tab shape, for a date.

The reconciliation's data path end to end — fetch, map to our categories and
labels, roll the gas/reward sub-accounts into one line — without Django or Google
in the way. Use it to eyeball a day before wiring it into the report, and to see
what is still unmapped.

    python -m navfund.preview                     # latest available date
    python -m navfund.preview --date 08-09-2026
    python -m navfund.preview --date 08-09-2026 --segment xlBTC
"""
import argparse
import os
import sys

if __package__ in (None, ""):  # allow `python navfund/preview.py ...`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from navfund import endpoints as ep  # noqa: E402
from navfund import mapping, normalize  # noqa: E402
from navfund.client import NavFundClient, load_credentials_env  # noqa: E402

CATEGORY_ORDER = ["Native reserve", "Aave position", "Spark sUSDS",
                  "Exchange funds & perp exposure"]


def _category_rank(name):
    return CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER)


def preview_segment(client, segment: str, fund_id: int, report_date, base_asset: str):
    rows_raw = ep.get_trading_gain_loss(client, fund_id, report_date)
    result = normalize.normalize_rows(rows_raw)
    by_asset = normalize.rollup_by_asset(result["rows"])
    by_location = normalize.rollup_by_location(result["rows"])

    print(f"\n{'=' * 104}")
    print(f"{segment}   {report_date}   ({len(rows_raw)} NAV rows, base {base_asset})")
    print(f"{'=' * 104}")
    print(f"{'Category':<32}{'Location (where)':<38}{'Item':<14}"
          f"{'Qty':>16}{'Value (Base)':>18}")

    ordered = sorted(by_asset.values(),
                     key=lambda r: (_category_rank(r["category"]), r["location"],
                                    r["our_asset"]))
    last = (None, None)
    for row in ordered:
        cat = row["category"] if row["category"] != last[0] else ""
        loc = row["location"] if (row["category"], row["location"]) != last else ""
        print(f"{cat:<32}{loc:<38}{row['our_asset']:<14}"
              f"{row['qty']:>16.8f}{row['value_base']:>18.8f}")
        last = (row["category"], row["location"])

    print(f"\n{'-' * 104}\nRolled up to our labels")
    for key in sorted(by_location, key=lambda k: (_category_rank(k[1]), k[2])):
        bucket = by_location[key]
        print(f"  {bucket['location']:<38}{bucket['value_base']:>18.8f}"
              f"   <- {len(bucket['nav_accounts'])} NAV account(s)")

    mapped = sum(r["value_base"] for r in result["rows"])
    orphan = sum(r["value_base"] for r in result["unmapped"])
    print(f"\n  {'MAPPED TOTAL':<38}{mapped:>18.8f}")
    if result["unmapped"]:
        print(f"  {'UNMAPPED TOTAL':<38}{orphan:>18.8f}")
        for row in result["unmapped"]:
            note = f" — {row['note'][:70]}" if row["note"] else ""
            print(f"      {row['nav_account']} / {row['ticker'] or '(none)'}: "
                  f"{row['value_base']:.8f}{note}")
    print(f"  {'NAV ROWS TOTAL':<38}{mapped + orphan:>18.8f}")

    # Independent check against NAV's own balance sheet. The per-account rows tie
    # to End Computed Equity; Ending Net Asset Value is that minus liabilities
    # (accrued incentive fees), so it is shown for context but not diffed.
    try:
        equity = ep.computed_equity_series(client, fund_id, report_date, report_date)
        nav = ep.ending_nav_series(client, fund_id, report_date, report_date)
    except Exception as exc:
        print(f"  (balance sheet unavailable: {exc})")
        return
    total = mapped + orphan
    for day, value in equity.items():
        print(f"  {'BS End Computed Equity':<38}{value:>18.8f}"
              f"   diff {total - value:+.8f}   <- the tie-out")
    for day, value in nav.items():
        print(f"  {'BS Ending Net Asset Value':<38}{value:>18.8f}"
              f"   (after liabilities: {value - equity.get(day, value):+.8f})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", metavar="MM-DD-YYYY", default=None,
                        help="report date; default is each fund's last available")
    parser.add_argument("--segment", default=None, help="only this segment, e.g. xlBTC")
    args = parser.parse_args(argv)

    load_credentials_env()
    client = NavFundClient()
    cfg = mapping.load_map()["segments"]
    funds = {f["FundName"]: f for f in ep.get_fund_list(client)}

    for segment, seg_cfg in cfg.items():
        if args.segment and segment != args.segment:
            continue
        fund = funds.get(seg_cfg["fund_name"])
        if fund is None:
            print(f"{segment}: {seg_cfg['fund_name']} not on the tenant — skipped")
            continue
        report_date = args.date or ep.nav_date(
            ep.parse_nav_date(fund.get("PortfolioLastAvailableDate")
                              or fund.get("FundDailyAccountingLastAvailableDate")))
        preview_segment(client, segment, seg_cfg["global_fund_id"],
                        report_date, seg_cfg.get("base_asset", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())