"""Mapping and normalisation, exercised against the real XLBTC payload."""
import json
import os

import pytest

from navfund import mapping, normalize

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "xlbtc_trading_gain_loss_2026-08-09.json")

# NAV's own figure for XLBTC on the fixture date (GetBalanceSheetForFund →
# Ending Net Asset Value), and the total of the 18 rows in the fixture.
BALANCE_SHEET_NAV = 0.0528104
FIXTURE_TOTAL = 0.05282474


@pytest.fixture(scope="module")
def nav_rows():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def normalized(nav_rows):
    return normalize.normalize_rows(nav_rows)


# --------------------------------------------------------------------------- #
# Mapping file integrity
# --------------------------------------------------------------------------- #
def test_map_loads_and_validates():
    data = mapping.load_map()
    assert set(data["segments"]) == {"xlBTC", "xlETH", "xlUSD"}
    assert "Native reserve" in data["categories"]


def test_fund_ids_match_the_tenant():
    assert mapping.fund_ids() == {"xlBTC": 247833, "xlETH": 247834, "xlUSD": 247832}


def test_segment_lookup_from_nav_fund_name():
    assert mapping.segment_for_fund("XLBTC SA") == "xlBTC"
    assert mapping.segment_for_fund("xlbtc sa") == "xlBTC"      # case-insensitive
    assert mapping.segment_for_fund("USDXLR SA") is None        # out of scope


def test_account_lookup_ignores_case_and_spacing():
    """NAV's casing differs between endpoints and reports for the same account."""
    canonical = mapping.resolve("XLBTC Hot Wallet on Arbitrum")
    assert canonical["location"] == "xlBTC Hot Wallet Arbitrum · arbeth"
    assert mapping.resolve("XLBTC HOT WALLET ON ARBITRUM") == canonical
    assert mapping.resolve("  xlbtc   hot  wallet on arbitrum ") == canonical


def test_unmapped_account_returns_none_with_a_recorded_reason():
    assert mapping.resolve("EX BTC_XLBTC_BITGO") is None
    assert "double-count" in mapping.unmapped_note("EX BTC_XLBTC_BITGO")


# --------------------------------------------------------------------------- #
# Ticker aliasing — the gas-account rename
# --------------------------------------------------------------------------- #
def test_gas_ticker_becomes_the_wallets_native_coin():
    on_arbitrum = mapping.resolve("XLBTC HOT WALLET ON ARBITRUM ETH GAS FEE", "ETH_GAS")
    on_mainnet = mapping.resolve("XLBTC HOT WALLET ON ETHEREUM ETH GAS FEE", "ETH_GAS")
    assert on_arbitrum["our_asset"] == "ARBETH"
    assert on_mainnet["our_asset"] == "ETH"
    # ...and both land on the wallet they belong to, not a gas pseudo-wallet.
    assert on_arbitrum["location"] == "xlBTC Hot Wallet Arbitrum · arbeth"
    assert on_mainnet["location"] == "xlBTC Hot Wallet · eth"


def test_atoken_keeps_our_prefixed_name():
    resolved = mapping.resolve("XLBTC Hot Wallet on Ethereum", "AETHWBTC")
    assert resolved["our_asset"] == "ETH:AETHWBTC"
    assert resolved["category"] == "Native reserve"  # not rerouted to Aave


def test_unaliased_ticker_passes_through():
    assert mapping.resolve("XLBTC Hot Wallet on Arbitrum", "USDC")["our_asset"] == "USDC"


def test_ticker_override_moves_spark_out_of_the_wallet():
    """NAV keeps sUSDS inside the Arbitrum wallet; we file it under Spark Savings."""
    wallet = mapping.resolve("XLUSD Hot Wallet on Arbitrum", "USDC")
    spark = mapping.resolve("XLUSD Hot Wallet on Arbitrum", "SUSDS")
    assert wallet["category"] == "Native reserve"
    assert wallet["location"] == "xlUSD Hot Wallet Arbitrum · arbeth"
    assert spark["category"] == "Spark Savings"
    assert spark["location"] == "Arbitrum"
    assert spark["our_asset"] == "sUSDS"


def test_categories_match_the_ones_our_report_emits():
    """Guard against drift: these strings are read off a generated Detail tab."""
    assert mapping.load_map()["categories"] == {
        "Native reserve", "Aave position", "Spark Savings",
        "Exchange funds & perp exposure"}


# --------------------------------------------------------------------------- #
# Normalisation over the real payload
# --------------------------------------------------------------------------- #
def test_every_row_is_either_mapped_or_reported(nav_rows, normalized):
    assert len(normalized["rows"]) + len(normalized["unmapped"]) == len(nav_rows)


def test_only_the_known_dust_account_is_unmapped(normalized):
    assert [r["nav_account"] for r in normalized["unmapped"]] == ["EX BTC_XLBTC_BITGO"]


def test_mapped_total_ties_to_navs_own_nav(normalized):
    """The mapping must not lose value: mapped + unmapped == the fixture total."""
    mapped = sum(r["value_base"] for r in normalized["rows"])
    unmapped = sum(r["value_base"] for r in normalized["unmapped"])
    assert mapped + unmapped == pytest.approx(FIXTURE_TOTAL, abs=1e-9)
    # And that total is NAV's own NAV for the date, to within a rounding hair.
    assert mapped + unmapped == pytest.approx(BALANCE_SHEET_NAV, abs=2e-5)


def test_rollup_collapses_gas_accounts_into_one_label(normalized):
    rolled = normalize.rollup_by_location(normalized["rows"])
    arbitrum = rolled[("xlBTC", "Native reserve", "xlBTC Hot Wallet Arbitrum · arbeth")]
    # The wallet's USDC plus its separate ETH GAS FEE account, as one line.
    assert sorted(arbitrum["nav_accounts"]) == [
        "XLBTC HOT WALLET ON ARBITRUM ETH GAS FEE", "XLBTC Hot Wallet on Arbitrum"]
    assert arbitrum["value_base"] == pytest.approx(0.00076898 + 0.00088401, abs=1e-8)


def test_rollup_groups_exchange_venues(normalized):
    rolled = normalize.rollup_by_location(normalized["rows"])
    binance = rolled[("xlBTC", "Exchange funds & perp exposure", "Binance")]
    assert sorted(binance["nav_accounts"]) == ["XLBTC_BINANCE SUB", "XLBTC_BINANCE SUB_GAS"]
    deribit = rolled[("xlBTC", "Exchange funds & perp exposure", "Deribit")]
    assert sorted(deribit["nav_accounts"]) == ["XLBTC_DERIBIT", "XLBTC_DERIBIT_gas"]


def test_per_asset_rollup_keys_on_our_asset_names(normalized):
    by_asset = normalize.rollup_by_asset(normalized["rows"])
    # Quantities that must tie against our Qty column, per the recon spec.
    assert by_asset[("xlBTC", "xlBTC Hot Wallet Arbitrum · arbeth", "USDC")]["qty"] \
        == pytest.approx(50.042206)
    assert by_asset[("xlBTC", "xlBTC Hot Wallet Arbitrum · arbeth", "ARBETH")]["qty"] \
        == pytest.approx(0.029975)
    assert by_asset[("xlBTC", "xlBTC Hot Wallet · eth", "ETH")]["qty"] \
        == pytest.approx(0.044576)
    assert by_asset[("xlBTC", "xlBTC Hot Wallet · eth", "ETH:AETHWBTC")]["qty"] \
        == pytest.approx(0.003991)


def test_cold_btc_rollup_sums_wallet_and_reward(normalized):
    by_asset = normalize.rollup_by_asset(normalized["rows"])
    # BTC_GAS on the reward account is renamed to BTC, so it adds to the wallet's
    # own BTC rather than appearing as a separate asset.
    cold = by_asset[("xlBTC", "xlBTC Reserves - BTC · btc", "BTC")]
    assert cold["qty"] == pytest.approx(0.04473766 + 0.00054455)
    assert len(cold["nav_accounts"]) == 2


def test_segment_totals(normalized):
    totals = normalize.segment_totals(normalized["rows"])
    assert set(totals) == {"xlBTC"}
    assert totals["xlBTC"] == pytest.approx(FIXTURE_TOTAL - 0.00000001, abs=1e-8)


def test_empty_input_is_handled():
    assert normalize.normalize_rows([]) == {"rows": [], "unmapped": []}
    assert normalize.normalize_rows(None) == {"rows": [], "unmapped": []}