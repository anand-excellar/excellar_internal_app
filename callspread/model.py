"""Turns a raw STS `/v1alpha/positions` payload into the tracker snapshot.

A line-by-line port of the original Node `src/model.js`. The maths, the field
names and the JSON shape are deliberately identical — the browser code in
static/callspread/ is the original unchanged, so this must produce exactly what
it already consumes. `tests/test_parity.py` checks that against the Node
implementation rather than trusting the translation.

---------------------------------------------------------------------------
STS greek conventions (confirmed against the worked example in the API docs)
---------------------------------------------------------------------------
STS reports every greek already money-scaled for the WHOLE position, in the
quote currency (USDT ~ USD). To recover the textbook per-contract ("raw") greek
we invert their scaling:

  deltaUsd  = D * qty * S              ->  D = deltaUsd / (qty * S)
  gammaUsd  = G * qty * 0.01 * S^2     ->  G = gammaUsd / (qty * 0.01 * S^2)
              (gammaUsd is the change in deltaUsd per 1% spot move)
  vegaUsd   = V * qty                  ->  V = vegaUsd / qty   (per 1 vol point)
  thetaUsd  = T * qty                  ->  T = thetaUsd / qty  (per day)
  rhoUsd    = R * qty                  ->  R = rhoUsd / qty    (per 1% rate)

Because qty carries the position's sign, the recovered raw greeks are always the
per-contract greeks of the OPTION ITSELF (a call's delta is positive whether we
are long or short it). The signed money greeks carry the position risk.
"""

import math
import re
from datetime import datetime, timedelta, timezone

CASH = "Cash"
OPTION = "Option"

_STRIKE_TYPE_RE = re.compile(r"-(\d+(?:\.\d+)?)-([CP])(?:-|$)")
_YMD_RE = re.compile(r"-(\d{4})(\d{2})(\d{2})-")

# JS Date#toISOString always renders exactly three fractional digits and a "Z".
# Python's isoformat() gives microseconds and "+00:00", which would change every
# timestamp the dashboard and history files carry.
_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def iso(dt: datetime) -> str:
    """Render a datetime the way JavaScript's toISOString() does."""
    dt = dt.astimezone(timezone.utc)
    return f"{dt.strftime(_ISO_FMT)}.{dt.microsecond // 1000:03d}Z"


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value):
    """Parse the date shapes STS emits into an aware datetime, or None.

    Mirrors `new Date(value)` for the inputs that actually occur: ISO strings
    (with Z, with an offset, or with none at all) and epoch milliseconds.

    The no-offset case matters. STS sends `transactTime` as
    "2026-07-22T00:38:43.701" with no zone, and ECMA-262 says a date-*time* with
    no offset is LOCAL time while a date-only string is UTC. Python's default is
    to treat naive input as UTC either way, which shifted every trade timestamp
    by the machine's offset. Following the JS rule keeps this port faithful.

    (That rule also means the original renders `openedAt` differently depending
    on the server's timezone — a pre-existing quirk, not one introduced here.
    On a UTC host, which EC2 is by default, the two agree exactly.)
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None

    date_only = bool(_DATE_ONLY_RE.match(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        if date_only:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            # A naive datetime passed to astimezone() is interpreted as local
            # time — exactly what the JS engine does here.
            parsed = parsed.astimezone(timezone.utc)
    return parsed


def num(v) -> float:
    """JavaScript Number(v), with null/undefined/NaN collapsing to 0."""
    if v is None or isinstance(v, bool):
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(f) else f


def fin(v) -> bool:
    """Number.isFinite: a real, finite number — not None, not a string."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def safe_div(a, b):
    return None if abs(b) < 1e-15 else a / b


def parse_instrument(position: dict) -> dict:
    """Parse `STS-CC-USDT-CC-20260821-0.135-C-E-V` / `CC-USDT-21AUG26-0.135-C`.

    Naming traps in the live payload, confirmed against production:
     - Call/put lives in `subType` ("C"), NOT in `optionType`. On the real CC
       book `optionType` is the *exercise* subtype ("V" = vanilla).
     - `/v1alpha/instruments` spells the same field lowercase `subtype`.
     - `shortInstrumentCode` is already the human label, so prefer it.
    Every field falls back to the instrument code independently, so a missing
    detail never takes the whole object down with it.
    """
    d = position.get("instrumentDetails") or {}
    code = str(d.get("normalizedInstrumentCode") or position.get("instrument") or "")

    strike_type = _STRIKE_TYPE_RE.search(code)
    ymd = _YMD_RE.search(code)

    expiry = None
    if d.get("expiry"):
        parsed_expiry = parse_date(d["expiry"])
        expiry = iso(parsed_expiry) if parsed_expiry else None
    if not expiry and ymd:
        expiry = iso(datetime(int(ymd[1]), int(ymd[2]), int(ymd[3]), 8, 0, 0, tzinfo=timezone.utc))

    call_put = d.get("subType") or d.get("subtype") or (strike_type[2] if strike_type else None)

    if d.get("strike") is not None:
        strike = num(d["strike"])
    elif strike_type:
        strike = float(strike_type[1])
    else:
        strike = None

    return {
        "code": code,
        "shortCode": d.get("shortInstrumentCode") or None,
        "base": d.get("baseCurrency") or position.get("currency") or None,
        "quote": d.get("quoteCurrency") or None,
        "strike": strike,
        "optionType": call_put,
        "exerciseStyle": d.get("observationStyle") or None,  # "E" = European
        "settlement": d.get("settlementCurrency") or None,
        "expiry": expiry,
    }


def short_label(parsed: dict) -> str:
    # STS already ships a display label — use it rather than reassembling one.
    if parsed["shortCode"]:
        return parsed["shortCode"]
    if parsed["strike"] is None:
        return parsed["code"]
    exp = ""
    if parsed["expiry"]:
        dt = parse_date(parsed["expiry"])
        if dt:
            # en-GB "21 Aug 26" with spaces stripped and uppercased -> 21AUG26
            exp = f"{dt.day:02d}{dt.strftime('%b')}{dt.strftime('%y')}".upper()
    strike = parsed["strike"]
    strike_text = f"{strike:g}"
    return f"{parsed['base']}-{strike_text}-{parsed['optionType']}" + (f" {exp}" if exp else "")


def build_leg(position: dict, spot: float) -> dict:
    """Per-leg view: signed money greeks as reported + recovered raw greeks."""
    parsed = parse_instrument(position)
    qty = num(position.get("quantity"))
    S = num(position.get("spotPrice")) or spot

    delta_usd = num(position.get("deltaUsd"))
    gamma_usd = num(position.get("gammaUsd"))
    vega_usd = num(position.get("vegaUsd"))
    theta_usd = num(position.get("thetaUsd"))
    rho_usd = num(position.get("rhoUsd"))
    vanna_usd = num(position.get("vannaUsd"))
    volga_usd = num(position.get("volgaUsd"))

    notional = qty * S
    mark_value = num(position.get("currentMarketValue"))

    is_itm = position.get("isITM")
    if is_itm is None:
        is_itm = (S > parsed["strike"]) if parsed["strike"] is not None else None

    days_to_expiry = position.get("daysToExpiry")
    days_to_expiry = None if days_to_expiry is None else num(days_to_expiry)

    return {
        "code": parsed["code"],
        "label": short_label(parsed),
        "side": "SHORT" if qty < 0 else "LONG",
        "strike": parsed["strike"],
        "optionType": parsed["optionType"],
        "exerciseStyle": parsed["exerciseStyle"],
        "expiry": parsed["expiry"],
        "daysToExpiry": days_to_expiry,
        "accountName": position.get("accountName") or None,
        "quantity": qty,
        "spot": S,
        "isITM": is_itm,
        "moneyness": (S / parsed["strike"] - 1) if parsed["strike"] else None,

        "markValue": mark_value,
        "markPerContract": safe_div(mark_value, qty),

        # As reported by STS — signed, position-level, quote currency.
        "usd": {
            "delta": delta_usd, "gamma": gamma_usd, "vega": vega_usd,
            "theta": theta_usd, "rho": rho_usd, "vanna": vanna_usd, "volga": volga_usd,
        },

        # Recovered per-contract greeks.
        "raw": {
            "delta": safe_div(delta_usd, notional),
            "gamma": safe_div(gamma_usd, qty * 0.01 * S * S),
            "vega": safe_div(vega_usd, qty),
            "theta": safe_div(theta_usd, qty),
            "rho": safe_div(rho_usd, qty),
            "vanna": safe_div(vanna_usd, qty),
            "volga": safe_div(volga_usd, qty),
        },

        # Delta expressed in units of the underlying — the currency the coverage
        # policy is actually written in.
        "deltaCcUnits": safe_div(delta_usd, S),
    }


def aggregate_fills(trades) -> dict:
    """Aggregate fills per instrument from `/v1alpha/trades`.

    Premium cash flow is signed by side: selling an option BRINGS IN premium,
    buying one PAYS it out. That signed sum is the "realized" leg of the P&L
    split — cash that has already changed hands, whatever the position does next.

    Uses `lastQty`/`lastPx` (the actual fill) rather than `orderQty`/`price`
    (what was asked for), so partial fills and price improvement are correct.
    """
    by_symbol: dict = {}
    for t in trades if isinstance(trades, list) else []:
        symbol = t.get("symbol")
        if not symbol:
            continue
        qty = num(t.get("lastQty")) or num(t.get("orderQty"))
        px = num(t.get("lastPx")) or num(t.get("price"))
        if not qty or not px:
            continue

        is_sell = str(t.get("sideText") or "").lower() == "sell" or str(t.get("side")) == "2"
        premium = qty * px if is_sell else -qty * px  # cash in on a sell, out on a buy
        signed_qty = -qty if is_sell else qty

        row = by_symbol.get(symbol)
        if row is None:
            row = {"symbol": symbol, "premium": 0.0, "signedQty": 0.0, "absQty": 0.0,
                   "notional": 0.0, "fills": 0, "firstAt": None, "lastAt": None}
            by_symbol[symbol] = row
        row["premium"] += premium
        row["signedQty"] += signed_qty
        row["absQty"] += qty
        row["notional"] += qty * px
        row["fills"] += 1
        at_dt = parse_date(t.get("transactTime")) if t.get("transactTime") else None
        at = iso(at_dt) if at_dt else None
        if at:
            if not row["firstAt"] or at < row["firstAt"]:
                row["firstAt"] = at
            if not row["lastAt"] or at > row["lastAt"]:
                row["lastAt"] = at

    # Volume-weighted average fill price across all fills on the symbol.
    for row in by_symbol.values():
        row["avgPrice"] = (row["notional"] / row["absQty"]) if row["absQty"] else None
    return by_symbol


_GREEK_KEYS = ("delta", "gamma", "vega", "theta", "rho", "vanna", "volga")


def _zero_greeks() -> dict:
    return {k: 0.0 for k in _GREEK_KEYS}


def _sum_greeks(items, pick) -> dict:
    acc = _zero_greeks()
    for item in items:
        g = pick(item)
        for k in _GREEK_KEYS:
            acc[k] += num(g.get(k))
    return acc


def build_snapshot(positions, config: dict, extras: dict = None) -> dict:
    """Build the full dashboard snapshot.

    :param positions: raw STS /v1alpha/positions payload
    :param config:    the tracker config dict (config.json)
    :param extras:    {spotOverride, entryCredit, asOf, source, trades}
    """
    extras = extras or {}
    base = config["book"]["baseCurrency"]
    quote = config["book"]["quoteCurrency"]
    rows = positions if isinstance(positions, list) else []

    def is_base(p):
        details = p.get("instrumentDetails") or {}
        instrument = str(p.get("instrument") or "")
        return (
            p.get("currency") == base
            or details.get("baseCurrency") == base
            or instrument.startswith(f"{base}-")
            or f"-{base}-" in instrument
        )

    option_rows = [p for p in rows if p.get("type") == OPTION and is_base(p)]
    base_cash_rows = [p for p in rows if p.get("type") == CASH and p.get("currency") == base]
    quote_cash_rows = [p for p in rows if p.get("type") == CASH and p.get("currency") == quote]

    # Reference spot: options quote it most reliably; fall back to base cash.
    def _first_spot(candidates):
        for p in candidates:
            if num(p.get("spotPrice")) > 0:
                return num(p.get("spotPrice"))
        return 0.0

    spot = num(extras.get("spotOverride")) or _first_spot(option_rows) or _first_spot(base_cash_rows) or 0.0

    legs = sorted((build_leg(p, spot) for p in option_rows),
                  key=lambda leg: leg["strike"] if leg["strike"] is not None else 0)

    # ---- Section 1: options book -------------------------------------------
    options_usd = _sum_greeks(legs, lambda leg: leg["usd"])
    net_option_mark_value = sum(leg["markValue"] for leg in legs)

    # ---- Section 2/3: CC unit accounting -----------------------------------
    margin_posted_units = sum(num(p.get("quantity")) for p in base_cash_rows)
    custody_units = num(config["externalHoldings"]["custodyCcUnits"])
    # USDT sitting in the derivatives account is option premium that has not been
    # converted to CC yet — real economic delta, just not real CC yet.
    unconverted_quote = 0.0
    for p in quote_cash_rows:
        mv = num(p.get("currentMarketValue"))
        unconverted_quote += mv if mv != 0 else num(p.get("quantity")) * (num(p.get("spotPrice")) or 1)
    pending_units = (unconverted_quote / spot) if spot else 0.0
    total_cc_held = margin_posted_units + custody_units + pending_units

    short_legs = [leg for leg in legs if leg["quantity"] < 0]
    coverage_ratio_cfg = num(config["policy"].get("coverageRatio") or 1)
    cc_reserved = sum(abs(leg["quantity"]) for leg in short_legs) * coverage_ratio_cfg
    available_to_write = total_cc_held - cc_reserved

    # ---- Section 4: portfolio aggregation ($) ------------------------------
    sources = [
        {"key": "options", "label": "STS options book",
         "delta": options_usd["delta"], "gamma": options_usd["gamma"], "vega": options_usd["vega"],
         "theta": options_usd["theta"], "rho": options_usd["rho"], "units": None},
        {"key": "margin", "label": "Margin posted at STS",
         "delta": margin_posted_units * spot, "gamma": 0, "vega": 0, "theta": 0, "rho": 0,
         "units": margin_posted_units},
        {"key": "custody", "label": f"Undeployed CC — {config['externalHoldings']['venue']}",
         "delta": custody_units * spot, "gamma": 0, "vega": 0, "theta": 0, "rho": 0,
         "units": custody_units},
        {"key": "pending", "label": "CC pending conversion",
         "delta": unconverted_quote, "gamma": 0, "vega": 0, "theta": 0, "rho": 0,
         "units": pending_units},
    ]
    portfolio = {k: sum(s[k] for s in sources) for k in ("delta", "gamma", "vega", "theta", "rho")}

    # ---- Section 5: delta monitor ------------------------------------------
    net_delta_cc_units = (portfolio["delta"] / spot) if spot else 0.0
    blended_delta = (net_delta_cc_units / total_cc_held) if total_cc_held else None
    policy = config["policy"]
    hard_floor = policy["hardFloor"]
    warning_buffer = policy["warningBuffer"]
    target = policy["target"]
    upper_band = policy["upperBand"]

    status, status_text = "good", "Within normal tolerance band"
    if blended_delta is None:
        status, status_text = "unknown", "No CC holdings recorded"
    elif blended_delta < hard_floor:
        status, status_text = "critical", "Below hard floor — rebalance now"
    elif blended_delta < hard_floor + warning_buffer:
        status = "warning"
        status_text = f"Inside {warning_buffer * 100:.0f}% warning buffer above the floor"
    elif upper_band and blended_delta > upper_band:
        status, status_text = "serious", "Above upper band — coverage under-utilised, room to write"

    # Two concrete ways back to target, both in units you would actually trade.
    short_delta_raw = abs(short_legs[0]["raw"]["delta"] or 0) if short_legs else 0
    rebalance = {
        "addSpotCcUnits": (
            (target * total_cc_held - net_delta_cc_units) / (1 - target)
            if blended_delta is not None and target < 1 else None
        ),
        "buyBackShortContracts": (
            (target * total_cc_held - net_delta_cc_units) / short_delta_raw
            if blended_delta is not None and short_delta_raw > 1e-9 else None
        ),
    }

    # ---- P&L split ----------------------------------------------------------
    # realized   = premium cash already exchanged (sell premium − buy premium)
    # unrealized = current market valuation of the open legs
    # They sum to total. Realized does not move unless a fill happens.
    fills = aggregate_fills(extras.get("trades"))

    pnl_legs = []
    seen = set()
    for leg in legs:
        f = fills.get(leg["code"])
        seen.add(leg["code"])
        pnl_legs.append({
            "label": leg["label"],
            "code": leg["code"],
            "side": leg["side"],
            "strike": leg["strike"],
            "quantity": leg["quantity"],
            "open": True,
            "entryPrice": f["avgPrice"] if f else None,
            "realized": f["premium"] if f else None,
            "unrealized": leg["markValue"],
            "markPerContract": leg["markPerContract"],
            "total": (f["premium"] + leg["markValue"]) if f else None,
            "openedAt": f["firstAt"] if f else None,
        })
    # Symbols we've traded but no longer hold — a closed or rolled leg still
    # carries realized premium and must not silently vanish from the P&L.
    for code, f in fills.items():
        if code in seen:
            continue
        parsed = parse_instrument({"instrument": code})
        pnl_legs.append({
            "label": parsed["shortCode"] or short_label(parsed),
            "code": code,
            "side": "SHORT" if f["signedQty"] < 0 else ("LONG" if f["signedQty"] > 0 else "CLOSED"),
            "strike": parsed["strike"],
            "quantity": f["signedQty"],
            "open": False,
            "entryPrice": f["avgPrice"],
            "realized": f["premium"],
            "unrealized": 0,
            "markPerContract": None,
            "total": f["premium"],
            "openedAt": f["firstAt"],
        })

    have_fills = len(fills) > 0
    # Without trades, the unconverted USDT balance is the best proxy for premium
    # received — exact only while none of it has been converted to CC.
    realized_pnl = sum(num(leg["realized"]) for leg in pnl_legs) if have_fills else unconverted_quote
    unrealized_pnl = net_option_mark_value

    # ---- Underlying (spot) P&L -----------------------------------------------
    # STS only ever sees the options book. The CC itself (margin + custody +
    # pending) was acquired off-STS, so neither its cost basis nor any
    # acquisition cost is visible to the API — both are set once from the
    # Settings panel. referenceSpot is the spot price when the covered call
    # book was opened, standing in for an actual purchase price.
    underlying_cfg = config.get("underlyingPnl") or {}
    raw_reference_spot = underlying_cfg.get("referenceSpot")
    reference_spot = num(raw_reference_spot) if raw_reference_spot not in (None, "") else None
    acquisition_cost = num(underlying_cfg.get("acquisitionCost"))
    spot_change_pnl = (spot - reference_spot) * total_cc_held if reference_spot is not None else None
    underlying_pnl = {
        "referenceSpot": reference_spot,
        "units": total_cc_held,
        "spotChangePnl": spot_change_pnl,
        "acquisitionCost": acquisition_cost,
        "total": (spot_change_pnl - acquisition_cost) if spot_change_pnl is not None else None,
    }
    portfolio_total_pnl = (
        realized_pnl + unrealized_pnl + underlying_pnl["total"]
        if underlying_pnl["total"] is not None else None
    )

    pnl = {
        "realized": realized_pnl,
        "unrealized": unrealized_pnl,
        "total": realized_pnl + unrealized_pnl,
        "realizedSource": "trades" if have_fills else "usdt-balance",
        "legs": pnl_legs,
        "tradeCount": sum(f["fills"] for f in fills.values()),
        "underlying": underlying_pnl,
        "portfolioTotal": portfolio_total_pnl,
    }

    # ---- Spread economics ---------------------------------------------------
    short_leg = short_legs[0] if short_legs else None
    long_leg = next((leg for leg in legs if leg["quantity"] > 0), None)
    # Prefer the actual fills; fall back to the unconverted USDT proxy.
    entry_credit = num(extras["entryCredit"]) if extras.get("entryCredit") is not None else realized_pnl
    contracts = abs(short_leg["quantity"]) if short_leg else 0
    width = (
        abs(long_leg["strike"] - short_leg["strike"])
        if short_leg and long_leg and short_leg["strike"] is not None and long_leg["strike"] is not None
        else None
    )

    spread = {
        "shortStrike": short_leg["strike"] if short_leg else None,
        "longStrike": long_leg["strike"] if long_leg else None,
        "contracts": contracts,
        "width": width,
        "netCredit": entry_credit,
        "creditPerContract": (entry_credit / contracts) if contracts else None,
        "maxProfit": entry_credit,
        "maxLoss": (width * contracts - entry_credit) if width is not None else None,
        "breakeven": (
            short_leg["strike"] + entry_credit / contracts
            if short_leg and short_leg["strike"] is not None and contracts else None
        ),
        "netMarkValue": net_option_mark_value,
        "totalPnl": realized_pnl + unrealized_pnl,
        "distanceToShortStrike": (
            spot / short_leg["strike"] - 1 if short_leg and short_leg["strike"] else None
        ),
        "daysToExpiry": (
            short_leg["daysToExpiry"] if short_leg and short_leg["daysToExpiry"] is not None
            else (long_leg["daysToExpiry"] if long_leg else None)
        ),
        # A call spread's lower strike must mark above the higher strike. When
        # STS's per-strike marks cross, every P&L figure from them is suspect.
        "marksInverted": (
            abs(short_leg["markPerContract"]) < abs(long_leg["markPerContract"])
            if short_leg and long_leg
            and short_leg["markPerContract"] is not None and long_leg["markPerContract"] is not None
            else False
        ),
    }

    # ---- Roll schedule ------------------------------------------------------
    # Policy: roll `rollDaysBeforeExpiry` days out, so the roll date — not the
    # expiry — is the date the book is actually managed to.
    roll_days_cfg = policy.get("rollDaysBeforeExpiry")
    roll_days = num(roll_days_cfg if roll_days_cfg is not None else 14)
    approach_cfg = policy.get("rollApproachDays")
    approach_days = num(approach_cfg if approach_cfg is not None else 3)
    expiry_iso = (short_leg or {}).get("expiry") or (long_leg or {}).get("expiry") or None
    dte = spread["daysToExpiry"]
    days_to_roll = (dte - roll_days) if fin(dte) else None

    roll_status, roll_text = "unknown", "No expiry reported by STS"
    if fin(dte):
        if dte <= 0:
            roll_status, roll_text = "critical", "Expired — settle or close now"
        elif dte <= roll_days / 2:
            roll_status = "critical"
            roll_text = f"Roll overdue by {-days_to_roll:.1f}d — well inside the roll window"
        elif days_to_roll <= 0:
            roll_status = "serious"
            roll_text = f"Roll window open — due {-days_to_roll:.1f}d ago"
        elif days_to_roll <= approach_days:
            roll_status = "warning"
            roll_text = f"Roll in {days_to_roll:.1f}d — prepare the next cycle"
        else:
            roll_status = "good"
            roll_text = f"Roll in {days_to_roll:.1f}d · {roll_days:g}d before expiry"

    expiry_dt = parse_date(expiry_iso) if expiry_iso else None
    roll = {
        "rollDaysBeforeExpiry": roll_days,
        "approachDays": approach_days,
        "expiry": expiry_iso,
        "rollDate": iso(expiry_dt - timedelta(days=roll_days)) if expiry_dt else None,
        "daysToExpiry": dte,
        "daysToRoll": days_to_roll,
        # Fraction of the remaining life that sits before the roll date.
        "rollFraction": max(0, min(1, (dte - roll_days) / dte)) if fin(dte) and dte > 0 else 0,
        "status": roll_status,
        "statusText": roll_text,
        # Closing at mark realises the position's current value.
        "closeProceeds": net_option_mark_value,
        "realisedIfClosedNow": net_option_mark_value + entry_credit,
        "legsToClose": [{
            "label": leg["label"],
            "action": "BUY TO CLOSE" if leg["quantity"] < 0 else "SELL TO CLOSE",
            "quantity": abs(leg["quantity"]),
            "markPerContract": abs(leg["markPerContract"] or 0),
            "cashflow": leg["markValue"],  # signed: what closing this leg pays you
        } for leg in legs],
        "marksReliable": not spread["marksInverted"],
    }

    return {
        "asOf": extras.get("asOf") or now_iso(),
        "source": extras.get("source") or "live",
        "baseCurrency": base,
        "quoteCurrency": quote,
        "spot": spot,
        "legs": legs,
        "optionsUsd": options_usd,
        "coverage": {
            "marginPostedUnits": margin_posted_units,
            "custodyUnits": custody_units,
            "pendingUnits": pending_units,
            "unconvertedQuote": unconverted_quote,
            "totalCcHeld": total_cc_held,
            "ccReserved": cc_reserved,
            "availableToWrite": available_to_write,
            "coverageRatio": (total_cc_held / cc_reserved) if total_cc_held and cc_reserved else None,
            "covered": available_to_write >= 0,
        },
        "aggregation": {"sources": sources, "portfolio": portfolio},
        "deltaMonitor": {
            "totalDeltaUsd": portfolio["delta"],
            "netDeltaCcUnits": net_delta_cc_units,
            "totalCcHeld": total_cc_held,
            "blendedDelta": blended_delta,
            "hardFloor": hard_floor,
            "warningBuffer": warning_buffer,
            "target": target,
            "upperBand": upper_band,
            "distanceToFloor": None if blended_delta is None else blended_delta - hard_floor,
            "distanceToTarget": None if blended_delta is None else blended_delta - target,
            "status": status,
            "statusText": status_text,
            "rebalance": rebalance,
        },
        "spread": spread,
        "roll": roll,
        "pnl": pnl,
    }


def to_history_point(snap: dict) -> dict:
    """The compact row we persist per poll — keeps history files small."""
    return {
        "t": snap["asOf"],
        "spot": snap["spot"],
        "delta": snap["aggregation"]["portfolio"]["delta"],
        "optionsDelta": snap["optionsUsd"]["delta"],
        "spotDelta": snap["aggregation"]["portfolio"]["delta"] - snap["optionsUsd"]["delta"],
        "gamma": snap["aggregation"]["portfolio"]["gamma"],
        "vega": snap["aggregation"]["portfolio"]["vega"],
        "theta": snap["aggregation"]["portfolio"]["theta"],
        "rho": snap["aggregation"]["portfolio"]["rho"],
        "blended": snap["deltaMonitor"]["blendedDelta"],
        "netUnits": snap["deltaMonitor"]["netDeltaCcUnits"],
        "totalCc": snap["coverage"]["totalCcHeld"],
        "available": snap["coverage"]["availableToWrite"],
        "realizedPnl": snap["pnl"]["realized"],
        "unrealizedPnl": snap["pnl"]["unrealized"],
        "totalPnl": snap["pnl"]["total"],
        "underlyingPnl": snap["pnl"]["underlying"]["total"],
        "portfolioPnl": snap["pnl"]["portfolioTotal"],
        # `pnl` / `markValue` kept for history written before the split existed —
        # the chart reconstructs the three series from either shape.
        "pnl": snap["pnl"]["total"],
        "markValue": snap["spread"]["netMarkValue"],
        "legs": [{
            "k": leg["label"],
            "q": leg["quantity"],
            "d": leg["usd"]["delta"],
            "g": leg["usd"]["gamma"],
            "v": leg["usd"]["vega"],
            "th": leg["usd"]["theta"],
            "rd": leg["raw"]["delta"],
            "dte": leg["daysToExpiry"],
        } for leg in snap["legs"]],
    }