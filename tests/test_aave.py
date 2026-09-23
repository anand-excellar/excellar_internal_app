"""Tests for the on-chain Aave v3 reader.

The hard-coded function selector is the only piece we can't see at a glance, so
it is re-derived here from keccak256 and asserted, guarding against drift.
"""
import pytest

from tracker.scrapers import aave


def test_selector_matches_keccak():
    # Recompute keccak256("getUserAccountData(address)")[:4] independently.
    from ccxt.static_dependencies.keccak.keccak import SHA3
    expected = "0x" + SHA3(b"getUserAccountData(address)")[:4].hex()
    assert aave.GET_USER_ACCOUNT_DATA_SELECTOR == expected


def test_encode_calldata_pads_address():
    addr = "0x48c597e9c0a8ea1e5a5b7199e264eab2fefd7e54"
    data = aave._encode_get_user_account_data(addr)
    assert data.startswith("0xbf92857c")
    # selector (8 hex) + 64-hex padded address
    assert len(data) == 2 + 8 + 64
    assert data.endswith("48c597e9c0a8ea1e5a5b7199e264eab2fefd7e54")


def test_encode_rejects_bad_address():
    with pytest.raises(ValueError):
        aave._encode_get_user_account_data("0x1234")


def _word(n: int) -> str:
    return f"{n:064x}"


def test_decode_account_data_scales_units():
    # collateral=241.85 USD (8dp), debt=100 USD, avail=50, liqThr=8250bps,
    # ltv=8000bps, HF=2.4e18
    result = "0x" + "".join([
        _word(24185000000),         # 241.85 * 1e8
        _word(10000000000),         # 100.00 * 1e8
        _word(5000000000),          # 50.00 * 1e8
        _word(8250),                # 82.5%
        _word(8000),                # 80%
        _word(24 * 10**17),         # 2.4 * 1e18
    ])
    d = aave._decode_account_data(result)
    assert d["collateral_usd"] == pytest.approx(241.85)
    assert d["debt_usd"] == pytest.approx(100.0)
    assert d["available_borrows_usd"] == pytest.approx(50.0)
    assert d["liquidation_threshold"] == pytest.approx(0.825)
    assert d["ltv"] == pytest.approx(0.80)
    assert d["health_factor"] == pytest.approx(2.4)


def test_decode_no_debt_yields_none_health_factor():
    result = "0x" + "".join([
        _word(10000000000),   # collateral 100 USD
        _word(0),             # no debt
        _word(8000000000),
        _word(8250),
        _word(8000),
        _word(aave._UINT256_MAX),  # HF = max uint when no debt
    ])
    d = aave._decode_account_data(result)
    assert d["debt_usd"] == 0.0
    assert d["health_factor"] is None


def test_fetch_account_data_uses_rpc(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"result": "0x" + "".join(_word(x) for x in
                    [10000000000, 5000000000, 0, 8250, 8000, 2 * 10**18])}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["data"] = json["params"][0]["data"]
        return FakeResp()

    monkeypatch.setattr(aave.requests, "post", fake_post)
    d = aave.fetch_aave_account_data(
        "https://rpc.example", "0xPool", "0x48c597e9c0a8ea1e5a5b7199e264eab2fefd7e54")
    assert captured["url"] == "https://rpc.example"
    assert captured["data"].startswith("0xbf92857c")
    assert d["collateral_usd"] == pytest.approx(100.0)
    assert d["health_factor"] == pytest.approx(2.0)


def test_fetch_for_wallet_aggregates_and_takes_min_hf(monkeypatch):
    calls = []

    def fake_account_data(rpc_url, pool, user, timeout=30):
        calls.append(rpc_url)
        if "eth" in rpc_url:
            return {"collateral_usd": 200.0, "debt_usd": 100.0, "available_borrows_usd": 50.0,
                    "liquidation_threshold": 0.8, "health_factor": 1.6}
        return {"collateral_usd": 50.0, "debt_usd": 40.0, "available_borrows_usd": 5.0,
                "liquidation_threshold": 0.7, "health_factor": 1.2}

    monkeypatch.setattr(aave, "fetch_aave_account_data", fake_account_data)
    markets = [
        {"chain": "ethereum", "rpc_url": "https://eth.rpc", "pool": "0xA"},
        {"chain": "arbitrum", "rpc_url": "https://arb.rpc", "pool": "0xB"},
    ]
    agg = aave.fetch_aave_for_wallet(markets, "0xabc")
    assert agg["collateral_usd"] == pytest.approx(250.0)
    assert agg["debt_usd"] == pytest.approx(140.0)
    assert agg["health_factor"] == pytest.approx(1.2)  # min across markets


def test_fetch_for_wallet_none_when_no_markets():
    assert aave.fetch_aave_for_wallet([], "0xabc") is None
    assert aave.fetch_aave_for_wallet([{"rpc_url": "x", "pool": "y"}], "") is None
