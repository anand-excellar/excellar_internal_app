import pytest
from datetime import datetime, timezone, date
from django.utils import timezone as tz


@pytest.mark.django_db
def test_snapshot_create():
    from tracker.models import Snapshot
    snap = Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        wallet_address="0x123",
        btc_price=84000.0,
        pnl_usdc=10.5,
        scrape_success=True,
    )
    assert snap.pk is not None
    assert snap.segment == "xlBTC"
    assert snap.btc_price == 84000.0
    assert snap.created_at is not None


@pytest.mark.django_db
def test_snapshot_null_fields_allowed():
    from tracker.models import Snapshot
    snap = Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlETH",
    )
    assert snap.btc_price is None
    assert snap.pnl_usdc is None


@pytest.mark.django_db
def test_daily_pnl_unique_constraint():
    from tracker.models import DailyPnl
    from django.db import IntegrityError
    DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlBTC", pnl_usdc=10.0)
    with pytest.raises(IntegrityError):
        DailyPnl.objects.create(date=date(2026, 4, 14), segment="xlBTC", pnl_usdc=20.0)


@pytest.mark.django_db
def test_daily_pnl_snapshot_fk():
    from tracker.models import Snapshot, DailyPnl
    snap = Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
    )
    daily = DailyPnl.objects.create(
        date=date(2026, 4, 14), segment="xlBTC", snapshot=snap
    )
    assert daily.snapshot_id == snap.pk


@pytest.mark.django_db
def test_cross_mtm_config_active():
    from tracker.models import CrossMtmConfig
    config = CrossMtmConfig.objects.create(
        asset="BTC",
        entry_cost=100.0,
        position_size=0.5,
        effective_from=tz.now(),
    )
    assert config.pk is not None
    assert config.effective_until is None


@pytest.mark.django_db
def test_portfolio_daily_unique_date():
    from tracker.models import PortfolioDaily
    from django.db import IntegrityError
    PortfolioDaily.objects.create(date=date(2026, 4, 14), total_value_usd=10000.0)
    with pytest.raises(IntegrityError):
        PortfolioDaily.objects.create(date=date(2026, 4, 14), total_value_usd=20000.0)
