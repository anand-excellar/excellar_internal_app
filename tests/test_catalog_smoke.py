import json
import pytest
from datetime import datetime, timezone
from django.test import Client as DjangoClient
from tracker.models import Snapshot


@pytest.fixture
def client():
    return DjangoClient()


@pytest.mark.django_db
def test_catalog_no_data(client):
    res = client.get("/d/nav/partials/catalog/")
    assert res.status_code == 200
    assert b"No data" in res.content


@pytest.mark.django_db
def test_catalog_renders_full_eth_breakdown(client):
    Snapshot.objects.create(
        timestamp=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
        eth_price=2015.39,
        wallet_native_amount=2.88,
        aave_supply_amount=0.12,
        aave_supply_usd=241.85,
        aave_borrow_usdc=100.0,
        aave_health_rate=2.4,
        hl_deposit_usdc=50.0,
        binance_collateral_usdc=50.0,
        perp_positions_json=json.dumps([
            {"venue": "Binance", "symbol": "BTC/USDC:USDC", "side": "long",
             "size": 0.001, "entry_price": 73650, "mark_price": 73652,
             "unrealized_pnl": 0.5},
            {"venue": "Hyperliquid", "symbol": "BTC", "side": "short",
             "size": 0.001, "entry_price": 73658, "mark_price": None,
             "unrealized_pnl": -0.4},
        ]),
    )
    res = client.get("/d/nav/partials/catalog/")
    body = res.content.decode()
    assert res.status_code == 200
    # Section headers
    assert "Native reserve" in body
    assert "Aave position" in body
    assert "Exchange funds &amp; perp exposure" in body
    # Rows
    assert "ETH (collateral)" in body
    assert "USDC (borrowed)" in body
    # Perp legs: venue on the sub-label, then "<side> <size>". The view stopped
    # rendering a signed size ("+0.001") in favour of the explicit side word,
    # which is what had left this assertion stale.
    assert "Binance" in body
    assert "Hyperliquid" in body
    assert "BTC perp" in body
    assert "long 0.001" in body
    assert "short 0.001" in body
    # LTV computed from borrow/collateral_usd = 100/241.85 = 41.3%
    assert "LTV 41" in body
    # Net position footer present. The heading was reworded to lead with NAV;
    # "Net position (after Aave loan)" now survives only as a code comment.
    assert "NAV (native)" in body
    assert "after Aave loan" in body


@pytest.mark.django_db
def test_carried_forward_values_are_flagged_as_stale(client):
    """A back-filled figure must be labelled, not presented as current.

    When a source fails to collect, the catalog fills the gap from an older
    snapshot. That is the right behaviour — a blank card would be worse — but
    unlabelled it reads as a real, smaller NAV, which is exactly how a broken
    BitGo credential went unnoticed.
    """
    Snapshot.objects.create(
        timestamp=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
        eth_price=2000.0,
        wallet_native_amount=5.0,
        wallet_balance_usd=10_000.0,
    )
    # The newest snapshot collected prices but no custody balances.
    Snapshot.objects.create(
        timestamp=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
        eth_price=2015.39,
        wallet_native_amount=None,
        wallet_balance_usd=None,
    )

    body = client.get("/d/nav/partials/catalog/").content.decode()
    assert "Stale" in body, "no staleness warning was shown"
    assert "custody (BitGo)" in body, "the stale source was not named"
    assert "20 Jun" in body, "the age of the stale figure was not shown"


@pytest.mark.django_db
def test_fresh_values_are_not_flagged(client):
    """The warning must not cry wolf when everything collected normally."""
    Snapshot.objects.create(
        timestamp=datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
        eth_price=2015.39,
        wallet_native_amount=2.88,
        wallet_balance_usd=5_804.0,
        aave_supply_usd=241.85,
        aave_net_usd=141.85,
    )
    body = client.get("/d/nav/partials/catalog/").content.decode()
    assert "not collected this cycle" not in body
    assert "Stale ·" not in body
