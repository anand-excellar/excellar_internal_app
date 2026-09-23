"""Put our catalog and NAV Fund Services' numbers side by side, and diff them.

The join key is ``(category, location, asset)`` — our Detail tab's own
coordinates. NAV's side reaches those coordinates via ``account_map.yaml``
(which account belongs to which of our labels) and its ticker renames (NAV's
``ETH_GAS`` is our ``ARBETH`` on Arbitrum, our ``ETH`` on mainnet).

Two differences are reported per row because they fail differently:

* ``diff_qty``  — native units. Price-independent, so a non-zero here is a real
  position difference: a missing wallet, an unrecorded transfer, an asset our
  scrapers don't see.
* ``diff_usd``  — value. Non-zero even when quantities agree, because NAV values
  at their settlement price and we value at ours.

Both sides are converted to USD for flagging so one threshold covers all three
funds: NAV's base-currency figures are multiplied by the same segment price the
report already uses (BTC for xlBTC, ETH for xlETH, 1.0 for xlUSD).

Rows present on only one side are kept, not silently dropped — a position NAV
knows about and we don't is the single most important thing this can find.
"""
import logging

from navfund import endpoints as ep
from navfund import mapping, normalize
from navfund.client import NavFundClient

logger = logging.getLogger(__name__)

# A difference is flagged when its USD equivalent exceeds this.
DEFAULT_THRESHOLD_USD = 1.0

ONLY_OURS = "ours only"
ONLY_NAV = "NAV only"
BOTH = "both"


def _key(category: str, location: str, asset: str) -> tuple:
    """Whitespace- and case-insensitive join key.

    Raw string equality is not safe here: at least one BitGo label carries a
    stray leading space (" xlBTC Hot Wallet · eth"), which silently splits a
    matched wallet into an "ours only" and a "NAV only" row — the exact failure
    this tool exists to detect, manufactured out of nothing.
    """
    return tuple(" ".join(str(part or "").split()).casefold()
                 for part in (category, location, asset))


def _flatten_our_catalog(segment: str, sections: list) -> dict:
    """Our catalog sections → ``{(category, location, asset): {qty, usd, price}}``.

    Mirrors ``gsheets._detail_rows_for_segment``'s location handling: a custody
    wallet's location is "label · network", an exchange venue's is the venue name,
    Aave and Spark carry their own. Only real asset rows are taken — the
    "Account equity" / "Available to withdraw" / "Total exchange equity" lines are
    summaries, not positions, and would double-count.
    """
    out = {}
    for section in sections or []:
        category = section.get("title", "")
        lowered = category.lower()
        is_exchange = lowered.startswith("exchange")
        is_aave = lowered.startswith("aave")
        is_spark = lowered.startswith("spark")

        location = ""
        for row in section.get("rows", []):
            if row.get("wallet_header"):
                name = row.get("wallet_header", "")
                net = str(row.get("wallet_net") or "")
                location = name if is_exchange else (f"{name} · {net}" if net else name)
                continue
            if row.get("total_row") or row.get("kv_row"):
                continue  # summary line, not a position

            asset = row.get("asset")
            if not asset:
                continue
            if is_aave:
                where = "Aave"
            elif is_spark:
                sub = row.get("sub") or ""
                where = sub.split("·")[0].strip() if sub else "Spark"
            else:
                where = location

            bucket = out.setdefault(_key(category, where, asset), {
                "category": category, "location": where, "asset": asset,
                "qty": 0.0, "usd": 0.0, "price": None, "has_value": False})
            if row.get("qty") is not None:
                bucket["qty"] += float(row["qty"])
            if row.get("value") is not None:
                bucket["usd"] += float(row["value"])
                bucket["has_value"] = True
            if bucket["price"] is None and row.get("price") is not None:
                bucket["price"] = float(row["price"])
    return out


def _flatten_nav_rows(nav_rows: list) -> dict:
    """NAV's normalized rows → the same ``(category, location, asset)`` keying."""
    out = {}
    for bucket in normalize.rollup_by_asset(nav_rows).values():
        out[_key(bucket["category"], bucket["location"], bucket["our_asset"])] = bucket
    return out


def reconcile_segment(segment: str, report_date, sections: list, base_price: float,
                      client=None, threshold_usd: float = DEFAULT_THRESHOLD_USD) -> dict:
    """Compare one segment for one date.

    ``sections`` and ``base_price`` come from the report we already build, so the
    "ours" side of every number is literally what the report publishes.
    """
    client = client or NavFundClient()
    fund_id = mapping.fund_ids().get(segment)
    if fund_id is None:
        raise ValueError(f"{segment} is not in account_map.yaml")

    raw = ep.get_trading_gain_loss(client, fund_id, report_date)
    normalized = normalize.normalize_rows(raw)
    nav_side = _flatten_nav_rows(normalized["rows"])
    our_side = _flatten_our_catalog(segment, sections)

    price = float(base_price or 0.0)
    rows = []
    for key in sorted(set(our_side) | set(nav_side)):
        ours = our_side.get(key)
        theirs = nav_side.get(key)
        # Display our spelling where we have one, else NAV's.
        source = ours or theirs
        category = source["category"]
        location = source["location"]
        asset = source["asset"] if ours else theirs["our_asset"]

        our_qty = ours["qty"] if ours else None
        nav_qty = theirs["qty"] if theirs else None
        our_usd = ours["usd"] if (ours and ours["has_value"]) else None

        # Price NAV's quantity on OUR OWN per-asset price when we have one, not
        # the segment's blended base_price: custody data and our own live price
        # feed can be carried forward from different, drifting snapshots (see
        # nav/views.py's per-field staleness fill), so valuing NAV's reported
        # quantity at "whatever price build_report() happened to have" makes an
        # exact quantity match look like a large break purely from that price
        # drift. Using the row's own price ties diff_usd to diff_qty, which is
        # what a real break actually is. Only a row with no our-side price
        # (present on NAV's side only) falls back to the segment price.
        row_price = ours.get("price") if ours else None
        if theirs is None:
            nav_usd = None
        elif row_price:
            nav_usd = theirs["qty"] * row_price
        else:
            nav_usd = theirs["value_base"] * price

        diff_qty = None if (our_qty is None or nav_qty is None) else our_qty - nav_qty
        # Always our USD minus theirs, treating an absent side as zero, so the
        # column sums exactly to the total difference and the whole table can be
        # checked by adding it up.
        diff_usd = (our_usd or 0.0) - (nav_usd or 0.0)

        # Known structural offsets, not breaks. We leave aTokens unpriced on the
        # wallet row and value the position once under Aave; NAV prices the
        # aToken on the wallet row and has no Aave row. The pair nets to ~0, so
        # each side is annotated and kept out of the breach count rather than
        # hidden — see the recon spec in README.md.
        note, structural = "", False
        is_atoken = asset.upper().startswith(("ETH:AETH", "AETH"))
        if is_atoken and our_usd is None:
            note = "we leave aTokens unpriced here; valued once under Aave position"
            structural = True
        elif category.lower().startswith("aave") and theirs is None and ours:
            note = "NAV books this as an aToken on the wallet row"
            structural = True

        rows.append({
            "category": category, "location": location, "asset": asset,
            "our_qty": our_qty, "nav_qty": nav_qty, "diff_qty": diff_qty,
            "our_usd": our_usd, "nav_usd": nav_usd, "diff_usd": diff_usd,
            "nav_value_base": theirs["value_base"] if theirs else None,
            "nav_accounts": theirs["nav_accounts"] if theirs else [],
            "presence": BOTH if (ours and theirs) else (ONLY_OURS if ours else ONLY_NAV),
            "note": note, "structural": structural,
            "flagged": (not structural) and abs(diff_usd) > threshold_usd,
        })

    return {
        "segment": segment,
        "report_date": report_date,
        "base_price": price,
        "threshold_usd": threshold_usd,
        "rows": rows,
        "unmapped": normalized["unmapped"],
        "nav_total_base": sum(r["value_base"] for r in normalized["rows"]),
        "our_total_usd": sum(r["our_usd"] or 0.0 for r in rows),
        "nav_total_usd": sum(r["nav_usd"] or 0.0 for r in rows),
        "diff_total_usd": sum(r["diff_usd"] for r in rows),
        "structural_usd": sum(r["diff_usd"] for r in rows if r["structural"]),
        "flagged_count": sum(1 for r in rows if r["flagged"]),
    }


def lookup(result: dict) -> dict:
    """``{join key: row}`` for a reconciliation, for annotating existing rows."""
    return {_key(r["category"], r["location"], r["asset"]): r
            for r in result.get("rows", [])}


def reconcile_report(report: dict, report_date=None, client=None,
                     threshold_usd: float = DEFAULT_THRESHOLD_USD) -> dict:
    """Reconcile every segment of a built report. ``{segment: result}``.

    ``report_date`` pins NAV's side for all segments; omitted, each fund is asked
    for its own last available date. Segments with no snapshot, or absent from
    ``account_map.yaml``, are skipped — the caller reports them.
    """
    client = client or NavFundClient()
    fund_ids = mapping.fund_ids()
    segments_cfg = mapping.load_map()["segments"]
    funds = {f["FundName"]: f for f in ep.get_fund_list(client)}

    out = {}
    for seg in report.get("segments", []):
        segment = seg["segment"]
        cfg = segments_cfg.get(segment)
        if not seg.get("has_data") or cfg is None or segment not in fund_ids:
            continue

        date_for_seg = report_date
        if date_for_seg is None:
            fund = funds.get(cfg["fund_name"]) or {}
            date_for_seg = ep.nav_date(ep.parse_nav_date(
                fund.get("PortfolioLastAvailableDate")
                or fund.get("FundDailyAccountingLastAvailableDate")))

        # Must be the actual base-asset spot price, not net_usd/net_native: that
        # ratio is a blended rate across every asset the segment holds (a
        # BTC-segment wallet can carry ETH/ARBETH gas reserves too), and using it
        # to convert NAV's base-currency value on a non-base-asset row produces a
        # diff_usd that is pure price-mix artifact — a near-zero diff_qty next to
        # a large diff_usd is the signature of this bug, not a real break.
        base_price = seg.get("price") or (
            seg["net_usd"] / seg["net_native"] if seg.get("net_native") else 1.0)
        try:
            out[segment] = reconcile_segment(
                segment, date_for_seg, seg.get("sections", []), base_price,
                client=client, threshold_usd=threshold_usd)
        except Exception as exc:
            logger.warning("Reconciliation failed for %s: %s", segment, exc)
            out[segment] = {"segment": segment, "report_date": date_for_seg,
                            "error": str(exc), "rows": [], "unmapped": []}
    return out