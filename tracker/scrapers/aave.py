"""On-chain Aave v3 position reader.

Reads a wallet's Aave position directly from the lending Pool via a single
``getUserAccountData(address)`` ``eth_call`` over JSON-RPC. That view returns
the borrow, the supplied collateral, the liquidation threshold and the
**health factor** in one call — exact and auditable, with no scraping and no
heavy dependencies (uses ``requests``, already vendored).

Why not web3.py: this runs in restricted environments where adding/installing
new packages is not always possible. Raw JSON-RPC keeps the dependency surface
at zero. The 4-byte selector is a hard-coded constant verified by a unit test
(``test_aave.py``) that recomputes keccak256 to guard against drift.

Aave v3 returns monetary values in the market "base currency" (USD) with 8
decimals; liquidation threshold and LTV in basis points (1e4); health factor
scaled by 1e18 (``type(uint256).max`` when there is no debt → no liquidation
risk, reported here as ``None``).
"""

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# keccak256("getUserAccountData(address)")[:4] — verified in tests/test_aave.py
GET_USER_ACCOUNT_DATA_SELECTOR = "0xbf92857c"

_BASE_DECIMALS = 8          # Aave v3 base-currency (USD) decimals
_BPS = 10_000               # liquidation threshold / LTV scale
_HF_SCALE = 10 ** 18        # health factor fixed-point scale
_UINT256_MAX = 2 ** 256 - 1


def _encode_get_user_account_data(user_address: str) -> str:
    """ABI-encode the getUserAccountData(address) call data."""
    addr = user_address.lower().removeprefix("0x")
    if len(addr) != 40:
        raise ValueError(f"invalid address: {user_address!r}")
    return GET_USER_ACCOUNT_DATA_SELECTOR + addr.rjust(64, "0")


def _decode_account_data(result_hex: str) -> dict:
    """Decode the 6 uint256 words returned by getUserAccountData."""
    raw = result_hex.removeprefix("0x")
    if len(raw) < 6 * 64:
        raise ValueError(f"short eth_call result: {len(raw)} hex chars")
    words = [int(raw[i * 64:(i + 1) * 64], 16) for i in range(6)]
    (total_collateral, total_debt, available_borrows,
     liq_threshold, ltv, health_factor_raw) = words

    has_debt = total_debt > 0
    health_factor = (
        health_factor_raw / _HF_SCALE
        if has_debt and health_factor_raw != _UINT256_MAX
        else None
    )
    return {
        "collateral_usd": total_collateral / 10 ** _BASE_DECIMALS,
        "debt_usd": total_debt / 10 ** _BASE_DECIMALS,
        "available_borrows_usd": available_borrows / 10 ** _BASE_DECIMALS,
        "liquidation_threshold": liq_threshold / _BPS,
        "ltv": ltv / _BPS,
        "health_factor": health_factor,
    }


def fetch_aave_account_data(
    rpc_url: str, pool_address: str, user_address: str, timeout: int = 30,
) -> dict:
    """eth_call getUserAccountData on one Aave Pool for one wallet.

    Raises on RPC/transport error or a JSON-RPC error object so the caller can
    decide how to degrade rather than persisting bogus zeros.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": pool_address, "data": _encode_get_user_account_data(user_address)},
            "latest",
        ],
    }
    resp = requests.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    return _decode_account_data(body["result"])


def fetch_aave_for_wallet(markets: list[dict], user_address: str) -> Optional[dict]:
    """Aggregate a wallet's Aave position across one or more markets/chains.

    Args:
        markets: list of {"rpc_url": str, "pool": str, "chain": str}.
        user_address: the wallet address to query.

    Returns:
        Aggregated dict, or None if no market could be read. USD collateral/debt
        are summed across chains; the health factor is the **minimum** across
        markets that carry debt (most conservative / closest to liquidation).
    """
    if not user_address or not markets:
        return None

    collateral_usd = debt_usd = available_usd = 0.0
    health_factors: list[float] = []
    liq_thresholds: list[float] = []
    any_ok = False

    for market in markets:
        rpc_url = market.get("rpc_url")
        pool = market.get("pool")
        if not rpc_url or not pool:
            continue
        try:
            data = fetch_aave_account_data(rpc_url, pool, user_address)
        except Exception as exc:
            logger.error(
                "Aave read failed (chain=%s, user=%s): %s",
                market.get("chain"), user_address, exc,
            )
            continue
        any_ok = True
        collateral_usd += data["collateral_usd"]
        debt_usd += data["debt_usd"]
        available_usd += data["available_borrows_usd"]
        if data["health_factor"] is not None:
            health_factors.append(data["health_factor"])
        if data["collateral_usd"] > 0:
            liq_thresholds.append(data["liquidation_threshold"])

    if not any_ok:
        return None

    return {
        "collateral_usd": collateral_usd,
        "debt_usd": debt_usd,
        "available_borrows_usd": available_usd,
        "health_factor": min(health_factors) if health_factors else None,
        "liquidation_threshold": min(liq_thresholds) if liq_thresholds else None,
    }


def fetch_aave_for_addresses(
    markets: list[dict], addresses: list[str],
) -> Optional[dict]:
    """Aggregate Aave positions across many wallet addresses (and markets).

    Sums collateral/debt/available across every address that has a position;
    health factor and liquidation threshold are the **minimum** across positions
    carrying debt (most conservative). Returns None if no address could be read
    on any market. Addresses with no Aave position simply contribute zero.
    """
    if not markets or not addresses:
        return None

    collateral_usd = debt_usd = available_usd = 0.0
    health_factors: list[float] = []
    liq_thresholds: list[float] = []
    any_ok = False

    for address in addresses:
        data = fetch_aave_for_wallet(markets, address)
        if data is None:
            continue
        any_ok = True
        collateral_usd += data["collateral_usd"]
        debt_usd += data["debt_usd"]
        available_usd += data["available_borrows_usd"]
        if data["health_factor"] is not None:
            health_factors.append(data["health_factor"])
        if data["liquidation_threshold"] is not None and data["collateral_usd"] > 0:
            liq_thresholds.append(data["liquidation_threshold"])

    if not any_ok:
        return None

    return {
        "collateral_usd": collateral_usd,
        "debt_usd": debt_usd,
        "available_borrows_usd": available_usd,
        "health_factor": min(health_factors) if health_factors else None,
        "liquidation_threshold": min(liq_thresholds) if liq_thresholds else None,
    }
