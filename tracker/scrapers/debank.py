"""DeBank Playwright scraper with XHR interception for DeFi position tracking."""

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Protocol ID patterns for matching DeBank API responses
PROTOCOL_IDS: dict[str, list[str]] = {
    "aave": ["avax_aave3", "aave3", "aave_v3", "aave"],
    "hyperliquid": ["hyperliquid"],
    "sky": ["sky", "eth_sky"],
}

# Segments for which Aave positions are extracted.
# This is derived from configured tokens rather than hard-coded names.

def _segment_uses_aave(segment: str) -> bool:
    try:
        from tracker.config import load_tokens
    except Exception:
        return segment in {"xlBTC", "xlETH"}

    token = load_tokens().get(segment)
    return bool(token and token.base_asset in {"BTC", "ETH"})


# Symbols that count as the native reserve asset for a given base_asset.
_NATIVE_SYMBOL_ALIASES: dict[str, set[str]] = {
    "BTC": {"BTC", "WBTC", "CBBTC", "TBTC"},
    "ETH": {"ETH", "WETH", "STETH", "WSTETH"},
    "USDC": {"USDC", "USDC.E"},
}


def _segment_base_asset(segment: str) -> str:
    """Resolve the base asset (BTC/ETH/USDC) for a segment, '' if unknown."""
    try:
        from tracker.config import load_tokens
    except Exception:
        return ""
    token = load_tokens().get(segment)
    return token.base_asset.upper() if token else ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_protocol(data: dict, protocol_key: str) -> dict | None:
    """Find a protocol entry in project_list data by matching known IDs."""
    id_patterns = PROTOCOL_IDS.get(protocol_key, [])
    for project in data.get("data", []):
        project_id = project.get("id", "").lower()
        for pattern in id_patterns:
            if project_id == pattern.lower():
                return project
    return None


def _get_portfolio_item(project: dict, item_name: str) -> dict | None:
    """Return the first portfolio item whose name matches (case-insensitive)."""
    for item in project.get("portfolio_item_list", []):
        if item.get("name", "").lower() == item_name.lower():
            return item
    return None


def _find_token(token_list: list[dict], symbol: str) -> dict | None:
    """Find a token entry by symbol (case-insensitive)."""
    for token in token_list:
        if token.get("symbol", "").upper() == symbol.upper():
            return token
    return None


# ---------------------------------------------------------------------------
# Public parser functions
# ---------------------------------------------------------------------------


def parse_project_list(data: dict, segment: str) -> dict[str, Any]:
    """Extract all DeFi position values from a DeBank project_list response.

    Args:
        data: Parsed JSON from the DeBank portfolio/project_list endpoint.
        segment: Fund segment name (e.g. "xlBTC", "xlETH").

    Returns:
        Dict with keys:
            aave_supply_amount, aave_supply_usd,
            aave_borrow_usdc, aave_health_rate, aave_net_usd, aave_profit,
            susds_eth_balance, susds_arbi_balance, hl_deposit_usdc.
        Missing/absent values are None.
    """
    result: dict[str, Any] = {
        "aave_supply_amount": None,
        "aave_supply_usd": None,
        "aave_borrow_usdc": None,
        "aave_health_rate": None,
        "aave_net_usd": None,
        "aave_profit": None,
        "susds_eth_balance": None,
        "susds_arbi_balance": None,
        "hl_deposit_usdc": None,
    }

    # --- Aave (only for BTC/ETH-backed segments by config) ---
    # if _segment_uses_aave(segment):
    aave = _find_protocol(data, "aave")
    if aave:
        lending_item = _get_portfolio_item(aave, "Lending")
        if lending_item:
            detail = lending_item.get("detail", {})
            stats = lending_item.get("stats", {})
            supply_list = detail.get("supply_token_list", [])
            if supply_list:
                # Take first supply token as the primary collateral
                primary = supply_list[0]
                result["aave_supply_amount"] = primary.get("amount")
            # aave_supply_usd from stats asset_usd_value
            if "asset_usd_value" in stats:
                result["aave_supply_usd"] = stats["asset_usd_value"]
            borrow_list = detail.get("borrow_token_list", [])
            usdc_borrow = _find_token(borrow_list, "USDC")
            if usdc_borrow:
                result["aave_borrow_usdc"] = usdc_borrow.get("amount")
            if "health_rate" in detail:
                result["aave_health_rate"] = detail["health_rate"]
            if "net_usd_value" in stats:
                result["aave_net_usd"] = stats["net_usd_value"]

    # --- Hyperliquid ---
    hl = _find_protocol(data, "hyperliquid")
    if hl:
        deposit_item = _get_portfolio_item(hl, "Deposit")
        if deposit_item:
            supply_list = deposit_item.get("detail", {}).get("supply_token_list", [])
            usdc_token = _find_token(supply_list, "USDC")
            if usdc_token:
                result["hl_deposit_usdc"] = usdc_token.get("amount")

    # --- Sky (sUSDS / USDS) ---
    sky = _find_protocol(data, "sky")
    if sky:
        deposit_item = (
            _get_portfolio_item(sky, "Yield")
            or _get_portfolio_item(sky, "Deposit")
            or _get_portfolio_item(sky, "Lending")
        )
        if deposit_item:
            stats = deposit_item.get("stats", {})
            supply_list = deposit_item.get("detail", {}).get("supply_token_list", [])
            susds_token = _find_token(supply_list, "sUSDS") or _find_token(supply_list, "USDS")
            if susds_token:
                # Use USD value: stats.asset_usd_value or amount * price
                usd_value = stats.get("asset_usd_value")
                if usd_value is None:
                    amount = susds_token.get("amount")
                    price = susds_token.get("price", 1.0)
                    if amount is not None:
                        usd_value = amount * price

                # Assign to eth or arbi based on chain or protocol ID
                chain = sky.get("chain", "").lower()
                pid = sky.get("id", "").lower()
                if "arbi" in pid or "arbitrum" in pid or chain == "arb":
                    result["susds_arbi_balance"] = usd_value
                else:
                    result["susds_eth_balance"] = usd_value

    return result


def parse_token_list(data: dict) -> dict[str, Any]:
    """Extract sUSDS wallet token USD values from a DeBank balance_list response.

    The balance_list contains wallet-held tokens (not protocol positions).
    sUSDS on Arbitrum appears here as a wallet token with symbol "sUSDS"
    and chain "arb".  We store amount * price (USD value), not raw amount.

    Args:
        data: Parsed JSON from the DeBank token/balance_list endpoint.

    Returns:
        Dict with key: susds_arbi_balance (USD value, None if not found).
    """
    result: dict[str, Any] = {"susds_arbi_balance": None}

    tokens = data if isinstance(data, list) else data.get("data", [])
    for token in tokens:
        symbol = token.get("symbol", "").upper()
        chain = token.get("chain", "").lower()
        if symbol == "SUSDS" and chain == "arb":
            amount = token.get("amount")
            price = token.get("price", 1.0)
            if amount is not None:
                result["susds_arbi_balance"] = amount * price

    return result


def parse_native_balance(data: dict, base_asset: str) -> dict[str, Any]:
    """Extract the free wallet balance of the segment's native asset.

    Scans a DeBank token/balance_list response for wallet-held tokens whose
    symbol matches the base asset (or a known alias) and sums their amounts.
    This represents the "Native reserve" — coins sitting in the wallet, not
    deployed into a protocol.

    Args:
        data: Parsed JSON from the DeBank token balance_list endpoint.
        base_asset: Segment base asset, e.g. "ETH", "BTC", "USDC".

    Returns:
        Dict with key: wallet_native_amount (summed amount, None if not found).
    """
    aliases = _NATIVE_SYMBOL_ALIASES.get((base_asset or "").upper())
    if not aliases:
        return {"wallet_native_amount": None}

    tokens = data if isinstance(data, list) else data.get("data", [])
    total = None
    for token in tokens:
        if token.get("symbol", "").upper() in aliases:
            amount = token.get("amount")
            if amount is not None:
                total = (total or 0.0) + float(amount)
    return {"wallet_native_amount": total}


def parse_total_balance(data: dict) -> dict[str, Any]:
    """Extract total wallet balance from a DeBank total_balance response.

    Args:
        data: Parsed JSON from the DeBank total_balance endpoint.

    Returns:
        Dict with key: wallet_balance_usd.
    """
    total_usd = data.get("data", {}).get("total_usd_value")
    return {"wallet_balance_usd": total_usd}


# ---------------------------------------------------------------------------
# Async scraper
# ---------------------------------------------------------------------------


INTERCEPT_PATTERNS = [
    "portfolio/project_list",
    "complex_protocol_list",
    "total_balance",
    "token/balance_list",
    "token/cache_balance_list",
]

# The key XHR pattern we wait for before considering a page "loaded"
_REQUIRED_PATTERN = "portfolio/project_list"


def _empty_result() -> dict[str, Any]:
    """Return a result dict with all keys set to None / default."""
    return {
        "aave_supply_amount": None,
        "aave_supply_usd": None,
        "aave_borrow_usdc": None,
        "aave_health_rate": None,
        "aave_net_usd": None,
        "aave_profit": None,
        "susds_eth_balance": None,
        "susds_arbi_balance": None,
        "hl_deposit_usdc": None,
        "wallet_balance_usd": None,
        "wallet_native_amount": None,
        "raw_debank_json": None,
        "scrape_success": 0,
    }


def _parse_captured(captured_responses: list[dict], segment: str) -> dict[str, Any]:
    """Parse all captured XHR responses into a single result dict."""
    result = _empty_result()
    base_asset = _segment_base_asset(segment)

    result["raw_debank_json"] = json.dumps(captured_responses) if captured_responses else None

    for entry in captured_responses:
        resp_url: str = entry["url"]
        body: dict = entry["body"]

        if "portfolio/project_list" in resp_url or "complex_protocol_list" in resp_url:
            parsed = parse_project_list(body, segment)
            for key, value in parsed.items():
                if value is not None:
                    result[key] = value

        elif "token/balance_list" in resp_url or "token/cache_balance_list" in resp_url:
            parsed_tokens = parse_token_list(body)
            for key, value in parsed_tokens.items():
                if value is not None:
                    result[key] = value
            parsed_native = parse_native_balance(body, base_asset)
            if parsed_native.get("wallet_native_amount") is not None:
                result["wallet_native_amount"] = parsed_native["wallet_native_amount"]

        elif "total_balance" in resp_url:
            parsed_balance = parse_total_balance(body)
            if parsed_balance.get("wallet_balance_usd") is not None:
                result["wallet_balance_usd"] = parsed_balance["wallet_balance_usd"]

    # Log Aave health rate warnings
    health_rate = result.get("aave_health_rate")
    if health_rate is not None:
        if health_rate < 1.1:
            logger.critical(
                "CRITICAL: Aave health rate %.3f is dangerously low (segment=%s)",
                health_rate, segment,
            )
        elif health_rate < 1.5:
            logger.warning(
                "WARNING: Aave health rate %.3f is below safe threshold (segment=%s)",
                health_rate, segment,
            )

    # A scrape is only "successful" if we got a non-429 project_list response.
    # Zero captures, all-429s, or captures-without-project_list all count as failures.
    project_list_ok = any(
        ("portfolio/project_list" in entry["url"]
         or "complex_protocol_list" in entry["url"])
        and entry.get("body", {}).get("error_code") != 429
        for entry in captured_responses
    )
    if not project_list_ok:
        if not captured_responses:
            reason = "no XHRs captured"
        elif all(entry.get("body", {}).get("error_code") == 429
                 for entry in captured_responses):
            reason = "rate-limited (429)"
        else:
            reason = f"project_list missing among {len(captured_responses)} XHRs"
        logger.warning("DeBank scrape failed for segment=%s — %s", segment, reason)
        result["scrape_success"] = 0
        return result

    result["scrape_success"] = 1
    return result


async def _scrape_single_page(browser, url: str, segment: str) -> dict[str, Any]:
    """Scrape one DeBank wallet page inside an already-running browser.

    Uses ``page.route()`` to intercept DeBank API calls before the response
    body is consumed, which is more reliable than passive ``response`` events.
    Waits for the critical ``project_list`` XHR via an asyncio.Event instead
    of a sleep-polling loop.
    """
    captured_responses: list[dict] = []
    project_list_ready = asyncio.Event()

    context = await browser.new_context()
    try:
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        async def handle_route(route):
            req_url = route.request.url
            is_target = any(pat in req_url for pat in INTERCEPT_PATTERNS)

            if not is_target:
                await route.continue_()
                return

            try:
                response = await route.fetch()
                try:
                    body = await response.json()
                    captured_responses.append({"url": req_url, "body": body})
                    logger.info("Captured DeBank XHR: %s (segment=%s)", req_url, segment)
                    if _REQUIRED_PATTERN in req_url:
                        project_list_ready.set()
                except Exception as exc:
                    logger.error(
                        "JSON parse failed (status=%d) for %s (segment=%s): %s",
                        response.status, req_url, segment, exc,
                    )
                await route.fulfill(response=response)
            except Exception as exc:
                logger.error("Route intercept failed for %s: %s", req_url, exc)
                await route.continue_()

        await page.route("**/api.debank.com/**", handle_route)

        logger.info("Navigating to %s (segment=%s)", url, segment)
        # domcontentloaded + event-driven XHR wait is more reliable than
        # networkidle on DeBank (which fires XHRs in bursts)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Content verification — detect bot-blocks / CAPTCHA pages
        try:
            title = await page.title()
            if title and "debank" not in title.lower():
                logger.warning(
                    "Unexpected page title for segment=%s: %r — possibly blocked",
                    segment, title,
                )
        except Exception:
            pass

        # Event-driven wait for project_list XHR (up to 25 s)
        if not project_list_ready.is_set():
            try:
                await asyncio.wait_for(project_list_ready.wait(), timeout=25.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "project_list XHR not received after 25 s (segment=%s) — "
                    "captured %d other XHRs",
                    segment, len(captured_responses),
                )

        # Brief grace period so any in-flight route handlers can finish
        # before context.close() disposes their responses
        await asyncio.sleep(1.0)
    finally:
        await context.close()

    return _parse_captured(captured_responses, segment)


async def _scrape_safe(browser, url: str, segment: str) -> tuple[str, dict[str, Any]]:
    """Wrap _scrape_single_page with error handling, returning (segment, data)."""
    try:
        return segment, await _scrape_single_page(browser, url, segment)
    except Exception as exc:
        logger.error("DeBank scrape failed for segment=%s: %s", segment, exc)
        return segment, _empty_result()


async def scrape_wallets(
    wallet_configs: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """Scrape multiple DeBank wallets in a single browser session, in parallel.

    Each wallet gets its own browser context (tab), so all pages load
    simultaneously instead of sequentially.

    Args:
        wallet_configs: List of ``(url, segment)`` tuples.

    Returns:
        Dict mapping segment name -> scraped data dict.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            pairs = await asyncio.gather(
                *[_scrape_safe(browser, url, segment) for url, segment in wallet_configs]
            )
        finally:
            await browser.close()

    return dict(pairs)


async def scrape_wallet(url: str, segment: str) -> dict[str, Any]:
    """Scrape a single DeBank wallet page (convenience wrapper).

    Launches Chromium (visible), navigates to *url*, and intercepts XHR
    responses from the DeBank API.

    For scraping multiple wallets, prefer :func:`scrape_wallets` which
    reuses a single browser instance.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            return await _scrape_single_page(browser, url, segment)
        except Exception:
            return _empty_result()
        finally:
            await browser.close()


async def scrape_wallets_sequential(
    wallet_configs: list[tuple[str, str]],
    pause_between_seconds: float = 3.0,
) -> dict[str, dict[str, Any]]:
    """Scrape multiple wallets *sequentially* in a single browser session.

    Unlike :func:`scrape_wallets` (parallel), this processes one page at a
    time to eliminate resource contention — useful for retry passes where
    reliability matters more than latency.

    Args:
        wallet_configs: List of ``(url, segment)`` tuples.
        pause_between_seconds: Delay between segments to reduce DeBank
            rate-limit pressure.

    Returns:
        Dict mapping segment name -> scraped data dict.
    """
    from playwright.async_api import async_playwright

    results: dict[str, dict[str, Any]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            for idx, (url, segment) in enumerate(wallet_configs):
                if idx > 0 and pause_between_seconds > 0:
                    await asyncio.sleep(pause_between_seconds)
                _, data = await _scrape_safe(browser, url, segment)
                results[segment] = data
        finally:
            await browser.close()
    return results
