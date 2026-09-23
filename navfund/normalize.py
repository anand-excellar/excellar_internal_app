"""Turn NAV's trading-gain-loss rows into rows shaped like our Detail tab.

Two levels come out of the same input, because the two answer different questions
and only one of them can be expressed in native units:

* **per-asset rows** carry NAV's ``Quantity`` in the ticker's own units, so they
  line up against our ``Qty`` column and a difference there is a real position
  break.
* **rollup rows** carry ``MarketValueBase`` summed over every NAV account and
  ticker that maps to one of our labels. Summing has to happen in base currency —
  you cannot add a USDC position to an ``ETH_GAS`` position in native units — and
  that is precisely why NAV's per-wallet gas/reward sub-accounts collapse into
  the single label we show.

Nothing is dropped: rows whose account isn't in ``account_map.yaml`` come back in
``unmapped``, with our recorded reason where we have one.
"""
import logging
from collections import defaultdict

from navfund import mapping

logger = logging.getLogger(__name__)

# Field names in NAV's GetTradingGainLossForFund response.
F_ACCOUNT = "AccName"
F_TICKER = "Ticker"
F_QTY = "Quantity"
F_PRICE_BASE = "PriceBase"
F_VALUE_BASE = "MarketValueBase"
F_SECURITY_TYPE = "SecurityType"


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def normalize_rows(nav_rows, map_path=mapping.MAP_FILE) -> dict:
    """Split NAV's rows into mapped per-asset rows and unmapped leftovers.

    Returns ``{"rows": [...], "unmapped": [...]}``. Each mapped row::

        {segment, category, location, nav_account, ticker, our_asset,
         qty, price_base, value_base, security_type}
    """
    rows, unmapped = [], []

    for raw in nav_rows or []:
        account = raw.get(F_ACCOUNT) or ""
        ticker = raw.get(F_TICKER) or ""
        common = {
            "nav_account": account,
            "ticker": ticker,
            "qty": _num(raw.get(F_QTY)),
            "price_base": _num(raw.get(F_PRICE_BASE)),
            "value_base": _num(raw.get(F_VALUE_BASE)),
            "security_type": raw.get(F_SECURITY_TYPE) or "",
        }

        resolved = mapping.resolve(account, ticker, map_path)
        if resolved is None:
            unmapped.append({**common,
                             "note": mapping.unmapped_note(account, ticker, map_path)})
            continue

        rows.append({**common,
                     "segment": resolved["segment"],
                     "category": resolved["category"],
                     "location": resolved["location"],
                     "our_asset": resolved["our_asset"]})

    return {"rows": rows, "unmapped": unmapped}


def rollup_by_location(rows) -> dict:
    """``{(segment, category, location): {...}}`` — one entry per our-side label.

    ``value_base`` is the sum across every NAV account/ticker mapping to that
    label; ``nav_accounts`` lists which ones contributed, so a surprising total
    can be traced without re-querying.
    """
    out = {}
    for row in rows:
        key = (row["segment"], row["category"], row["location"])
        bucket = out.setdefault(key, {
            "segment": row["segment"], "category": row["category"],
            "location": row["location"], "value_base": 0.0,
            "nav_accounts": [], "tickers": [],
        })
        bucket["value_base"] += row["value_base"]
        if row["nav_account"] not in bucket["nav_accounts"]:
            bucket["nav_accounts"].append(row["nav_account"])
        if row["our_asset"] not in bucket["tickers"]:
            bucket["tickers"].append(row["our_asset"])
    return out


def rollup_by_asset(rows) -> dict:
    """``{(segment, location, our_asset): {qty, value_base, ...}}``.

    Keyed on **our** asset name, so NAV's per-wallet gas account lands on the same
    key as the wallet's native coin — e.g. ``ETH_GAS`` in the Arbitrum gas account
    becomes ``ARBETH`` on the Arbitrum wallet, which is the row our Qty column
    holds. Several NAV rows can therefore contribute to one key.
    """
    out = {}
    for row in rows:
        key = (row["segment"], row["location"], row["our_asset"])
        bucket = out.setdefault(key, {
            "segment": row["segment"], "category": row["category"],
            "location": row["location"], "our_asset": row["our_asset"],
            "qty": 0.0, "value_base": 0.0, "price_base": None, "nav_accounts": [],
        })
        bucket["qty"] += row["qty"]
        bucket["value_base"] += row["value_base"]
        # Price is a property of the ticker, not additive; keep the first non-zero.
        if not bucket["price_base"] and row["price_base"]:
            bucket["price_base"] = row["price_base"]
        if row["nav_account"] not in bucket["nav_accounts"]:
            bucket["nav_accounts"].append(row["nav_account"])
    return out


def segment_totals(rows, unmapped=()) -> dict:
    """``{segment: base-currency total}``, mapped rows only.

    ``unmapped`` is accepted so callers can report how much value sits outside
    the mapping: a segment total that ties to NAV's balance sheet only once the
    unmapped rows are added back means the mapping is incomplete, not that NAV
    disagrees with us.
    """
    totals = defaultdict(float)
    for row in rows:
        totals[row["segment"]] += row["value_base"]
    return dict(totals)