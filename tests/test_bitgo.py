import json
import pytest
from datetime import datetime, timezone

from django.test import Client as DjangoClient

from tracker.scrapers import bitgo
from tracker.models import Snapshot


# --------------------------- pure-function tests ---------------------------

def test_normalize_symbol_aliases_and_passthrough():
    assert bitgo.normalize_symbol("btc") == "BTC"
    assert bitgo.normalize_symbol("hteth") == "ETH"
    assert bitgo.normalize_symbol("polygon") == "MATIC"
    assert bitgo.normalize_symbol("usdc") == "USDC"
    assert bitgo.normalize_symbol(None) == ""


def test_to_amount_uses_exact_decimal_scaling():
    # 1 ETH in wei -> exactly 1.0, no float drift
    assert bitgo._to_amount("1000000000000000000", "ETH") == 1.0
    # 1 BTC in sats
    assert bitgo._to_amount("100000000", "BTC") == 1.0
    # 50 USDC (6 decimals)
    assert bitgo._to_amount("50000000", "USDC") == 50.0
    # garbage -> None, not a crash
    assert bitgo._to_amount("not-a-number", "ETH") is None
    assert bitgo._to_amount(None, "ETH") is None


def test_aggregate_holdings_sums_across_wallets_and_tokens():
    wallets = [
        {"coin": "eth", "balanceString": "1000000000000000000"},   # 1 ETH
        {"coin": "eth", "balanceString": "880000000000000000",     # 0.88 ETH
         "tokens": {"usdc": {"coin": "usdc", "balanceString": "100000000"}}},  # 100 USDC
        {"coin": "btc", "balanceString": "50000000"},              # 0.5 BTC
    ]
    prices = {"ETH": 2000.0, "BTC": 70000.0}
    agg = bitgo.aggregate_holdings(wallets, prices)

    by = agg["by_symbol"]
    assert by["ETH"]["amount"] == pytest.approx(1.88)
    assert by["ETH"]["usd"] == pytest.approx(3760.0)
    assert by["BTC"]["amount"] == pytest.approx(0.5)
    assert by["BTC"]["usd"] == pytest.approx(35000.0)
    # USDC valued at $1 even without an explicit price
    assert by["USDC"]["amount"] == pytest.approx(100.0)
    assert by["USDC"]["usd"] == pytest.approx(100.0)

    # total = 3760 + 35000 + 100
    assert agg["total_usd"] == pytest.approx(38860.0)
    # holdings sorted by usd desc -> BTC first
    assert agg["holdings"][0]["symbol"] == "BTC"


def test_aggregate_holdings_handles_unknown_price_as_none_usd():
    wallets = [{"coin": "sol", "balanceString": "2000000000"}]  # 2 SOL, 9 decimals
    agg = bitgo.aggregate_holdings(wallets, price_by_symbol={})
    assert agg["by_symbol"]["SOL"]["amount"] == pytest.approx(2.0)
    assert agg["by_symbol"]["SOL"]["usd"] is None
    # unknown-price holding excluded from total
    assert agg["total_usd"] == pytest.approx(0.0)


def test_fetch_all_wallets_requires_token():
    with pytest.raises(ValueError):
        bitgo.fetch_all_wallets(access_token="")


# --------------------------- view test ---------------------------

@pytest.fixture
def client():
    return DjangoClient()


@pytest.mark.django_db
def test_bitgo_holdings_partial_no_data(client):
    res = client.get("/d/nav/partials/bitgo-holdings/")
    assert res.status_code == 200
    assert b"No BitGo holdings yet" in res.content


@pytest.mark.django_db
def test_bitgo_holdings_partial_renders_aggregate(client):
    agg = {
        "holdings": [
            {"symbol": "BTC", "amount": 0.5, "price": 70000.0, "usd": 35000.0},
            {"symbol": "USDC", "amount": 100.0, "price": 1.0, "usd": 100.0},
        ],
        "total_usd": 35100.0,
    }
    Snapshot.objects.create(
        timestamp=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        bitgo_holdings_json=json.dumps(agg),
    )
    res = client.get("/d/nav/partials/bitgo-holdings/")
    body = res.content.decode()
    assert res.status_code == 200
    assert "BTC" in body
    assert "35,000.00" in body
    assert "Total custody value" in body
    assert "35,100.00" in body
