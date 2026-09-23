import pytest
from datetime import datetime, timezone, date, timedelta
from django.test import Client as DjangoClient
from tracker.models import Snapshot, DailyPnl
import json


@pytest.fixture
def client():
    return DjangoClient()


@pytest.mark.django_db
def test_dashboard_index_loads(client):
    res = client.get("/d/nav/")
    assert res.status_code == 200
    assert b"navdash" in res.content


@pytest.mark.django_db
def test_dashboard_index_contains_segment_names(client, xleth_disabled):
    res = client.get("/d/nav/")
    assert b"xlBTC" in res.content
    assert b"xlUSD" in res.content
    assert b"xlETH" not in res.content


@pytest.mark.django_db
def test_partial_snapshots_no_data(client):
    res = client.get("/d/nav/partials/snapshots/")
    assert res.status_code == 200
    assert b"No data" in res.content


@pytest.mark.django_db
def test_partial_snapshots_with_data(client):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        btc_price=84000.0,
        pnl_usdc=10.5,
    )
    res = client.get("/d/nav/partials/snapshots/")
    assert res.status_code == 200
    assert b"xlBTC" in res.content


@pytest.mark.django_db
def test_partial_snapshots_excludes_disabled_segment_data(client, xleth_disabled):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
        eth_price=3200.0,
        pnl_usdc=7.5,
    )
    res = client.get("/d/nav/partials/snapshots/")
    assert res.status_code == 200
    assert b"xlETH" not in res.content
    assert b"7.5000" not in res.content


@pytest.mark.django_db
def test_pnl_debug_excludes_disabled_segment_data(client, xleth_disabled):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
        pnl_usdc=7.5,
    )
    res = client.get("/d/nav/partials/pnl-debug/")
    assert res.status_code == 200
    assert b"xlETH" not in res.content
    assert b"1234.0000" not in res.content


@pytest.mark.django_db
def test_partial_portfolio_no_data(client):
    res = client.get("/d/nav/partials/portfolio/")
    assert res.status_code == 200


@pytest.mark.django_db
def test_partial_portfolio_with_data(client):
    DailyPnl.objects.create(
        date=date(2026, 4, 14), segment="xlBTC",
        portfolio_value=0.033, rolling_max=0.040, drawdown=-0.175,
    )
    res = client.get("/d/nav/partials/portfolio/")
    assert res.status_code == 200
    assert b"xlBTC" in res.content


@pytest.mark.django_db
def test_partial_portfolio_excludes_disabled_segment_data(client, xleth_disabled):
    DailyPnl.objects.create(
        date=date(2026, 4, 14), segment="xlETH",
        portfolio_value=1.234, rolling_max=1.500, drawdown=-0.1,
    )
    res = client.get("/d/nav/partials/portfolio/")
    assert res.status_code == 200
    assert b"xlETH" not in res.content
    assert b"1.234" not in res.content


@pytest.mark.django_db
def test_chart_data_returns_json(client):
    row_date = date.today() - timedelta(days=1)
    DailyPnl.objects.create(
        date=row_date, segment="xlBTC",
        pnl_usdc=10.0, portfolio_value=0.033,
    )
    res = client.get("/d/nav/chart-data/?segment=xlBTC&days=30")
    assert res.status_code == 200
    data = json.loads(res.content)
    assert "labels" in data
    assert "pnl_usdc" in data
    assert len(data["labels"]) == 1
    assert data["labels"][0] == str(row_date)
    assert data["pnl_usdc"][0] == 10.0


@pytest.mark.django_db
def test_chart_data_default_segment(client):
    res = client.get("/d/nav/chart-data/")
    assert res.status_code == 200
    data = json.loads(res.content)
    assert "labels" in data


@pytest.mark.django_db
def test_chart_data_rejects_disabled_segment(client, xleth_disabled):
    res = client.get("/d/nav/chart-data/?segment=xlETH&days=30")
    assert res.status_code == 400
