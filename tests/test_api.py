import pytest
from datetime import datetime, timezone, date
from rest_framework.test import APIClient
from tracker.models import Snapshot, DailyPnl, CrossMtmConfig


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_snapshots_list_empty(client):
    res = client.get("/api/nav/snapshots/")
    assert res.status_code == 200
    assert res.json()["results"] == []


@pytest.mark.django_db
def test_snapshots_list_returns_data(client):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        btc_price=84000.0,
        pnl_usdc=10.5,
    )
    res = client.get("/api/nav/snapshots/")
    assert res.status_code == 200
    assert len(res.json()["results"]) == 1
    assert res.json()["results"][0]["segment"] == "xlBTC"


@pytest.mark.django_db
def test_snapshots_list_excludes_raw_json(client):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        raw_debank_json='{"large": "blob"}',
    )
    res = client.get("/api/nav/snapshots/")
    assert "raw_debank_json" not in res.json()["results"][0]


@pytest.mark.django_db
def test_snapshot_detail_includes_raw_json(client):
    snap = Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        raw_debank_json='{"key": "val"}',
    )
    res = client.get(f"/api/nav/snapshots/{snap.pk}/")
    assert res.status_code == 200
    assert res.json()["raw_debank_json"] == '{"key": "val"}'


@pytest.mark.django_db
def test_snapshots_latest(client):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        btc_price=84000.0,
    )
    res = client.get("/api/nav/snapshots/latest/")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["segment"] == "xlBTC"


@pytest.mark.django_db
def test_snapshots_filter_by_segment(client):
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc), segment="xlBTC"
    )
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc), segment="xlETH"
    )
    res = client.get("/api/nav/snapshots/?segment=xlBTC")
    results = res.json()["results"]
    assert all(r["segment"] == "xlBTC" for r in results)


@pytest.mark.django_db
def test_daily_pnl_list(client):
    DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlBTC", pnl_usdc=10.0)
    res = client.get("/api/nav/daily-pnl/")
    assert res.status_code == 200
    assert len(res.json()["results"]) == 1


@pytest.mark.django_db
def test_daily_pnl_filter_by_segment(client):
    DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlBTC", pnl_usdc=10.0)
    DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlETH", pnl_usdc=5.0)
    res = client.get("/api/nav/daily-pnl/?segment=xlBTC")
    results = res.json()["results"]
    assert all(r["segment"] == "xlBTC" for r in results)


@pytest.mark.django_db
def test_daily_pnl_detail(client):
    DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlBTC", pnl_usdc=10.0)
    res = client.get("/api/nav/daily-pnl/xlBTC/2026-04-14/")
    assert res.status_code == 200
    assert res.json()["pnl_usdc"] == 10.0


@pytest.mark.django_db
def test_daily_pnl_detail_404(client):
    res = client.get("/api/nav/daily-pnl/xlBTC/2026-01-01/")
    assert res.status_code == 404


@pytest.mark.django_db
def test_portfolio_summary(client):
    DailyPnl.objects.create(
        date=date(2026, 4, 14), segment="xlBTC",
        portfolio_value=0.033, rolling_max=0.040,
        drawdown=-0.175, annualized_return=1.095,
    )
    res = client.get("/api/nav/portfolio/summary/")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["segment"] == "xlBTC"
    assert data[0]["portfolio_value"] == pytest.approx(0.033)


@pytest.mark.django_db
def test_cross_mtm_config_list(client):
    from django.utils import timezone as tz
    CrossMtmConfig.objects.create(
        asset="BTC", entry_cost=100.0, position_size=0.5, effective_from=tz.now()
    )
    res = client.get("/api/nav/cross-mtm-config/")
    assert res.status_code == 200
    assert len(res.json()["results"]) == 1


@pytest.mark.django_db
def test_daily_pnl_export_csv(client):
    DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlBTC", pnl_usdc=10.0)
    res = client.get("/api/nav/daily-pnl/export/")
    assert res.status_code == 200
    assert "text/csv" in res["Content-Type"]
    lines = res.content.decode().strip().split("\n")
    assert len(lines) == 2  # header + 1 data row


from unittest.mock import patch


@pytest.mark.django_db
def test_trigger_snapshot_enqueued(client):
    with patch("tracker.tasks.run_snapshot_cycle"):
        res = client.post("/api/nav/actions/snapshot/")
    assert res.status_code == 200
    assert res.json()["status"] == "snapshot cycle enqueued"


@pytest.mark.django_db
def test_trigger_finalize_with_date(client):
    with patch("tracker.services.collector.finalize_daily_for_date"):
        res = client.post("/api/nav/actions/finalize/", {"date": "2026-04-14"}, format="json")
    assert res.status_code == 200
    assert res.json()["status"] == "finalization triggered"


@pytest.mark.django_db
def test_trigger_finalize_invalid_date(client):
    res = client.post("/api/nav/actions/finalize/", {"date": "not-a-date"}, format="json")
    assert res.status_code == 400
    assert "error" in res.json()
