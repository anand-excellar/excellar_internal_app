"""Tests for the on-chain Spark Savings (sUSDS) reader.

The hard-coded 4-byte selectors are the only pieces not obvious at a glance, so
they are re-derived here from keccak256 and asserted, guarding against drift.
"""
import pytest

from tracker.scrapers import spark


def test_selectors_match_keccak():
    from ccxt.static_dependencies.keccak.keccak import SHA3
    assert spark.BALANCE_OF_SELECTOR == "0x" + SHA3(b"balanceOf(address)")[:4].hex()
    assert spark.CONVERT_TO_ASSETS_SELECTOR == (
        "0x" + SHA3(b"convertToAssets(uint256)")[:4].hex())


def test_encode_address_arg_pads():
    addr = "0x07c1335e5dceecf9594a83c27bdcb977172863f2"
    data = spark._encode_address_arg(spark.BALANCE_OF_SELECTOR, addr)
    assert data.startswith("0x70a08231")
    assert len(data) == 2 + 8 + 64          # 0x + selector + 32-byte word
    assert data.endswith("07c1335e5dceecf9594a83c27bdcb977172863f2")


def test_encode_address_rejects_bad_address():
    with pytest.raises(ValueError):
        spark._encode_address_arg(spark.BALANCE_OF_SELECTOR, "0x1234")


def test_encode_uint_arg():
    data = spark._encode_uint_arg(spark.CONVERT_TO_ASSETS_SELECTOR, 10 ** 18)
    assert data.startswith("0x07a2d13a")
    assert data.endswith(f"{10 ** 18:064x}")


def test_fetch_rate_reads_convert_to_assets(monkeypatch):
    # convertToAssets(1e18) -> 1.1e18  => rate 1.10
    monkeypatch.setattr(spark, "_eth_call", lambda *a, **k: 11 * 10 ** 17)
    rate = spark.fetch_susds_rate(
        {"chain": "ethereum", "token": "0xToken", "rpc_url": "https://rpc"})
    assert rate == pytest.approx(1.10)


def test_fetch_rate_falls_back_to_one():
    assert spark.fetch_susds_rate(None) == 1.0
    assert spark.fetch_susds_rate({"chain": "x"}) == 1.0  # missing rpc/token


def test_fetch_rate_falls_back_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(spark, "_eth_call", boom)
    rate = spark.fetch_susds_rate(
        {"chain": "ethereum", "token": "0xToken", "rpc_url": "https://rpc"})
    assert rate == 1.0  # conservative floor, never over-states


def test_fetch_for_addresses_aggregates_and_values(monkeypatch):
    # Two wallets, 100 + 50 sUSDS shares on arbitrum; rate 1.10 -> $165.
    def fake_balance(rpc_url, token, address, timeout=30):
        return {"0xaaa": 100.0, "0xbbb": 50.0}[address]

    monkeypatch.setattr(spark, "fetch_susds_balance", fake_balance)
    markets = [{"chain": "arbitrum", "token": "0xT", "rpc_url": "https://arb"}]
    out = spark.fetch_susds_for_addresses(markets, ["0xaaa", "0xbbb"], rate=1.10)
    assert out["by_chain"]["arbitrum"] == pytest.approx(165.0)
    assert out["total_usd"] == pytest.approx(165.0)


def test_fetch_for_addresses_empty_chain_is_zero_not_none(monkeypatch):
    # A chain that reads OK but holds nothing yields 0.0 (so a withdrawn
    # position is stored as 0, not left None for the history back-fill).
    monkeypatch.setattr(spark, "fetch_susds_balance", lambda *a, **k: 0.0)
    markets = [{"chain": "arbitrum", "token": "0xT", "rpc_url": "https://arb"}]
    out = spark.fetch_susds_for_addresses(markets, ["0xaaa"], rate=1.10)
    assert out["by_chain"]["arbitrum"] == 0.0
    assert out["total_usd"] == 0.0


def test_fetch_for_addresses_none_when_all_reads_fail(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(spark, "fetch_susds_balance", boom)
    markets = [{"chain": "arbitrum", "token": "0xT", "rpc_url": "https://arb"}]
    assert spark.fetch_susds_for_addresses(markets, ["0xaaa"], rate=1.10) is None


def test_fetch_for_addresses_guards_empty_inputs():
    assert spark.fetch_susds_for_addresses([], ["0xaaa"]) is None
    assert spark.fetch_susds_for_addresses(
        [{"chain": "arbitrum", "token": "0xT", "rpc_url": "x"}], []) is None