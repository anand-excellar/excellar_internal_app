"""On-chain ERC-4626 vault position reader.

Reads a wallet's position in an ERC-4626 vault — a MetaMorpho vault such as
Gauntlet USDC Prime, for example — by querying its share balance with
``balanceOf(address)`` and converting those shares to the underlying asset with
``convertToAssets(uint256)`` at the live redemption rate.

This is the same shape as the Spark reader: the vault share token sitting in the
wallet **is** the position, so there is no pool to query the way the Aave reader
does — only the share balance and its conversion rate.

Share decimals deliberately do not appear anywhere here. ``convertToAssets``
takes *raw* shares and returns *raw* units of the underlying, so only the
underlying asset's decimals are needed to reach a human number. A vault whose
shares are 18-decimal over 6-decimal USDC therefore needs no special handling,
which removes the likeliest way to misconfigure a vault into a wrong NAV.

Like the Aave and Spark readers this uses raw JSON-RPC ``eth_call`` (no web3.py)
to keep the dependency surface at zero. The 4-byte selectors are hard-coded
constants verified in tests/test_vaults.py against keccak256.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# keccak256("balanceOf(address)")[:4] — verified in tests/test_vaults.py
BALANCE_OF_SELECTOR = "0x70a08231"
# keccak256("convertToAssets(uint256)")[:4] — verified in tests/test_vaults.py
CONVERT_TO_ASSETS_SELECTOR = "0x07a2d13a"


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
    response = requests.post(rpc_url, json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"eth_call failed: {body['error']}")
    raw = body.get("result") or "0x"
    if raw in ("", "0x"):
        return 0
    return int(raw, 16)


def fetch_vault_shares(rpc_url: str, vault: str, address: str, timeout: int = 30) -> int:
    """Raw (undivided) share balance an address holds in the vault."""
    data = _encode_address_arg(BALANCE_OF_SELECTOR, address)
    return _eth_call(rpc_url, vault, data, timeout=timeout)


def convert_to_assets(rpc_url: str, vault: str, shares: int, timeout: int = 30) -> int:
    """Raw underlying-asset value of ``shares``, at the vault's live rate."""
    if shares <= 0:
        return 0
    data = _encode_uint_arg(CONVERT_TO_ASSETS_SELECTOR, shares)
    return _eth_call(rpc_url, vault, data, timeout=timeout)


def fetch_vaults_for_addresses(
    vaults: list[dict], addresses: list[str], price_by_symbol: Optional[dict] = None,
) -> Optional[dict]:
    """Aggregate ERC-4626 vault positions in USD across addresses.

    Args:
        vaults: list of ``{"name", "symbol", "chain", "vault", "rpc_url",
            "asset_symbol", "asset_decimals"}``.
        addresses: wallet addresses to query.
        price_by_symbol: SYMBOL -> USD price for the underlying asset. A missing
            price on a stablecoin underlying defaults to 1.0; any other missing
            price leaves that vault unpriced rather than guessing.

    Returns:
        ``{"positions": [...], "total_usd": float}`` summed across every
        address, or None if no vault could be read at all. A vault that reads OK
        but holds nothing contributes 0.0 — so a withdrawn position is stored as
        0, not left None (which the history back-fill would otherwise resurrect
        from a stale snapshot).
    """
    if not vaults or not addresses:
        return None

    price_by_symbol = price_by_symbol or {}
    _STABLE = {"USDC", "USDT", "DAI", "USDS", "USDC.E"}

    positions: list[dict] = []
    any_ok = False
    for vault_cfg in vaults:
        rpc_url = vault_cfg.get("rpc_url")
        vault = vault_cfg.get("vault")
        if not rpc_url or not vault:
            continue

        decimals = int(vault_cfg.get("asset_decimals") or 0)
        if decimals <= 0:
            logger.error("Vault %s has no asset_decimals — skipping rather than "
                         "reporting a wrong size", vault_cfg.get("name") or vault)
            continue

        total_shares = 0
        vault_ok = False
        for address in addresses:
            try:
                total_shares += fetch_vault_shares(rpc_url, vault, address)
                vault_ok = True
            except Exception as exc:
                logger.warning("Vault %s balanceOf(%s) failed: %s",
                               vault_cfg.get("name") or vault, address, exc)

        if not vault_ok:
            continue

        try:
            raw_assets = convert_to_assets(rpc_url, vault, total_shares)
        except Exception as exc:
            logger.warning("Vault %s convertToAssets failed: %s",
                           vault_cfg.get("name") or vault, exc)
            continue

        any_ok = True
        amount = raw_assets / (10 ** decimals)
        asset_symbol = (vault_cfg.get("asset_symbol") or "").upper()
        price = price_by_symbol.get(asset_symbol)
        if price is None and asset_symbol in _STABLE:
            price = 1.0

        positions.append({
            "name": vault_cfg.get("name") or vault,
            "symbol": vault_cfg.get("symbol") or "",
            "chain": vault_cfg.get("chain", ""),
            "vault": vault,
            "shares": total_shares / (10 ** int(vault_cfg.get("share_decimals") or 18)),
            "asset_symbol": asset_symbol,
            "amount": amount,
            "price": price,
            "usd": (amount * price) if price is not None else None,
        })

    if not any_ok:
        return None

    return {
        "positions": positions,
        "total_usd": sum(p["usd"] for p in positions if p["usd"] is not None),
    }
