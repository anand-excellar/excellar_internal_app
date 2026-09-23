"""ERC-4626 vault reader: selectors, decimals handling, and NAV inclusion.

The decimals case matters most: Gauntlet USDC Prime has 18-decimal shares over
6-decimal USDC, and getting that wrong misreports NAV by a factor of 10^12
rather than failing loudly.
"""
import pytest

from tracker.scrapers import vaults

# The real vault this was built for, so the fixture can't drift into fiction.
VAULT = "0x8c106eEDaD96553e64287a5a6839c3cc78AFA3D0"
WALLET = "0x7978643d7e4450123bfd1b341e2a379277a4c7a3"

MARKET = {
    "name": "Gauntlet USDC Prime",
    "symbol": "gtusdcp",
    "chain": "ethereum",
    "vault": VAULT,
    "asset_symbol": "USDC",
    "asset_decimals": 6,
    "share_decimals": 18,
    "rpc_url": "https://rpc.example",
}

# 1937.338040643832447325 shares (18dp) worth 2007.297878 USDC (6dp).
SHARES_RAW = 1937338040643832447325
ASSETS_RAW = 2007297878
ASSETS_USD = 2007.297878


def _fetch(monkeypatch, calls, market=None, addresses=None, prices=None):
    it = iter(calls)

    def fake_eth_call(*args, **kwargs):
        value = next(it)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(vaults, "_eth_call", fake_eth_call)
    return vaults.fetch_vaults_for_addresses(
        [market or MARKET], addresses or [WALLET], prices)


def test_selectors_match_keccak():
    from ccxt.static_dependencies.keccak.keccak import SHA3
    assert vaults.BALANCE_OF_SELECTOR == "0x" + SHA3(b"balanceOf(address)")[:4].hex()
    assert vaults.CONVERT_TO_ASSETS_SELECTOR == (
        "0x" + SHA3(b"convertToAssets(uint256)")[:4].hex())


def test_encode_address_arg_pads():
    data = vaults._encode_address_arg(vaults.BALANCE_OF_SELECTOR, WALLET)
    assert data.startswith(vaults.BALANCE_OF_SELECTOR)
    assert data.endswith(WALLET[2:].lower())
    assert len(data) == 10 + 64


def test_encode_address_rejects_bad_address():
    with pytest.raises(ValueError):
        vaults._encode_address_arg(vaults.BALANCE_OF_SELECTOR, "0xdeadbeef")


def test_encode_uint_arg():
    data = vaults._encode_uint_arg(vaults.CONVERT_TO_ASSETS_SELECTOR, SHARES_RAW)
    assert len(data) == 10 + 64
    assert int(data[10:], 16) == SHARES_RAW


def test_values_18dp_shares_over_6dp_usdc(monkeypatch):
    result = _fetch(monkeypatch, [SHARES_RAW, ASSETS_RAW])
    pos = result["positions"][0]
    assert pos["amount"] == pytest.approx(ASSETS_USD)
    assert pos["usd"] == pytest.approx(ASSETS_USD)
    assert pos["shares"] == pytest.approx(1937.3380406438324)
    assert result["total_usd"] == pytest.approx(ASSETS_USD)


def test_sums_shares_across_addresses_before_converting(monkeypatch):
    """One convertToAssets on the summed shares, not per-address rounding."""
    other = "0x4b3855402b21b40b2dda236bd14b7c10b1a0b442"
    result = _fetch(monkeypatch, [SHARES_RAW, SHARES_RAW, ASSETS_RAW * 2],
                    addresses=[WALLET, other])
    assert result["total_usd"] == pytest.approx(2 * ASSETS_USD)


def test_empty_position_is_zero_not_none(monkeypatch):
    """A withdrawn position must persist as 0: None would let the history
    back-fill resurrect a stale balance."""
    result = _fetch(monkeypatch, [0, 0])
    assert result["total_usd"] == 0.0
    assert result["positions"][0]["usd"] == 0.0


def test_none_when_the_vault_cannot_be_read(monkeypatch):
    assert _fetch(monkeypatch, [RuntimeError("rpc down")]) is None


def test_missing_asset_decimals_is_skipped_not_guessed(monkeypatch):
    market = {k: v for k, v in MARKET.items() if k != "asset_decimals"}
    assert _fetch(monkeypatch, [SHARES_RAW, ASSETS_RAW], market=market) is None


def test_guards_empty_inputs():
    assert vaults.fetch_vaults_for_addresses([], [WALLET]) is None
    assert vaults.fetch_vaults_for_addresses([MARKET], []) is None


def test_non_stable_underlying_needs_a_price(monkeypatch):
    market = {**MARKET, "asset_symbol": "WETH"}
    result = _fetch(monkeypatch, [SHARES_RAW, ASSETS_RAW], market=market)
    assert result["positions"][0]["usd"] is None
    assert result["total_usd"] == 0.0

    result = _fetch(monkeypatch, [SHARES_RAW, ASSETS_RAW], market=market,
                    prices={"WETH": 2.0})
    assert result["positions"][0]["usd"] == pytest.approx(2 * ASSETS_USD)


def test_vault_usd_counts_toward_nav():
    from nav.views import compute_nav_usd
    base = {"bitgo_holdings_json": "{}", "wallet_native_amount": None}
    assert compute_nav_usd(base) == 0.0
    assert compute_nav_usd({**base, "vault_usd": ASSETS_USD}) == pytest.approx(ASSETS_USD)


def test_report_components_reconcile_to_nav():
    """The report breakdown must sum to the headline NAV, or the two drift."""
    from nav.views import compute_nav_usd
    from tracker.services.nav_report import compute_nav_components

    merged = {
        "bitgo_holdings_json": "{}",
        "wallet_native_amount": None,
        "susds_arbi_balance": 2009.61,
        "vault_usd": ASSETS_USD,
        "hl_deposit_usdc": 0.39,
        "aave_supply_usd": 10.0,
        "aave_borrow_usdc": 4.0,
    }
    c = compute_nav_components(merged)
    summed = (c["native_reserve_usd"] + c["aave_collateral_usd"] - c["aave_loan_usd"]
              + c["spark_usd"] + c["vault_usd"] + c["exchange_equity_usd"])
    assert summed == pytest.approx(compute_nav_usd(merged))


def test_price_base_aliases_cover_every_variant():
    """A variant that no exchange lists must resolve to a base ticker, or the
    holding is left unpriced and silently dropped from NAV."""
    from tracker.services import collector

    for sym in collector._ETH_PRICED:
        assert collector._PRICE_BASE_ALIASES[sym] == "ETH"
    for sym in collector._BTC_PRICED:
        assert collector._PRICE_BASE_ALIASES[sym] == "BTC"
    assert collector._PRICE_BASE_ALIASES["ARBETH"] == "ETH"
