"""On-chain Spark Savings (sUSDS) position reader.

Reads a wallet's Spark Savings position by querying its sUSDS ERC-20 balance on
each configured chain via ``balanceOf(address)`` and valuing it at the live
sUSDS -> USDS exchange rate. Spark Savings is *not* a lending pool: the sUSDS
token held in the wallet **is** the position, so there is no pool to query the
way the Aave reader does — only the token balance and its redemption rate.

Why a separate rate source: sUSDS is Sky's savings token. On Ethereum mainnet it
is a full ERC-4626 vault and exposes ``convertToAssets``; on some cross-chain
deployments (e.g. the Arbitrum token reached via Sky's SkyLink layer) those
views revert. The share->asset rate is driven by the **global** Sky Savings
Rate, so it is identical on every chain — we read it once from a market that
implements ``convertToAssets`` (mainnet sUSDS) and apply it to balances read on
any chain.

Like the Aave reader, this uses raw JSON-RPC ``eth_call`` (no web3.py) to keep
the dependency surface at zero. The 4-byte selectors are hard-coded constants
verified in tests/test_spark.py against keccak256.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# keccak256("balanceOf(address)")[:4] — verified in tests/test_spark.py
BALANCE_OF_SELECTOR = "0x70a08231"
# keccak256("convertToAssets(uint256)")[:4] — verified in tests/test_spark.py
CONVERT_TO_ASSETS_SELECTOR = "0x07a2d13a"

_WAD = 10 ** 18  # sUSDS and USDS both use 18 decimals


def _encode_address_arg(selector: str, address: str) -> str:
    """ABI-encode a call taking a single address argument."""
    addr = address.lower().removeprefix("0x")
    if len(addr) != 40:
        raise ValueError(f"invalid address: {address!r}")
    return selector + addr.rjust(64, "0")


def _encode_uint_arg(selector: str, value: int) -> str:
    """ABI-encode a call taking a single uint256 argument."""
    return selector + f"{value:064x}"


def _eth_call(rpc_url: str, to: str, data: str, timeout: int = 30) -> int:
    """eth_call returning a single uint256 word (0 for empty/'0x' results).

    Raises on RPC/transport error or a JSON-RPC error object so the caller can
    decide how to degrade rather than persisting bogus zeros.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    resp = requests.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    raw = body.get("result")
    if not raw or raw == "0x":
        return 0
    return int(raw, 16)


def fetch_susds_rate(rate_source: Optional[dict], timeout: int = 30) -> float:
    """sUSDS -> USDS exchange rate (assets per share) via convertToAssets(1e18).

    Returns 1.0 (a conservative floor) when the rate source is missing or the
    call fails — sUSDS is always worth >= 1 USDS, so 1.0 only ever *under*-values
    the position rather than inventing value.
    """
    if not rate_source:
        return 1.0
    rpc_url = rate_source.get("rpc_url")
    token = rate_source.get("token")
    if not rpc_url or not token:
        return 1.0
    try:
        assets = _eth_call(
            rpc_url, token, _encode_uint_arg(CONVERT_TO_ASSETS_SELECTOR, _WAD), timeout
        )
        rate = assets / _WAD
        return rate if rate > 0 else 1.0
    except Exception as exc:
        logger.error("sUSDS rate fetch failed (chain=%s): %s",
                     rate_source.get("chain"), exc)
        return 1.0


def fetch_susds_balance(rpc_url: str, token: str, address: str, timeout: int = 30) -> float:
    """sUSDS share balance (human units) held by one address on one chain."""
    shares = _eth_call(
        rpc_url, token, _encode_address_arg(BALANCE_OF_SELECTOR, address), timeout
    )
    return shares / _WAD


def fetch_susds_for_addresses(
    markets: list[dict], addresses: list[str], rate: float = 1.0,
) -> Optional[dict]:
    """Aggregate Spark Savings (sUSDS) USD value across addresses and chains.

    Args:
        markets: list of {"chain": str, "token": str, "rpc_url": str}.
        addresses: wallet addresses to query.
        rate: sUSDS -> USDS exchange rate (from :func:`fetch_susds_rate`).

    Returns:
        ``{"by_chain": {chain: usd}, "total_usd": float}`` summed across every
        address/chain, or None if no market could be read at all. A chain that
        reads OK but holds nothing contributes 0.0 — so a withdrawn position is
        stored as 0, not left None (which the history back-fill would otherwise
        resurrect from a stale snapshot).
    """
    if not markets or not addresses:
        return None

    by_chain: dict[str, float] = {}
    any_ok = False
    for market in markets:
        rpc_url = market.get("rpc_url")
        token = market.get("token")
        chain = market.get("chain", "")
        if not rpc_url or not token:
            continue
        chain_shares = 0.0
        chain_ok = False
        for address in addresses:
            try:
                chain_shares += fetch_susds_balance(rpc_url, token, address)
                chain_ok = True
            except Exception as exc:
                logger.error("sUSDS read failed (chain=%s, user=%s): %s",
                             chain, address, exc)
        if chain_ok:
            any_ok = True
            by_chain[chain] = by_chain.get(chain, 0.0) + chain_shares * rate

    if not any_ok:
        return None
    return {"by_chain": by_chain, "total_usd": sum(by_chain.values())}