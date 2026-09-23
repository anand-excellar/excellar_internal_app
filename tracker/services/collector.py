"""Collector service: adapted from nav-tracker, uses Django ORM instead of raw SQL."""
import asyncio
import json
import logging
from datetime import datetime, timezone, date, timedelta
from typing import Optional
import os
import ccxt
import requests

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Max

from tracker.models import Snapshot, DailyPnl
from tracker.scrapers import bitgo, aave, spark, vaults
from tracker.scrapers.exchanges import (
    fetch_prices, fetch_cumulative_funding,
    create_binance, create_binance_public, create_binance_spot_public,
    create_hyperliquid, create_hyperliquid_public, create_deribit,
)
from tracker.calculator.pnl import compute_pnl, compute_cross_mtm
from tracker.config import load_tokens

logger = logging.getLogger(__name__)


async def fetch_binance_positions(prefix: str, env_keys: dict) -> list[dict]:
    api_key_name = f"{prefix}_BINANCE_API_KEY"
    api_secret_name = f"{prefix}_BINANCE_API_SECRET"
    api_key = env_keys.get(api_key_name, "")
    api_secret = env_keys.get(api_secret_name, "")

    logger.info(
        "Fetching Binance positions for %s account: api_key_set=%s api_secret_set=%s",
        prefix, bool(api_key), bool(api_secret),
    )

    client = create_binance(api_key=api_key, api_secret=api_secret)
    try:
        positions = await client.fetch_positions()
        print(f"Raw Binance positions for {prefix} account: {positions}")
    finally:
        await _close_exchange_client(client)

    active = []
    for p in positions:
        if float(p.get("contracts", 0)) != 0:
            print(f"Active position found for {prefix} account: {p.get('symbol')}")
            active.append({
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "contracts": p.get("contracts"),
                "entryPrice": p.get("entryPrice"),
                "markPrice": p.get("markPrice"),
                "unrealizedPnl": p.get("unrealizedPnl"),
                "liquidationPrice": p.get("liquidationPrice"),
                "initialMargin": p.get("initialMargin"),
                "leverage": p.get("leverage"),
            })

    return active


async def fetch_hl_positions(prefix: str, env_keys: dict) -> list[dict]:
    wallet_name = f"{prefix}_HL_WALLET_ADDRESS"
    wallet = env_keys.get(wallet_name, "")
    logger.info("Fetching Hyperliquid positions for %s account; wallet_set=%s", prefix, bool(wallet))

    url = "https://api.hyperliquid.xyz/info"
    payload = {
        "type": "clearinghouseState",
        "user": wallet,
    }

    # requests is blocking: called bare inside this coroutine it stops the whole
    # event loop, so every other venue's in-flight fetch burns its own timeout
    # waiting on this one. Offload it, and bound it — an unbounded call here
    # hangs the entire snapshot cycle.
    def _call() -> dict:
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()
        return res.json()

    data = await asyncio.to_thread(_call)

    positions = data.get("assetPositions", [])
    print(f"Raw HL positions for {prefix} account: {positions}")
    rows = []
    for p in positions:
        pos = p.get("position", {})
        size = float(pos.get("szi", 0))
        if size == 0:
            continue
        # HL gives positionValue (notional at mark); derive the mark price from
        # it so our notional matches Hyperliquid's rather than using entry.
        position_value = _to_float(pos.get("positionValue"))
        mark_price = (position_value / abs(size)) if position_value else None
        leverage = pos.get("leverage") or {}
        rows.append({
            "symbol": pos.get("coin"),
            "size": size,
            "entryPrice": float(pos.get("entryPx", 0)),
            "markPrice": mark_price,
            "positionValue": position_value,
            "unrealizedPnl": float(pos.get("unrealizedPnl", 0)),
            "liquidationPrice": _to_float(pos.get("liquidationPx")),
            "margin": _to_float(pos.get("marginUsed")),
            "leverage": _to_float(leverage.get("value")) if isinstance(leverage, dict) else None,
        })

    return rows


def _build_perp_legs(bn_positions: list, hl_positions: list) -> list[dict]:
    """Normalise fetched Binance + Hyperliquid perp positions into catalog legs.

    Returns a list of dicts with a uniform shape so the dashboard can render
    each exchange's leg as a row:
        {venue, symbol, side, size, entry_price, mark_price, unrealized_pnl,
         liquidation_price, margin, leverage}
    Notional is left to the view so it can use the mark price.
    """
    legs: list[dict] = []

    for p in bn_positions or []:
        size = abs(float(p.get("contracts") or 0))
        if size == 0:
            continue
        legs.append({
            "venue": "Binance",
            "symbol": p.get("symbol"),
            "side": (p.get("side") or "").lower(),
            "size": size,
            "entry_price": _to_float(p.get("entryPrice")),
            "mark_price": _to_float(p.get("markPrice")),
            "unrealized_pnl": _to_float(p.get("unrealizedPnl")),
            "liquidation_price": _to_float(p.get("liquidationPrice")),
            "margin": _to_float(p.get("initialMargin")),
            "leverage": _to_float(p.get("leverage")),
        })

    for p in hl_positions or []:
        size = float(p.get("size") or 0)
        if size == 0:
            continue
        legs.append({
            "venue": "Hyperliquid",
            "symbol": p.get("symbol"),
            "side": "long" if size > 0 else "short",
            "size": abs(size),
            "entry_price": _to_float(p.get("entryPrice")),
            "mark_price": _to_float(p.get("markPrice")),  # derived from positionValue
            "unrealized_pnl": _to_float(p.get("unrealizedPnl")),
            "liquidation_price": _to_float(p.get("liquidationPrice")),
            "margin": _to_float(p.get("margin")),
            "leverage": _to_float(p.get("leverage")),
        })

    return legs


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Coins treated as $1 when valuing exchange balances.
_STABLES = {"USDC", "USDT", "DAI", "USD", "USDC.E", "BUSD", "FDUSD", "TUSD"}


def _usd_value(coin, amount, price_map: dict) -> Optional[float]:
    """USD value of ``amount`` of ``coin``: $1 for stables, else via price_map.

    Returns None when the coin has no known price (so callers can skip it rather
    than under-count it as zero).
    """
    amount = _to_float(amount)
    if amount is None:
        return None
    sym = (coin or "").upper()
    if sym in _STABLES:
        return amount
    price = price_map.get(sym) if price_map else None
    return amount * price if price is not None else None


async def fetch_hl_account_value(
    prefix: str, env_keys: dict, price_map: Optional[dict] = None,
) -> Optional[dict]:
    """Total Hyperliquid funds for an account across PERP + SPOT (unified view).

    ``clearinghouseState`` only reports the perp margin account, so on a unified
    account the spot balance is missing. We add ``spotClearinghouseState`` so the
    totals match Hyperliquid's Unified Account (perp equity + spot holdings).

    Returns ``{"account_value": <perp equity + spot value>,
               "available": <perp withdrawable + free spot>}`` or None.
    """
    price_map = price_map or {}
    wallet = env_keys.get(f"{prefix}_HL_WALLET_ADDRESS", "")
    if not wallet:
        return None

    async def _post(kind: str) -> dict:
        def _call() -> dict:
            res = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": kind, "user": wallet},
                timeout=30,
            )
            res.raise_for_status()
            return res.json()

        return await asyncio.to_thread(_call)

    try:
        perp = await _post("clearinghouseState")
        perp_equity = _to_float(perp.get("marginSummary", {}).get("accountValue")) or 0.0
        perp_avail = _to_float(perp.get("withdrawable")) or 0.0
        margin_used = _to_float(perp.get("marginSummary", {}).get("totalMarginUsed")) or 0.0

        # Spot side holds the unified account's cash (USDC + any priced token).
        # Capture each non-zero asset for the per-venue breakdown.
        spot_value = 0.0
        assets: list[dict] = []
        for bal in (await _post("spotClearinghouseState")).get("balances", []):
            coin = bal.get("coin")
            total = _to_float(bal.get("total"))
            if not total:
                continue
            tv = _usd_value(coin, total, price_map)
            price = (tv / total) if (tv is not None and total) else None
            assets.append({"coin": coin, "amount": total, "price": price, "usd": tv})
            if tv:
                spot_value += tv

        # Unified account: perp margin is drawn from the SAME balance shown in
        # spot, so the spot balance already IS the account value — do NOT add
        # perp equity on top (that double-counts). Available = balance − margin.
        # Fall back to the perp account for a classic (spot-less) perp wallet.
        if spot_value > 0:
            account_value = spot_value
            available = spot_value - margin_used
        else:
            account_value = perp_equity
            available = perp_avail

        return {"account_value": account_value, "available": available, "assets": assets}
    except Exception as exc:
        logger.error("HL account value fetch failed for %s: %s", prefix, exc)
        return None


async def fetch_binance_collateral(
    prefix: str, env_keys: dict, price_map: Optional[dict] = None,
) -> Optional[dict]:
    """Binance portfolio-margin funds for an account.

    ccxt's ``total``/``free`` only reflect the UM futures sub-wallet
    (``umWalletBalance + umUnrealizedPNL``), which comes out ~negative. The real
    collateral is the CROSS-margin balance, so we read the raw papi balance list:

    - ``available`` = free USDC collateral (``crossMarginFree`` of stablecoins) —
      the USDC available to trade after margin.
    - ``total`` = account equity (``actualEquity`` from papiGetAccount) — full
      collateral value incl. other assets + unrealized PnL.
    """
    price_map = price_map or {}
    api_key = env_keys.get(f"{prefix}_BINANCE_API_KEY", "")
    api_secret = env_keys.get(f"{prefix}_BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        return None
    client = create_binance(api_key=api_key, api_secret=api_secret)
    try:
        balance = await client.fetch_balance()

        # Per-asset cross-margin balances from the papi list. crossMarginAsset =
        # total collateral of that asset; crossMarginFree = the free portion.
        available = 0.0
        assets: list[dict] = []
        info = balance.get("info")
        if isinstance(info, list):
            for asset in info:
                sym = (asset.get("asset") or "").upper()
                amount = _to_float(asset.get("crossMarginAsset"))
                free = _to_float(asset.get("crossMarginFree"))
                if not amount:
                    continue
                usd = _usd_value(sym, amount, price_map)
                price = (usd / amount) if (usd is not None and amount) else None
                assets.append({"coin": sym, "amount": amount, "price": price,
                               "usd": usd, "free": free})
                if sym in _STABLES and free:
                    available += free

        # Account-level equity for the total (real NAV contribution).
        total = sum(a["usd"] for a in assets if a["usd"] is not None)
        try:
            acct = await client.papiGetAccount()
            equity = _to_float(acct.get("actualEquity"))
            if equity is not None:
                total = equity
        except Exception as acct_exc:
            logger.warning("Binance papiGetAccount failed for %s: %s", prefix, acct_exc)

        return {"total": total, "available": available, "assets": assets}
    except Exception as exc:
        logger.error("Binance collateral fetch failed for %s: %s", prefix, exc)
        return None
    finally:
        await _close_exchange_client(client)


async def fetch_deribit_equity(
    prefix: str, env_keys: dict, price_map: Optional[dict] = None,
) -> Optional[dict]:
    """Deribit account equity for a segment, summed across its per-currency wallets.

    Deribit keeps a separate account ("wallet") per currency (BTC, ETH, USDC, …);
    ``get_account_summaries`` returns one summary each. Per currency we read:

    - ``equity`` = account value in that currency, INCLUDING unrealized PnL on
      any open options/futures (mark-to-market) — the figure that enters NAV.
    - ``available_funds`` = free collateral (equity minus margin locked).

    Each is denominated in its own currency, so we value it in USD via
    ``price_map`` (stables = $1). Returns
    ``{"total": <equity USD>, "available": <free USD>, "assets": [...]}`` or None
    when the segment has no Deribit credentials configured.
    """
    price_map = price_map or {}
    client_id = env_keys.get(f"{prefix}_DERIBIT_CLIENT_ID", "")
    client_secret = env_keys.get(f"{prefix}_DERIBIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    base_url = getattr(settings, "NAV_DERIBIT", {}).get("base_url", "")
    client = create_deribit(client_id, client_secret, base_url)
    try:
        balance = await client.fetch_balance()
        summaries = (balance.get("info") or {}).get("summaries") or []
        total = 0.0
        available = 0.0
        assets: list[dict] = []
        for summary in summaries:
            coin = (summary.get("currency") or "").upper()
            equity = _to_float(summary.get("equity"))
            free = _to_float(summary.get("available_funds"))
            if not equity:
                continue
            usd = _usd_value(coin, equity, price_map)
            free_usd = _usd_value(coin, free, price_map)
            price = (usd / equity) if (usd is not None and equity) else None
            assets.append({"coin": coin, "amount": equity, "price": price,
                           "usd": usd, "free": free})
            if usd is not None:
                total += usd
            if free_usd is not None:
                available += free_usd
        return {"total": total, "available": available, "assets": assets}
    except Exception as exc:
        logger.error("Deribit equity fetch failed for %s: %s", prefix, exc)
        return None
    finally:
        await _close_exchange_client(client)


async def _fetch_asset_prices(coins: set) -> dict:
    """Live USD prices for the given coins via Binance public tickers.

    Used to value exchange/custody assets outside the base BTC/ETH set. Tries
    COIN/USDC then COIN/USDT; unpriceable coins are omitted. Stablecoins are $1.
    """
    prices: dict[str, float] = {}
    coins = {c.upper() for c in coins if c}
    if not coins:
        return prices
    client = create_binance_spot_public()
    try:
        for coin in coins:
            if coin in _STABLES:
                prices[coin] = 1.0
                continue
            # No exchange lists a chain-scoped or wrapped variant ("ARBETH"), so
            # ask for the asset it tracks and apply that price to the variant.
            # Without this the holding is left unpriced, and an unpriced custody
            # asset is dropped from NAV entirely rather than flagged.
            ticker_coin = _PRICE_BASE_ALIASES.get(coin, coin)
            for quote in ("USDT", "USDC"):
                try:
                    ticker = await client.fetch_ticker(f"{ticker_coin}/{quote}")
                except Exception:
                    continue
                last = ticker.get("last") if ticker else None
                if last:
                    prices[coin] = float(last)
                    break
    finally:
        await _close_exchange_client(client)
    return prices


def _value_assets(assets: list, price_map: dict) -> None:
    """Fill in price/usd for any asset that is still unpriced, in place."""
    for a in assets or []:
        if a.get("usd") is not None or not a.get("amount"):
            continue
        price = _usd_price(a.get("coin"), price_map)
        if price is not None:
            a["price"] = price
            a["usd"] = a["amount"] * price


def _usd_price(coin, price_map: dict) -> Optional[float]:
    """Per-unit USD price for a coin: $1 for stables, else from price_map."""
    sym = (coin or "").upper()
    if sym in _STABLES:
        return 1.0
    return price_map.get(sym) if price_map else None


def compute_cross_mtm_params(base_asset: str, bn_positions: list, hl_positions: list) -> dict:
    """Compute cross_mtm_params from fetched positions."""

    # Filter positions for the base_asset
    # bn_filtered = [p for p in bn_positions if p['symbol'].startswith(base_asset)]
    # hl_filtered = [p for p in hl_positions if p['symbol'] == base_asset]
    #As position can be made on any asset on either exchange, hence no need to filter

    print(f"Positions for {base_asset} - Binance: {bn_positions}, Hyperliquid: {hl_positions}")

    bn_size = float(bn_positions[0].get('contracts', 0))
    bn_entryPrice = float(bn_positions[0].get('entryPrice', 0))

    hl_size = float(hl_positions[0].get('size', 0))
    hl_entryPrice = float(hl_positions[0].get('entryPrice', 0))

    # Net position_size and entry_cost
    position_size = bn_size if bn_size != 0 else abs(hl_size)
    entry_cost = (bn_entryPrice-hl_entryPrice)*hl_size
    # entry_cost = (hl_entryPrice-bn_entryPrice)*position_size if hl_size < 0 else (bn_entryPrice-hl_entryPrice)*position_size

    return {"entry_cost": entry_cost, "position_size": position_size, "direction": (hl_size < 0) - (hl_size > 0)}


def _get_reference_snapshot(segment: str, current_date: str):
    """Last snapshot with timestamp before current_date (i.e. from previous days)."""
    cutoff = datetime.fromisoformat(current_date).replace(tzinfo=timezone.utc)
    return (
        Snapshot.objects
        .filter(segment=segment, timestamp__lt=cutoff)
        .order_by("-timestamp")
        .first()
    )


def _get_daily_pnl(segment: str, date_str: str):
    return DailyPnl.objects.filter(segment=segment, date=date_str).first()


def _get_rolling_max(segment: str, before_date: str = None) -> Optional[float]:
    from django.db.models import Max
    qs = DailyPnl.objects.filter(segment=segment)
    if before_date:
        qs = qs.exclude(date=before_date)
    result = qs.aggregate(Max("portfolio_value"))
    return result["portfolio_value__max"]


async def _close_exchange_client(client) -> None:
    """Close ccxt exchange client and any custom aiohttp session it owns."""
    if client is None:
        return
    session = getattr(client, "session", None)
    if session is not None:
        try:
            await session.close()
        except Exception:
            pass
    try:
        await client.close()
    except Exception:
        pass


def _snapshot_to_dict(snap: Snapshot) -> dict:
    """Convert a Snapshot model instance to a plain dict for compute_pnl."""
    return {f.name: getattr(snap, f.name) for f in snap._meta.fields}


def _build_snapshot_data(
    segment, wallet_config, debank_data, prices, funding, cross_mtm_params,
    timestamp_str, current_date, perp_legs=None, bitgo_holdings_json=None,
) -> dict:
    btc_price = prices.get("btc_binance")
    eth_price = prices.get("eth_binance")
    price_diff = prices.get("btc_price_diff")

    current_data = {
        "aave_borrow_usdc": debank_data.get("aave_borrow_usdc"),
        "susds_eth_balance": debank_data.get("susds_eth_balance"),
        "susds_arbi_balance": debank_data.get("susds_arbi_balance"),
        "btc_price": btc_price,
        "eth_price": eth_price,
        "price_diff_hl_bn": (price_diff)*cross_mtm_params.get("direction", 1) if price_diff is not None else None,
    }

    reference = _get_reference_snapshot(segment, current_date)
    prev_day_row = _get_daily_pnl(segment, _prev_date(current_date))

    if prev_day_row is not None:
        prev_day_portfolio_value = prev_day_row.portfolio_value
    elif reference is not None:
        prev_day_portfolio_value = reference.portfolio_value
    else:
        prev_day_portfolio_value = None

    base_asset = wallet_config.get("base_asset", "BTC").upper()
    pnl_result = compute_pnl(
        segment=segment,
        base_asset=base_asset,
        current=current_data,
        reference=_snapshot_to_dict(reference) if reference is not None else None,
        funding_today=funding,
        cross_mtm_params=cross_mtm_params,
        prev_day_portfolio_value=prev_day_portfolio_value,
    )

    timestamp_dt = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )

    return {
        "timestamp": timestamp_dt,
        "segment": segment,
        "wallet_address": wallet_config.get("address", ""),
        "btc_price": btc_price,
        "eth_price": eth_price,
        "aave_supply_amount": debank_data.get("aave_supply_amount"),
        "aave_supply_usd": debank_data.get("aave_supply_usd"),
        "aave_borrow_usdc": debank_data.get("aave_borrow_usdc"),
        "aave_health_rate": debank_data.get("aave_health_rate"),
        "aave_net_usd": debank_data.get("aave_net_usd"),
        "aave_profit": debank_data.get("aave_profit"),
        "susds_eth_balance": debank_data.get("susds_eth_balance"),
        "susds_arbi_balance": debank_data.get("susds_arbi_balance"),
        "vault_usd": debank_data.get("vault_usd"),
        "vault_positions_json": debank_data.get("vault_positions_json"),
        "hl_deposit_usdc": debank_data.get("hl_deposit_usdc"),
        "hl_available_usdc": debank_data.get("hl_available_usdc"),
        "binance_collateral_usdc": debank_data.get("binance_collateral_usdc"),
        "binance_available_usdc": debank_data.get("binance_available_usdc"),
        "deribit_equity_usd": debank_data.get("deribit_equity_usd"),
        "deribit_available_usd": debank_data.get("deribit_available_usd"),
        "exchange_balances_json": debank_data.get("exchange_balances_json"),
        "wallet_balance_usd": debank_data.get("wallet_balance_usd"),
        "wallet_native_amount": debank_data.get("wallet_native_amount"),
        # Always store a JSON array (even "[]" when flat) so the history back-fill
        # doesn't treat a closed-position snapshot as "missing" and resurrect
        # stale legs from an earlier snapshot.
        "perp_positions_json": json.dumps(perp_legs or []),
        "bitgo_holdings_json": bitgo_holdings_json,
        "funding_cumulative": funding,
        "price_diff_hl_bn": (price_diff)*cross_mtm_params.get("direction", 1) if price_diff is not None else None,
        "cross_mtm_value": pnl_result["cross_mtm_value"],
        "pnl_usdc": pnl_result["pnl_usdc"],
        "pnl_native": pnl_result["pnl_native"],
        "pnl_pct": pnl_result["pnl_pct"],
        "portfolio_value": pnl_result["portfolio_value"],
        "annualized_return": pnl_result["annualized_return"],
        "raw_debank_json": debank_data.get("raw_debank_json"),
        "scrape_success": bool(debank_data.get("scrape_success", 0)),
    }


async def _fetch_bitgo_managed(enterprise_id: str) -> tuple[list[dict], bool]:
    """Fetch BitGo custody wallets for one enterprise, off the event loop.

    A single access token can reach multiple enterprises (XLtokens); this
    fetches the wallets for the given ``enterprise_id`` (empty string = every
    wallet the token can see). Returns ``(wallets, ok)`` where ``ok``
    distinguishes a successful fetch (even if it legitimately returned zero
    wallets) from a skip/failure, so the caller never persists empty balances
    as if they were real.
    """
    cfg = settings.NAV_BITGO
    if not cfg.get("access_token"):
        logger.warning("BitGo access token not configured — skipping wallet balances")
        return [], False
    try:
        wallets = await asyncio.to_thread(
            bitgo.fetch_all_wallets,
            access_token=cfg["access_token"],
            base_url=cfg.get("base_url") or "https://app.bitgo.com",
            enterprise_id=enterprise_id or "",
        )
        return wallets, True
    except Exception as exc:
        logger.error("BitGo wallet fetch failed for enterprise=%s: %s",
                     enterprise_id or "all", exc)
        return [], False


def _enterprise_by_segment(enabled_segments: dict) -> dict[str, str]:
    """Map each enabled segment to its BitGo enterprise id.

    Uses the per-segment override (BITGO_ENTERPRISE_ID_<segment>) when present,
    otherwise the global BITGO_ENTERPRISE_ID (which may be empty = all wallets).
    """
    cfg = settings.NAV_BITGO
    overrides = cfg.get("enterprise_by_segment", {}) or {}
    default = cfg.get("enterprise_id", "") or ""
    return {seg: overrides.get(seg, default) for seg in enabled_segments}


# Wrapped / L2 variants valued 1:1 at the underlying BTC/ETH price. NOTE: Aave
# aTokens (AETHWETH/AARBWBTC) are deliberately excluded — they are valued by the
# Aave reader, and are filtered out via bitgo.is_atoken() before pricing.
# UETH/UBTC are the Unit-bridged tickers HyperCore spot reports for ETH/BTC —
# a spot buy on Hyperliquid credits "UBTC", not "BTC", and without these the
# holding is left unpriced and drops out of the venue's account value.
_ETH_PRICED = ("ETH", "WETH", "ARBETH", "STETH", "UETH")
_BTC_PRICED = ("BTC", "WBTC", "CBBTC", "TBTC", "UBTC")

# The ticker to ask an exchange for when pricing one of the variants above.
# Derived from the same tuples so the two can't drift apart.
_PRICE_BASE_ALIASES = {
    **{_s: "ETH" for _s in _ETH_PRICED},
    **{_s: "BTC" for _s in _BTC_PRICED},
}


def _price_by_symbol(prices: dict) -> dict[str, float]:
    """Build a SYMBOL->USD price map for valuing custody + exchange assets.

    Maps wrapped/L2 variants (WETH, ARBETH, WBTC, …) to their base BTC/ETH price
    so e.g. Arbitrum ETH isn't left unpriced.
    """
    out: dict[str, float] = {}
    btc = prices.get("btc_binance")
    eth = prices.get("eth_binance")
    if btc is not None:
        out.update({s: btc for s in _BTC_PRICED})
    if eth is not None:
        out.update({s: eth for s in _ETH_PRICED})
    return out


def _bitgo_segment_data(segment_base_asset: str, aggregate: dict, ok: bool) -> dict:
    """Map aggregated BitGo holdings into the per-segment fields the snapshot
    builder expects. DeFi fields are intentionally left None — BitGo does not
    expose Aave/sUSDS/Hyperliquid protocol state (sourced separately)."""
    base = (segment_base_asset or "").upper()
    entry = (aggregate.get("by_symbol") or {}).get(base)
    return {
        "aave_supply_amount": None,
        "aave_supply_usd": None,
        "aave_borrow_usdc": None,
        "aave_health_rate": None,
        "aave_net_usd": None,
        "aave_profit": None,
        "susds_eth_balance": None,
        "susds_arbi_balance": None,
        "vault_usd": None,
        "vault_positions_json": None,
        "hl_deposit_usdc": None,
        "wallet_native_amount": entry.get("amount") if entry else None,
        "wallet_balance_usd": entry.get("usd") if entry else None,
        "binance_collateral_usdc": None,
        "raw_debank_json": None,
        "scrape_success": 1 if ok else 0,
    }


def _merge_aave(wallet_data: dict, aave_data: Optional[dict], base_asset: str, prices: dict) -> None:
    """Fold on-chain Aave account data into the per-segment snapshot fields.

    Aave reports USD aggregates; the per-asset collateral *amount* is derived
    from the collateral USD value and the base asset's live price.
    """
    if not aave_data:
        return
    collateral_usd = aave_data.get("collateral_usd")
    debt_usd = aave_data.get("debt_usd")
    wallet_data["aave_supply_usd"] = collateral_usd
    wallet_data["aave_borrow_usdc"] = debt_usd  # debt is USD; ~USDC for a USDC-only borrow
    wallet_data["aave_health_rate"] = aave_data.get("health_factor")
    if collateral_usd is not None and debt_usd is not None:
        wallet_data["aave_net_usd"] = collateral_usd - debt_usd

    price = None
    if base_asset == "ETH":
        price = prices.get("eth_binance")
    elif base_asset == "BTC":
        price = prices.get("btc_binance")
    elif base_asset == "USDC":
        price = 1.0
    if collateral_usd and price:
        wallet_data["aave_supply_amount"] = collateral_usd / price


def _merge_spark(wallet_data: dict, spark_data: Optional[dict]) -> None:
    """Fold on-chain Spark Savings (sUSDS) USD values into the snapshot fields.

    ``susds_arbi_balance`` / ``susds_eth_balance`` hold the USD value of the
    Spark Savings position per chain. Only a chain that was actually read is
    written (so a read failure leaves the field None for the history back-fill),
    and a chain read as empty is written 0.0 (so a withdrawn position isn't
    resurrected from a stale snapshot).
    """
    if not spark_data:
        return
    by_chain = spark_data.get("by_chain") or {}
    arbi = {c: v for c, v in by_chain.items() if "arb" in c.lower()}
    eth = {c: v for c, v in by_chain.items() if "arb" not in c.lower()}
    if arbi:
        wallet_data["susds_arbi_balance"] = sum(arbi.values())
    if eth:
        wallet_data["susds_eth_balance"] = sum(eth.values())


def _merge_vaults(wallet_data: dict, vault_data: Optional[dict]) -> None:
    """Fold ERC-4626 vault positions into the snapshot fields.

    Same degradation rule as Spark: a read failure leaves the field None (so the
    history back-fill keeps the last known value), while a successful read
    holding nothing writes 0.0 (so a withdrawn position isn't resurrected).
    """
    if not vault_data:
        return
    wallet_data["vault_usd"] = vault_data.get("total_usd") or 0.0
    positions = vault_data.get("positions") or []
    wallet_data["vault_positions_json"] = json.dumps(positions) if positions else None


async def _fetch_prices_managed(exchange_config: dict) -> dict:
    """Fetch BTC/ETH prices with guaranteed client cleanup."""
    bn_pub = hl_pub = None
    try:
        bn_pub = create_binance_public()
        hl_pub = create_hyperliquid_public()
        return await fetch_prices(
            bn_pub, hl_pub,
            exchange_config["btc_price_symbol"],
            exchange_config["eth_price_symbol"],
        )
    except Exception as exc:
        logger.error("Price fetch failed: %s", exc)
        return {}
    finally:
        for client in (bn_pub, hl_pub):
            await _close_exchange_client(client)


async def _fetch_funding_managed(
    prefix: str, env_keys: dict, exchange_config: dict,
    since_midnight_utc: datetime,
) -> float:
    """Fetch today's cumulative funding for one account with guaranteed cleanup."""
    bn_auth = hl_auth = None
    try:
        bn_auth = create_binance(
            api_key=env_keys[f"{prefix}_BINANCE_API_KEY"],
            api_secret=env_keys[f"{prefix}_BINANCE_API_SECRET"],
        )
        hl_auth = create_hyperliquid(
            api_key=env_keys[f"{prefix}_HL_API_KEY"],
            private_key=env_keys[f"{prefix}_HL_PRIVATE_KEY"],
            wallet_address=env_keys[f"{prefix}_HL_WALLET_ADDRESS"],
        )
        return await fetch_cumulative_funding(
            bn_auth, hl_auth,
            exchange_config["funding_symbol"],
            since_midnight_utc,
        )
    except Exception as exc:
        logger.error("Funding fetch failed for %s: %s", prefix, exc)
        return 0.0
    finally:
        for client in (bn_auth, hl_auth):
            await _close_exchange_client(client)


async def collect_snapshot_cycle() -> list[int]:
    """Collect snapshots for all enabled segments. Called from Huey task via asyncio.run()."""
    now = datetime.now(timezone.utc)
    current_date = now.date().isoformat()
    since_midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    timestamp_str = now.strftime("%Y-%m-%dT%H:%M:%S")

    env_keys = settings.NAV_ENV_KEYS
    exchange_config = settings.NAV_EXCHANGE

    # Load token registry (prefer new `tokens:` config, fall back to `wallets:`)
    tokens = load_tokens(settings)  # dict[str, Token]
    enabled_tokens_map = {seg: tok for seg, tok in tokens.items() if tok.enabled}
    # Keep the same dict-shape expected elsewhere by converting to plain dicts
    enabled_segments = {seg: tok.to_dict() for seg, tok in enabled_tokens_map.items()}
    print(f"Enabled segments from config: {list(enabled_segments.keys())}")
    if not enabled_segments:
        logger.warning("No enabled segments in config")
        return []

    # Each xltoken has its OWN exchange accounts, keyed by the uppercased segment
    # name (e.g. xlBTC -> XLBTC_BINANCE_API_KEY / XLBTC_HL_WALLET_ADDRESS).
    seg_prefix = {seg: seg.upper() for seg in enabled_segments}

    # Kick off all independent network fetches concurrently. create_task starts
    # them immediately; each helper catches its own errors and returns a safe
    # default, so awaiting never cancels siblings.
    logger.info("Starting parallel fetch: BitGo + prices + exchange funds + Aave for %s",
                ",".join(enabled_segments))
    # Each segment (XLtoken) maps to its own BitGo enterprise. Fetch each
    # distinct enterprise once, concurrently (multiple segments may share one).
    enterprise_by_segment = _enterprise_by_segment(enabled_segments)
    bitgo_tasks = {
        ent: asyncio.create_task(_fetch_bitgo_managed(ent))
        for ent in set(enterprise_by_segment.values())
    }
    prices_task = asyncio.create_task(_fetch_prices_managed(exchange_config))
    funding_tasks = {
        seg: asyncio.create_task(_fetch_funding_managed(
            seg_prefix[seg], env_keys, exchange_config, since_midnight_utc))
        for seg in enabled_segments
    }
    # Aave markets to read on-chain. The wallet addresses to query are derived
    # from the BitGo wallets per enterprise (below), not from config.yaml.
    aave_markets = [m for m in getattr(settings, "NAV_AAVE_MARKETS", []) if m.get("rpc_url")]

    try:
        prices = await prices_task
    except Exception as exc:
        logger.error("Prices fetch raised: %s", exc)
        prices = {}

    async def _resolve(tasks: dict, default):
        out = {}
        for key, task in tasks.items():
            try:
                out[key] = await task
            except Exception as exc:
                logger.error("Fetch failed for %s: %s", key, exc)
                out[key] = default
        return out

    # Price map (SYMBOL -> USD) used to value non-stable exchange/custody assets.
    price_map = _price_by_symbol(prices)

    # Exchange funds are fetched after prices so we can value every asset held
    # (spot + perp), not just USDC. Each xltoken reads its own account.
    hl_value_tasks = {
        seg: asyncio.create_task(fetch_hl_account_value(seg_prefix[seg], env_keys, price_map))
        for seg in enabled_segments
    }
    bn_collat_tasks = {
        seg: asyncio.create_task(fetch_binance_collateral(seg_prefix[seg], env_keys, price_map))
        for seg in enabled_segments
    }
    deribit_tasks = {
        seg: asyncio.create_task(fetch_deribit_equity(seg_prefix[seg], env_keys, price_map))
        for seg in enabled_segments
    }

    # (wallets, ok) per enterprise; empty/failed fetches resolve to ([], False).
    bitgo_by_enterprise = await _resolve(bitgo_tasks, ([], False))
    # Exchange funds/funding, keyed per segment (each xltoken's own account).
    funding_by_segment = await _resolve(funding_tasks, 0.0)
    hl_value_by_segment = await _resolve(hl_value_tasks, None)
    bn_collat_by_segment = await _resolve(bn_collat_tasks, None)
    # Deribit equity per segment (None when the segment has no Deribit creds).
    deribit_by_segment = await _resolve(deribit_tasks, None)

    # Value any exchange asset still unpriced (outside base BTC/ETH/stables) by
    # fetching its live price, then re-value in place — so "everything available"
    # gets a USD value.
    _all_funds = [f for f in list(hl_value_by_segment.values())
                  + list(bn_collat_by_segment.values())
                  + list(deribit_by_segment.values()) if f]
    _unpriced = {
        a.get("coin") for f in _all_funds for a in (f.get("assets") or [])
        if a.get("usd") is None and a.get("amount")
    }
    if _unpriced:
        extra_prices = await _fetch_asset_prices(_unpriced)
        full_price_map = {**price_map, **extra_prices}
        for f in _all_funds:
            _value_assets(f.get("assets"), full_price_map)

    # Pull the public EVM address of every BitGo wallet per enterprise. Both the
    # Aave reader and the Spark Savings reader query all of them across every
    # market, summing per position so nothing is missed regardless of which
    # wallet holds it. Runs after BitGo since it needs those addresses.
    addresses_by_enterprise = {
        ent: bitgo.evm_addresses(wallets)
        for ent, (wallets, ok) in bitgo_by_enterprise.items()
    }

    aave_by_enterprise: dict[str, Optional[dict]] = {}
    if aave_markets:
        aave_tasks = {
            ent: asyncio.create_task(asyncio.to_thread(
                aave.fetch_aave_for_addresses, aave_markets, addrs))
            for ent, addrs in addresses_by_enterprise.items() if addrs
        }
        aave_by_enterprise = await _resolve(aave_tasks, None)

    # Spark Savings (sUSDS): read each enterprise's BitGo wallet addresses
    # on-chain and value at the live global sUSDS->USDS rate (fetched once).
    spark_cfg = getattr(settings, "NAV_SPARK", {}) or {}
    spark_markets = [m for m in spark_cfg.get("markets", []) if m.get("rpc_url")]
    spark_by_enterprise: dict[str, Optional[dict]] = {}
    if spark_markets:
        spark_rate = await asyncio.to_thread(
            spark.fetch_susds_rate, spark_cfg.get("rate_source"))
        spark_tasks = {
            ent: asyncio.create_task(asyncio.to_thread(
                spark.fetch_susds_for_addresses, spark_markets, addrs, spark_rate))
            for ent, addrs in addresses_by_enterprise.items() if addrs
        }
        spark_by_enterprise = await _resolve(spark_tasks, None)

    # ERC-4626 vaults (e.g. Gauntlet USDC Prime). Same BitGo-derived addresses:
    # the share token is held in the wallet, so whichever wallet holds it is
    # found without naming it in config.
    vault_markets = [v for v in getattr(settings, "NAV_VAULTS", []) if v.get("rpc_url")]
    vault_by_enterprise: dict[str, Optional[dict]] = {}
    if vault_markets:
        vault_tasks = {
            ent: asyncio.create_task(asyncio.to_thread(
                vaults.fetch_vaults_for_addresses, vault_markets, addrs, price_map))
            for ent, addrs in addresses_by_enterprise.items() if addrs
        }
        vault_by_enterprise = await _resolve(vault_tasks, None)

    # Value custody assets outside the base BTC/ETH/stables set (e.g. XLM) by
    # fetching their live price. aTokens are left unpriced — the Aave reader
    # values that collateral, so pricing it here would double-count.
    custody_price_map = dict(price_map)
    unpriced_custody = set()
    for ent, (wallets, ok) in bitgo_by_enterprise.items():
        probe = bitgo.aggregate_holdings(wallets, custody_price_map)
        for sym, holding in probe["by_symbol"].items():
            if holding["usd"] is None and holding.get("amount") and not bitgo.is_atoken(sym):
                unpriced_custody.add(sym)
    if unpriced_custody:
        custody_price_map.update(await _fetch_asset_prices(unpriced_custody))

    # Aggregate custody balances per enterprise (valued with live prices). Each
    # segment reads only its own enterprise's holdings, so XLtokens don't share
    # a combined pool.
    bitgo_aggregate_by_enterprise: dict[str, tuple[dict, bool]] = {}
    bitgo_holdings_json_by_enterprise: dict[str, Optional[str]] = {}
    for ent, (wallets, ok) in bitgo_by_enterprise.items():
        aggregate = bitgo.aggregate_holdings(wallets, custody_price_map)
        bitgo_aggregate_by_enterprise[ent] = (aggregate, ok)
        bitgo_holdings_json_by_enterprise[ent] = json.dumps(aggregate) if ok else None


    # Positions + cross-MtM per segment, each read from that xltoken's own
    # exchange account.
    cross_mtm_by_segment: dict[str, dict] = {}
    perp_legs_by_segment: dict[str, list[dict]] = {}
    for seg, wallet_cfg in enabled_segments.items():
        asset = wallet_cfg["base_asset"].upper()
        prefix = seg_prefix[seg]
        logger.info("Fetching positions for segment=%s asset=%s", seg, asset)
        bn_positions = []
        hl_positions = []

        try:
            bn_positions = await fetch_binance_positions(prefix, env_keys)
        except Exception as e:
            logger.error("Binance positions fetch failed for %s: %s", seg, e)
        try:
            hl_positions = await fetch_hl_positions(prefix, env_keys)
        except Exception as e:
            logger.error("HL positions fetch failed for %s: %s", seg, e)

        perp_legs_by_segment[seg] = _build_perp_legs(bn_positions, hl_positions)
        if bn_positions and hl_positions:  # cross-MtM needs both venues
            cross_mtm_by_segment[seg] = compute_cross_mtm_params(asset, bn_positions, hl_positions)
        elif not bn_positions and not hl_positions:
            # Flat on both venues (positions closed) → no basis position, so
            # cross-MtM is zero. (Don't fall back to the config params, which
            # describe a position that no longer exists → phantom PnL.)
            cross_mtm_by_segment[seg] = {"entry_cost": 0.0, "position_size": 0.0, "direction": 1}
        else:
            # Positions on only one venue (partial / transient fetch miss) —
            # keep the last-known config params rather than fabricate a leg.
            logger.warning("Segment %s has positions on only one venue; using default cross MTM", seg)
            cross_mtm_by_segment[seg] = settings.NAV_CROSS_MTM.get(
                asset, {"entry_cost": 0.0, "position_size": 0.0, "direction": 1})

    row_ids: list[int] = []
    for segment, wallet_cfg in enabled_segments.items():
        funding = funding_by_segment.get(segment, 0.0)
        base_asset = wallet_cfg["base_asset"].upper()

        # This segment's own BitGo enterprise holdings (not a shared pool).
        ent = enterprise_by_segment.get(segment, "")
        bitgo_aggregate, bitgo_ok = bitgo_aggregate_by_enterprise.get(
            ent, ({"by_symbol": {}, "holdings": [], "total_usd": 0.0}, False))
        bitgo_holdings_json = bitgo_holdings_json_by_enterprise.get(ent)
        wallet_data = _bitgo_segment_data(base_asset, bitgo_aggregate, bitgo_ok)

        # Exchange funds from this segment's own exchange accounts: equity
        # (collateral) and free/available for both venues.
        hl_funds = hl_value_by_segment.get(segment) or {}
        bn_funds = bn_collat_by_segment.get(segment) or {}
        db_funds = deribit_by_segment.get(segment) or {}
        wallet_data["hl_deposit_usdc"] = hl_funds.get("account_value")
        wallet_data["hl_available_usdc"] = hl_funds.get("available")
        wallet_data["binance_collateral_usdc"] = bn_funds.get("total")
        wallet_data["binance_available_usdc"] = bn_funds.get("available")
        wallet_data["deribit_equity_usd"] = db_funds.get("total")
        wallet_data["deribit_available_usd"] = db_funds.get("available")

        # Per-venue asset breakdown (all assets held at each exchange). Always
        # store a JSON object (even "{}" when empty) so the history back-fill
        # doesn't resurrect stale balances for a fully-drained account.
        exchange_balances = {}
        if bn_funds.get("assets"):
            exchange_balances["Binance"] = bn_funds["assets"]
        if hl_funds.get("assets"):
            exchange_balances["Hyperliquid"] = hl_funds["assets"]
        if db_funds.get("assets"):
            exchange_balances["Deribit"] = db_funds["assets"]
        wallet_data["exchange_balances_json"] = json.dumps(exchange_balances)

        # Aave borrow/lend + health factor: summed across this enterprise's
        # BitGo wallet addresses (read on-chain).
        _merge_aave(wallet_data, aave_by_enterprise.get(ent), base_asset, prices)

        # Spark Savings (sUSDS): summed across this enterprise's wallet
        # addresses (read on-chain), valued at the live sUSDS->USDS rate.
        _merge_spark(wallet_data, spark_by_enterprise.get(ent))
        _merge_vaults(wallet_data, vault_by_enterprise.get(ent))

        cross_mtm = cross_mtm_by_segment.get(segment, {"entry_cost": 0.0, "position_size": 0.0, "direction": 1})

        data = await sync_to_async(_build_snapshot_data)(
            segment=segment,
            wallet_config=wallet_cfg,
            debank_data=wallet_data,
            prices=prices,
            funding=funding,
            cross_mtm_params=cross_mtm,
            timestamp_str=timestamp_str,
            current_date=current_date,
            perp_legs=perp_legs_by_segment.get(segment, []),
            bitgo_holdings_json=bitgo_holdings_json,
        )

        snap = await sync_to_async(Snapshot.objects.create)(**data)
        logger.info("Snapshot stored: segment=%s id=%s pnl_usdc=%.4f",
                    segment, snap.pk, snap.pnl_usdc or 0.0)
        row_ids.append(snap.pk)

    return row_ids


def finalize_daily(segment: str, date_str: str) -> None:
    """Create or update a DailyPnl row from the last snapshot of that date."""
    next_date = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    day_start = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    day_end = datetime.fromisoformat(next_date).replace(tzinfo=timezone.utc)
    snapshot = (
        Snapshot.objects
        .filter(segment=segment, timestamp__gte=day_start, timestamp__lt=day_end)
        .order_by("-timestamp")
        .first()
    )
    if snapshot is None:
        logger.warning("No snapshot found for segment=%s date=%s", segment, date_str)
        return

    portfolio_value = snapshot.portfolio_value
    prior_max = _get_rolling_max(segment, before_date=date_str)
    candidates = [v for v in (prior_max, portfolio_value) if v is not None]
    rolling_max = max(candidates) if candidates else portfolio_value

    if rolling_max and portfolio_value and rolling_max > 0:
        drawdown = (portfolio_value - rolling_max) / rolling_max
    else:
        drawdown = 0.0

    DailyPnl.objects.update_or_create(
        date=date_str,
        segment=segment,
        defaults={
            "pnl_usdc": snapshot.pnl_usdc,
            "pnl_native": snapshot.pnl_native,
            "pnl_pct": snapshot.pnl_pct,
            "portfolio_value": portfolio_value,
            "annualized_return": snapshot.annualized_return,
            "funding_total": snapshot.funding_cumulative,
            "rolling_max": rolling_max,
            "drawdown": drawdown,
            "asset_price": snapshot.eth_price if segment == "xlETH" else snapshot.btc_price,
            "price_diff_hl_bn": snapshot.price_diff_hl_bn,
            "aave_borrow_value": snapshot.aave_borrow_usdc,
            "aave_profit_value": snapshot.aave_profit,
            "susds_eth_value": snapshot.susds_eth_balance,
            "susds_arbi_value": snapshot.susds_arbi_balance,
            "cross_mtm_value": snapshot.cross_mtm_value,
            "snapshot": snapshot,
        },
    )
    logger.info("Daily PnL finalized: segment=%s date=%s drawdown=%.4f",
                segment, date_str, drawdown)


def finalize_daily_all_segments() -> None:
    """Finalize yesterday's daily PnL for all enabled segments."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    from tracker.config import load_tokens

    for segment, token in load_tokens().items():
        if not token.enabled:
            continue
        try:
            finalize_daily(segment, yesterday)
        except Exception as exc:
            logger.error("Daily finalization failed for %s: %s", segment, exc, exc_info=True)


def finalize_daily_for_date(date_str: str) -> None:
    """Finalize a specific date for all enabled segments."""
    from tracker.config import load_tokens

    for segment, token in load_tokens().items():
        if not token.enabled:
            continue
        finalize_daily(segment, date_str)


def _prev_date(date_str: str) -> str:
    d = date.fromisoformat(date_str)
    return (d - timedelta(days=1)).isoformat()
