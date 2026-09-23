"""Fetch prices and funding data from Binance and Hyperliquid via ccxt."""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)


def _make_session() -> aiohttp.ClientSession:
    """Create aiohttp session with threaded DNS resolver (works on Windows)."""
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


async def fetch_prices(binance, hyperliquid, btc_symbol: str, eth_symbol: str) -> dict:
    """Fetch BTC and ETH prices from both exchanges.

    Returns dict with keys: btc_binance, btc_hyperliquid, btc_price_diff, eth_binance.
    """
    bn_btc_ticker, hl_btc_ticker, bn_eth_ticker = await asyncio.gather(
        binance.fetch_ticker(btc_symbol),
        hyperliquid.fetch_ticker(btc_symbol),
        binance.fetch_ticker(eth_symbol),
    )

    btc_bn = bn_btc_ticker["last"]
    btc_hl = hl_btc_ticker["last"]
    eth_bn = bn_eth_ticker["last"]
    print(f"Fetched prices - Binance BTC: {btc_bn}, Hyperliquid BTC: {btc_hl}, Binance ETH: {eth_bn}")
    return {
        "btc_binance": btc_bn,
        "btc_hyperliquid": btc_hl,
        "btc_price_diff": btc_hl - btc_bn,
        "eth_binance": eth_bn,
    }


async def _fetch_all_funding(exchange, symbol: str, since_ms: int) -> float:
    """Paginate through all funding history from since_ms and return the total.

    Loops with cursor advancement (batch[-1].timestamp + 1) until no more
    records are returned or we reach current time.
    """
    all_records = []
    now_ms = exchange.milliseconds()
    cursor = since_ms
    limit = 100 if exchange.id == "binance" else 200

    while True:
        batch = await exchange.fetch_funding_history(symbol, since=cursor, limit=limit)
        if not batch:
            break
        all_records.extend(batch)
        last_ts = batch[-1]["timestamp"] + 1
        if last_ts >= now_ms:
            break
        if last_ts <= cursor:
            break
        cursor = last_ts

    total = 0.0
    for row in all_records:
        amount = row.get("amount")
        if amount is None:
            amount = row.get("info", {}).get("amount", 0)
        total += float(amount or 0)
    return total


async def fetch_cumulative_funding(
    binance, hyperliquid, symbol: str, since: datetime,
) -> float:
    """Fetch today's net funding income from both exchanges.

    Fetches all funding records from `since` (midnight UTC today) to now,
    paginating as needed.

    Args:
        since: Start of the current UTC day (00:00 UTC).

    Returns:
        Sum of today's funding payments from both exchanges.
    """
    since_ms = int(since.timestamp() * 1000)

    bn_total, hl_total = await asyncio.gather(
        _fetch_all_funding(binance, symbol, since_ms),
        _fetch_all_funding(hyperliquid, symbol, since_ms),
    )

    total = bn_total + hl_total
    logger.info(
        "Funding today (since %s): Binance=%.4f, Hyperliquid=%.4f, Total=%.4f",
        since.isoformat(), bn_total, hl_total, total,
    )
    return total


def create_binance(api_key: str, api_secret: str) -> ccxt.binance:
    """Create authenticated Binance futures client with portfolio margin."""
    return ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "future",
            "portfolioMargin": True,
            "fetchCurrencies": False,
            "adjustForTimeDifference": True,
        },
        "session": _make_session(),
    })


def create_binance_public() -> ccxt.binance:
    """Create unauthenticated Binance client for public endpoints (prices)."""
    return ccxt.binance({
        "enableRateLimit": True,
        "options": {
            "defaultType": "future",
            "fetchCurrencies": False,
        },
        "session": _make_session(),
    })


def create_binance_spot_public() -> ccxt.binance:
    """Unauthenticated Binance SPOT client — for pricing spot-only assets
    (e.g. XLM) that have no futures market."""
    return ccxt.binance({
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
            "fetchCurrencies": False,
        },
        "session": _make_session(),
    })


def create_deribit(client_id: str, client_secret: str, base_url: str = "") -> ccxt.deribit:
    """Create authenticated Deribit client (read-only use).

    Deribit's OAuth2 credentials map to ccxt's apiKey/secret: ``client_id`` ->
    apiKey, ``client_secret`` -> secret. Pass a test.deribit.com ``base_url`` to
    target the testnet; blank uses production (www.deribit.com).
    """
    exchange = ccxt.deribit({
        "apiKey": client_id,
        "secret": client_secret,
        "enableRateLimit": True,
        "session": _make_session(),
    })
    if base_url and "test" in base_url:
        exchange.set_sandbox_mode(True)
    return exchange


def create_hyperliquid(api_key: str, private_key: str, wallet_address: str) -> ccxt.hyperliquid:
    """Create authenticated Hyperliquid client."""
    return ccxt.hyperliquid({
        "apiKey": api_key,
        "privateKey": private_key,
        "walletAddress": wallet_address,
        "enableRateLimit": True,
        "options": {"defaultSlippage": 0.002},
        "session": _make_session(),
    })


def create_hyperliquid_public() -> ccxt.hyperliquid:
    """Create unauthenticated Hyperliquid client for public endpoints (prices)."""
    return ccxt.hyperliquid({
        "enableRateLimit": True,
        "session": _make_session(),
    })
