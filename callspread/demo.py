"""Demo book — used when no STS credentials are present.

Port of the original `src/demo.js`. It emits a payload in the exact shape of
`/v1alpha/positions` for the live 21AUG26 CC call spread, priced with
Black-Scholes so the greeks move realistically as spot and time move. The
per-strike vols below were solved from the deltas STS actually reported on the
book (delta 0.220 @ 0.135, delta 0.0745 @ 0.15), so the demo reproduces the real
position's risk profile rather than inventing one.
"""

import math
from datetime import datetime, timezone

from callspread.model import iso, parse_date

EXPIRY = "2026-08-21T08:00:00+00:00"
CONTRACTS = 10_000

LEGS = [
    {"strike": 0.135, "qty": -CONTRACTS, "vol": 0.506,
     "code": "STS-CC-USDT-CC-20260821-0.135-C-E-V"},
    {"strike": 0.15, "qty": CONTRACTS, "vol": 0.523,
     "code": "STS-CC-USDT-CC-20260821-0.15-C-E-V"},
]

DERIV_ACCOUNT = "11111111-1111-1111-1111-111111111111"
SPOT_ACCOUNT = "22222222-2222-2222-2222-222222222222"

SEED_SPOT = 0.119439
STS_MARGIN_CC = 1000
NET_CREDIT_USDT = 21.73  # sold 0.135 @ 0.00615, bought 0.15 @ 0.00397

DEMO_META = {"seedSpot": SEED_SPOT, "netCredit": NET_CREDIT_USDT,
             "expiry": EXPIRY, "contracts": CONTRACTS}

# --- Black-Scholes (r = 0; these are forward-ish marks, rho is negligible) ---
_SQRT2PI = math.sqrt(2 * math.pi)
_SQRT2 = math.sqrt(2)


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _cdf(x: float) -> float:
    # Abramowitz & Stegun 7.1.26 on erf — kept rather than math.erf so the
    # demo's numbers stay identical to the Node implementation's.
    s = -1 if x < 0 else 1
    z = abs(x) / _SQRT2
    t = 1 / (1 + 0.3275911 * z)
    y = 1 - (((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t
               - 0.284496736) * t + 0.254829592) * t * math.exp(-z * z))
    return 0.5 * (1 + s * y)


def _price_call(S: float, K: float, sigma: float, T: float) -> dict:
    if T <= 0:
        return {"price": max(S - K, 0), "delta": 1 if S > K else 0, "gamma": 0,
                "vega": 0, "theta": 0, "rho": 0, "vanna": 0, "volga": 0}
    x = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * x * x) / x
    d2 = d1 - x
    nd1 = _pdf(d1)
    return {
        "price": S * _cdf(d1) - K * _cdf(d2),
        "delta": _cdf(d1),
        "gamma": nd1 / (S * x),
        "vega": (S * math.sqrt(T) * nd1) / 100,                     # per 1 vol point
        "theta": -(S * nd1 * sigma) / (2 * math.sqrt(T)) / 365,     # per day
        "rho": (K * T * _cdf(d2)) / 100,                            # per 1% rate
        "vanna": (-nd1 * d2) / sigma / 100,
        "volga": (S * math.sqrt(T) * nd1 * d1 * d2) / sigma / 100,
    }


def demo_positions(spot: float = SEED_SPOT, now: datetime = None,
                   custody_credit: float = NET_CREDIT_USDT) -> list:
    """Build an STS-shaped positions payload for a given spot / time."""
    t = now or datetime.now(timezone.utc)
    ms_to_expiry = (parse_date(EXPIRY).timestamp() - t.timestamp()) * 1000
    dte = max(0.0, ms_to_expiry / 86_400_000)
    T = dte / 365

    option_rows = []
    for leg in LEGS:
        g = _price_call(spot, leg["strike"], leg["vol"], T)
        notional = leg["qty"] * spot
        option_rows.append({
            "accountId": DERIV_ACCOUNT,
            "legalEntity": "EI813",
            "book": None,
            "accountName": "EI813 - Derivatives Trading",
            "venue": "sts",
            "instrument": leg["code"],
            "spotPrice": spot,
            "quantity": leg["qty"],
            "currentMarketValue": g["price"] * leg["qty"],
            "deltaUsd": g["delta"] * notional,
            "gammaUsd": g["gamma"] * leg["qty"] * 0.01 * spot * spot,
            "vegaUsd": g["vega"] * leg["qty"],
            "vegaRTUsd": None,
            "thetaUsd": g["theta"] * leg["qty"],
            "rhoUsd": g["rho"] * leg["qty"],
            "vannaUsd": g["vanna"] * leg["qty"],
            "volgaUsd": g["volga"] * leg["qty"],
            "daysToExpiry": dte,
            "type": "Option",
            "currency": "CC",
            "delta": g["delta"] * notional,
            "text": None,
            "isITM": spot > leg["strike"],
            "instrumentDetails": {
                "normalizedInstrumentCode": leg["code"],
                "baseCurrency": "CC",
                "quoteCurrency": "USDT",
                "optionType": "C",
                "settlementCurrency": "CC",
                "strike": leg["strike"],
                "expiry": EXPIRY,
            },
        })

    cash_rows = [
        {
            "accountId": DERIV_ACCOUNT, "legalEntity": "EI813", "book": None,
            "accountName": "EI813 - Derivatives Trading", "venue": None,
            "instrument": "USDT", "spotPrice": 0.99899, "quantity": custody_credit,
            "currentMarketValue": custody_credit * 0.99899,
            "deltaUsd": custody_credit * 0.99899,
            "gammaUsd": None, "vegaUsd": None, "vegaRTUsd": None, "thetaUsd": None,
            "rhoUsd": None, "vannaUsd": None, "volgaUsd": None,
            "daysToExpiry": None, "type": "Cash", "currency": "USDT",
            "delta": custody_credit * 0.99899,
            "text": None, "isITM": None, "instrumentDetails": None,
        },
        {
            "accountId": SPOT_ACCOUNT, "legalEntity": "EI813", "book": None,
            "accountName": "EI813 - Spot Trading", "venue": None,
            "instrument": "CC", "spotPrice": spot, "quantity": STS_MARGIN_CC,
            "currentMarketValue": STS_MARGIN_CC * spot,
            "deltaUsd": STS_MARGIN_CC * spot,
            "gammaUsd": None, "vegaUsd": None, "vegaRTUsd": None, "thetaUsd": None,
            "rhoUsd": None, "vannaUsd": None, "volgaUsd": None,
            "daysToExpiry": None, "type": "Cash", "currency": "CC",
            "delta": STS_MARGIN_CC * spot,
            "text": None, "isITM": None, "instrumentDetails": None,
        },
    ]

    return cash_rows + option_rows


def demo_trades() -> list:
    """The two opening fills, shaped like `/v1alpha/trades`.

    Prices are the real executed prices from the book, so the demo's realized
    P&L matches production.
    """
    return [
        {
            "execID": "demo-sell-0135", "orderID": "demo-ord-0135", "account": DERIV_ACCOUNT,
            "side": "2", "sideText": "Sell", "ordType": "2", "ordTypeText": "Limit",
            "symbol": LEGS[0]["code"], "orderQty": CONTRACTS, "price": 0.006145,
            "lastQty": CONTRACTS, "lastPx": 0.006145,
            "transactTime": "2026-07-22T00:38:13.977", "premiumCurrency": "USDT",
        },
        {
            "execID": "demo-buy-015", "orderID": "demo-ord-015", "account": DERIV_ACCOUNT,
            "side": "1", "sideText": "Buy", "ordType": "2", "ordTypeText": "Limit",
            "symbol": LEGS[1]["code"], "orderQty": CONTRACTS, "price": 0.003972,
            "lastQty": CONTRACTS, "lastPx": 0.003972,
            "transactTime": "2026-07-22T00:38:43.701", "premiumCurrency": "USDT",
        },
    ]


_U32 = 0xFFFFFFFF


def mulberry32(seed: int):
    """Deterministic PRNG so a restart doesn't redraw a different past.

    A bit-exact port: JS bitwise ops coerce to 32 bits and `>>>` reads the
    unsigned representation, so staying in unsigned 32-bit arithmetic here
    reproduces `Math.imul` and friends value-for-value.
    """
    a = seed & _U32

    def rnd() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & _U32
        t = ((a ^ (a >> 15)) * (1 | a)) & _U32
        t = (((t + (((t ^ (t >> 7)) * (61 | t)) & _U32)) & _U32) ^ t) & _U32
        return ((t ^ (t >> 14)) & _U32) / 4294967296

    return rnd


def demo_spot_path(end_spot: float = SEED_SPOT, end_time_ms: float = None,
                   points: int = 288, step_ms: int = 15 * 60_000,
                   vol: float = 0.52, seed: int = 20260821) -> list:
    """Walk spot BACKWARDS from `end_spot` so history joins the live value smoothly.

    :returns: oldest-first [{t, spot}]
    """
    if end_time_ms is None:
        end_time_ms = datetime.now(timezone.utc).timestamp() * 1000
    rnd = mulberry32(seed)
    dt = step_ms / (365 * 86_400_000)
    sigma = vol * math.sqrt(dt)

    def at(ms: float) -> str:
        return iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))

    path = [{"t": at(end_time_ms), "spot": end_spot}]
    s = end_spot
    for i in range(1, points):
        # Box-Muller, walked in reverse.
        u1 = max(rnd(), 1e-9)
        u2 = rnd()
        z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
        s = s * math.exp(-(sigma * z - 0.5 * sigma * sigma))
        path.append({"t": at(end_time_ms - i * step_ms), "spot": s})
    path.reverse()
    return path