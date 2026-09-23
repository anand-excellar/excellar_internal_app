"""PnL computation for each portfolio segment."""

import logging

logger = logging.getLogger(__name__)

# Initial portfolio values (in native asset)
INITIAL_VALUES = {
    "xlBTC": 0.03301,
    "xlETH": 1.1084,
    "xlUSD": 3336.0,
}


def _safe_get(data: dict | None, key: str, default: float = 0.0) -> float:
    """Get a numeric value from a dict, returning default if missing or None."""
    if data is None:
        return default
    val = data.get(key)
    return val if val is not None else default


def compute_cross_mtm(price_diff: float, entry_cost: float, position_size: float) -> float:
    """Compute cross MtM value: entry_cost - (price_diff * position_size)."""
    return entry_cost - (price_diff * position_size)


# Maps segment base_asset to the price key in current_data
_NATIVE_PRICE_KEY = {
    "BTC": "btc_price",
    "ETH": "eth_price",
    "USDC": None,  # USDC is already in USDC, no conversion needed
}


def compute_pnl(
    segment: str,
    base_asset: str,
    current: dict,
    reference: dict | None,
    funding_today: float,
    cross_mtm_params: dict,
    prev_day_portfolio_value: float | None = None,
) -> dict:
    """Compute cumulative intraday PnL for any segment.

    Args:
        segment: Fund segment name (e.g. "xlBTC", "xlETH", "xlUSD").
        base_asset: Native asset for this segment ("BTC", "ETH", "USDC").
        current: Current snapshot data (aave_borrow_usdc,
            btc_price, eth_price, price_diff_hl_bn).
        reference: Last snapshot of previous UTC day (same keys). None for first day.
        funding_today: Cumulative funding income from 00:00 UTC to now.
        cross_mtm_params: Dict with entry_cost and position_size.
        prev_day_portfolio_value: Portfolio value at end of previous day (in native asset).

    Returns:
        Dict with: cross_mtm_value, pnl_usdc, pnl_native, pnl_pct, portfolio_value, annualized_return.
    """
    initial_value = INITIAL_VALUES.get(segment, 1.0)
    if prev_day_portfolio_value is None:
        prev_day_portfolio_value = initial_value

    # Component deltas (only compute if we have a reference snapshot)
    if reference is not None:
        aave_delta = (
            _safe_get(reference, "aave_borrow_usdc")
            - _safe_get(current, "aave_borrow_usdc")
        )

        susds_eth_delta = (
            _safe_get(current, "susds_eth_balance")
            - _safe_get(reference, "susds_eth_balance")
        )

        susds_arbi_delta = (
            _safe_get(current, "susds_arbi_balance")
            - _safe_get(reference, "susds_arbi_balance")
        )
    else:
        aave_delta = 0.0
        susds_eth_delta = 0.0
        susds_arbi_delta = 0.0

    # Cross MtM
    price_diff = _safe_get(current, "price_diff_hl_bn")
    cross_mtm_now = compute_cross_mtm(
        price_diff, cross_mtm_params["entry_cost"], cross_mtm_params["position_size"]
    )
    if reference is not None:
        cross_mtm_ref = _safe_get(reference, "cross_mtm_value")
        cross_mtm_delta = cross_mtm_now - cross_mtm_ref
    else:
        cross_mtm_delta = 0.0

    # SUSDS yield is only applicable to the xlUSD segment
    susds_component = (susds_eth_delta + susds_arbi_delta) if segment == "xlUSD" else 0.0

    # Cumulative PnL in USDC
    pnl_usdc = aave_delta + susds_component + funding_today + cross_mtm_delta

    # Convert to native asset
    price_key = _NATIVE_PRICE_KEY.get(base_asset, "btc_price")
    if price_key is None:
        # USDC segment: native = USDC, no conversion
        native_price = 1.0
    else:
        native_price = _safe_get(current, price_key, 1.0)

    pnl_native = pnl_usdc / native_price if native_price > 0 else 0.0

    pnl_pct = pnl_native / initial_value if initial_value > 0 else 0.0
    portfolio_value = prev_day_portfolio_value + pnl_native
    annualized = pnl_pct * 365

    logger.debug(
        "%s PnL: aave=%.2f susds=%.2f funding=%.4f cross_mtm=%.4f -> usdc=%.4f native=%.8f",
        segment, aave_delta, susds_component, funding_today, cross_mtm_delta, pnl_usdc, pnl_native,
    )

    return {
        "cross_mtm_value": cross_mtm_now,
        "pnl_usdc": pnl_usdc,
        "pnl_native": pnl_native,
        "pnl_pct": pnl_pct,
        "portfolio_value": portfolio_value,
        "annualized_return": annualized,
    }
