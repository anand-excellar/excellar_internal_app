"""BitGo custody balance fetcher.

Replaces DeBank scraping for *non-exchange* positions: lists every wallet the
access token can see (across all coins) via the BitGo platform REST API and
aggregates their balances into per-asset holdings.

BitGo only knows custodial balances — it does NOT expose DeFi protocol state
(Aave borrow / health factor, sUSDS yield, Hyperliquid deposits). Those fields
are sourced separately; this module deliberately leaves them untouched.

API reference:
    GET {base_url}/api/v2/wallets?expandBalance=true
    Authorization: Bearer <access_token>
Balances are returned as base-unit strings (satoshis / wei); we convert with a
per-coin decimal table. The wallet object carries no fiat value, so USD is
computed here from a caller-supplied price map.
"""

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# A 0x-prefixed 20-byte EVM address (Ethereum, Arbitrum, etc.).
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Transient HTTP statuses worth retrying (rate limit + gateway/server errors).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BACKOFF_BASE_SECONDS = 1.5

# Base-unit decimals per asset symbol. Extend as new coins are custodied.
COIN_DECIMALS: dict[str, int] = {
    "BTC": 8, "WBTC": 8, "CBBTC": 8, "TBTC": 8, "LTC": 8, "BCH": 8,
    "ETH": 18, "WETH": 18, "STETH": 18, "WSTETH": 18, "DAI": 18,
    "MATIC": 18, "POL": 18, "AVAX": 18, "BNB": 18, "ARB": 18, "OP": 18,
    "USDC": 6, "USDT": 6, "USDC.E": 6,
    "SOL": 9,
    "XLM": 7, "XRP": 6, "TRX": 6, "ADA": 6,
}
DEFAULT_DECIMALS = 18

# For tokens not matched exactly (e.g. Aave aTokens like ETH:AETHWBTC), decimals
# are inferred from the trailing underlying symbol. Ordered most-specific first
# so WBTC beats BTC and WETH beats ETH. aTokens inherit the underlying decimals.
_DECIMAL_SUFFIXES = [
    "WSTETH", "WETH", "STETH", "CBBTC", "TBTC", "WBTC",
    "USDC.E", "USDC", "USDT", "DAI", "BTC", "ETH", "SOL", "LTC", "BCH",
]

# Coins that should be valued at $1 when no explicit price is supplied.
# NOTE: sUSDS is deliberately excluded — it is a Spark Savings receipt worth
# >1 USDS, valued by the on-chain Spark reader (tracker/scrapers/spark.py). If
# BitGo ever surfaced it, pricing it at $1 here would both mis-value and
# double-count it against that reader.
STABLECOINS = {"USDC", "USDT", "DAI", "USDC.E", "USDS"}

# Normalise BitGo coin tickers (incl. testnet prefixes) to a display symbol.
# Keyed by the token id AND by its post-chain-prefix base (see normalize_symbol),
# so e.g. ARBETH:USDCV2 -> USDCV2 -> USDC.
_COIN_ALIASES: dict[str, str] = {
    "TBTC": "BTC", "TBTC4": "BTC", "TBTCSIG": "BTC",
    "HTETH": "ETH", "TETH": "ETH", "GTETH": "ETH",
    "POLYGON": "MATIC", "TPOLYGON": "MATIC", "MATIC": "MATIC",
    "TSOL": "SOL",
    "AVAXC": "AVAX", "TAVAXC": "AVAX",
    # USDC variants across chains (Arbitrum native USDCV2, Base USDbC, bridged .e)
    "USDCV2": "USDC", "USDBC": "USDC", "USDCE": "USDC",
}


def normalize_symbol(coin: Optional[str]) -> str:
    """Map a BitGo coin/token ticker to an uppercase display symbol.

    Also strips the chain prefix (e.g. ``ARBETH:USDCV2`` -> ``USDCV2``) and
    re-checks the alias table, so chain-scoped stablecoin variants collapse to
    their canonical symbol. Unknown tokens (incl. Aave aTokens like
    ``ETH:AETHWBTC``) are returned unchanged so they keep their identity.
    """
    if not coin:
        return ""
    sym = str(coin).upper()
    if sym in _COIN_ALIASES:
        return _COIN_ALIASES[sym]
    base = sym.split(":")[-1]
    if base in _COIN_ALIASES:
        return _COIN_ALIASES[base]
    if base in STABLECOINS:      # chain-prefixed stable (e.g. ETH:USDC) -> USDC
        return base
    return sym


def is_atoken(symbol: Optional[str]) -> bool:
    """True for Aave aToken symbols (e.g. ETH:AETHWBTC, AETHWETH, AARBWBTC).

    These are receipts for collateral supplied to Aave; they are valued by the
    on-chain Aave reader, so callers skip pricing them here to avoid
    double-counting.
    """
    base = (symbol or "").upper().split(":")[-1]
    return base.startswith("AETH") or base.startswith("AARB")


def _decimals(symbol: str) -> int:
    if symbol in COIN_DECIMALS:
        return COIN_DECIMALS[symbol]
    # Strip a chain prefix (e.g. "ETH:AETHWBTC" -> "AETHWBTC").
    base = symbol.split(":")[-1]
    if base in COIN_DECIMALS:
        return COIN_DECIMALS[base]
    # Aave aTokens etc.: infer from the trailing underlying symbol.
    for suffix in _DECIMAL_SUFFIXES:
        if base.endswith(suffix):
            return COIN_DECIMALS[suffix]
    return DEFAULT_DECIMALS


def _to_amount(balance_string: Any, symbol: str) -> Optional[float]:
    """Convert a base-unit balance string to a human-readable amount.

    Uses ``Decimal`` for the division so large 18-decimal (wei) balances do not
    lose precision the way binary floats would. The result is returned as a
    float for storage/JSON, but the scaling itself is exact.
    """
    if balance_string in (None, ""):
        return None
    try:
        scaled = Decimal(str(balance_string)) / (Decimal(10) ** _decimals(symbol))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("BitGo: un-parseable balance %r for %s", balance_string, symbol)
        return None
    return float(scaled)


def _error_detail(response) -> str:
    """BitGo's own explanation for a failed call.

    requests' stock message is just "401 Client Error: Unauthorized for url:
    ...", which is the same text whether the token expired, lacks enterprise
    access, or is being used from an IP that is not on its allowlist. BitGo puts
    the actual reason in the body, so surfacing it turns a day of guessing into
    a one-line diagnosis. Only the "error" field is read — never the whole body,
    which can echo request details.
    """
    if response is None:
        return "no response"
    try:
        payload = response.json()
    except ValueError:
        return "unparseable error body"
    detail = payload.get("error") or payload.get("message")
    return str(detail) if detail else "no error detail in body"


def _get_with_retry(
    session: requests.Session, url: str, params: dict, timeout: int,
) -> dict:
    """GET with bounded exponential backoff on rate-limit / 5xx responses.

    Never logs the Authorization header or token. Raises the last error if all
    retries are exhausted.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code in _RETRYABLE_STATUS:
                raise requests.HTTPError(f"retryable status {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # Don't retry genuine client errors (401/403/404 etc.)
            if status is not None and status not in _RETRYABLE_STATUS:
                raise requests.HTTPError(
                    f"HTTP {status} from BitGo: {_error_detail(exc.response)}",
                    response=exc.response,
                ) from exc
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "BitGo request failed (attempt %d/%d, status=%s); retrying in %.1fs",
                    attempt, _MAX_RETRIES, status, delay,
                )
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def fetch_all_wallets(
    access_token: str,
    base_url: str = "https://app.bitgo.com",
    enterprise_id: str = "",
    page_limit: int = 100,
    timeout: int = 30,
) -> list[dict]:
    """Return every wallet the token can access, with balances expanded.

    Paginates through the cross-coin ``/api/v2/wallets`` endpoint using the
    ``nextBatchPrevId`` cursor, retrying transient failures. Raises on a
    terminal error (auth failure, exhausted retries) so the caller can decide
    how to degrade rather than silently storing partial/empty balances.
    """
    if not access_token:
        raise ValueError("BitGo access token is not configured")

    url = f"{base_url.rstrip('/')}/api/v2/wallets"
    params: dict[str, Any] = {"expandBalance": "true", "limit": page_limit}
    if enterprise_id:
        params["enterprise"] = enterprise_id

    wallets: list[dict] = []
    prev_id = None
    with requests.Session() as session:
        session.headers.update({"Authorization": f"Bearer {access_token}"})
        # Guard against a malformed/looping cursor returning the same page.
        seen_cursors: set[str] = set()
        while True:
            page_params = dict(params)
            if prev_id:
                page_params["prevId"] = prev_id
            data = _get_with_retry(session, url, page_params, timeout)
            wallets.extend(data.get("wallets", []))
            prev_id = data.get("nextBatchPrevId")
            if not prev_id or prev_id in seen_cursors:
                break
            seen_cursors.add(prev_id)
    logger.info("BitGo: fetched %d wallets", len(wallets))
    return wallets


def _price_for(symbol: str, price_by_symbol: dict[str, float]) -> Optional[float]:
    if symbol in price_by_symbol:
        return price_by_symbol[symbol]
    if symbol in STABLECOINS:
        return 1.0
    return None


def _add_holding(acc: dict[str, dict], symbol: str, amount: float, price_by_symbol):
    if not symbol or not amount:
        return
    price = _price_for(symbol, price_by_symbol)
    entry = acc.setdefault(symbol, {"symbol": symbol, "amount": 0.0, "price": price, "usd": 0.0})
    entry["amount"] += amount
    entry["price"] = price
    entry["usd"] = (entry["amount"] * price) if price is not None else None


def _asset_entry(symbol: str, amount: float, price_by_symbol) -> dict:
    """A single valued asset row: {symbol, amount, price, usd}."""
    price = _price_for(symbol, price_by_symbol)
    return {
        "symbol": symbol,
        "amount": amount,
        "price": price,
        "usd": (amount * price) if price is not None else None,
    }


def _wallet_breakdown(wallet: dict, price_by_symbol) -> Optional[dict]:
    """Per-wallet view: label/coin/id plus each non-zero asset it holds.

    Returns None for a wallet with no non-zero balances so callers can skip
    empty wallets in a per-wallet display.
    """
    assets: list[dict] = []
    symbol = normalize_symbol(wallet.get("coin"))
    amount = _to_amount(wallet.get("balanceString"), symbol)
    if amount:
        assets.append(_asset_entry(symbol, amount, price_by_symbol))
    for token_name, token in (wallet.get("tokens") or {}).items():
        tsym = normalize_symbol(token.get("coin") or token_name)
        tamount = _to_amount(token.get("balanceString"), tsym)
        if tamount:
            assets.append(_asset_entry(tsym, tamount, price_by_symbol))
    if not assets:
        return None
    return {
        "label": wallet.get("label") or "",
        "coin": wallet.get("coin") or "",
        "id": wallet.get("id") or "",
        "assets": assets,
    }


def wallet_evm_address(wallet: dict) -> Optional[str]:
    """Best-effort extraction of a wallet's public EVM (0x) address.

    For account-based coins (eth/arbeth/...) BitGo exposes the wallet's address
    as both ``coinSpecific.baseAddress`` and ``receiveAddress.address`` (they are
    identical). Non-EVM wallets (btc/xlm/ofc) have no 0x address and return None.
    """
    coin_specific = wallet.get("coinSpecific")
    receive = wallet.get("receiveAddress")
    candidates = [
        coin_specific.get("baseAddress") if isinstance(coin_specific, dict) else None,
        receive.get("address") if isinstance(receive, dict) else None,
        wallet.get("address"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and _EVM_ADDRESS_RE.match(candidate.strip()):
            return candidate.strip().lower()
    return None


def evm_addresses(wallets: list[dict]) -> list[str]:
    """Distinct lower-cased EVM addresses across a list of wallets (order kept)."""
    seen: set[str] = set()
    out: list[str] = []
    for wallet in wallets:
        addr = wallet_evm_address(wallet)
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def aggregate_holdings(
    wallets: list[dict],
    price_by_symbol: Optional[dict[str, float]] = None,
) -> dict:
    """Aggregate per-coin balances across every wallet into a combined view.

    Args:
        wallets: Raw wallet dicts from :func:`fetch_all_wallets`.
        price_by_symbol: Map of SYMBOL -> USD price (e.g. {"BTC": 73000, "ETH": 2015}).
            Stablecoins default to 1.0 when absent.

    Returns:
        {
          "by_symbol": {SYMBOL: {symbol, amount, price, usd}},
          "holdings":  [ ... sorted by usd desc ... ],
          "total_usd": float,
          "wallets":   [ {label, coin, id, assets:[{symbol, amount, price, usd}]} ],
        }

    ``wallets`` preserves the per-wallet breakdown (empty wallets omitted) so a
    caller can render balances wallet-by-wallet; the aggregate fields above are
    unchanged.
    """
    price_by_symbol = price_by_symbol or {}
    acc: dict[str, dict] = {}
    wallets_out: list[dict] = []

    for wallet in wallets:
        breakdown = _wallet_breakdown(wallet, price_by_symbol)
        if breakdown is None:
            continue
        wallets_out.append(breakdown)
        for asset in breakdown["assets"]:
            _add_holding(acc, asset["symbol"], asset["amount"], price_by_symbol)

    holdings = sorted(
        acc.values(),
        key=lambda h: (h["usd"] if h["usd"] is not None else -1),
        reverse=True,
    )
    total_usd = sum(h["usd"] for h in holdings if h["usd"] is not None)
    return {
        "by_symbol": acc,
        "holdings": holdings,
        "total_usd": total_usd,
        "wallets": wallets_out,
    }
