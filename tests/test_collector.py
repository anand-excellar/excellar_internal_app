import pytest
from datetime import datetime, timezone, date


@pytest.mark.django_db
def test_get_reference_snapshot_returns_latest_before_date():
    from tracker.models import Snapshot
    from tracker.services.collector import _get_reference_snapshot
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 13, 23, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        portfolio_value=0.033,
    )
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 12, 10, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        portfolio_value=0.032,
    )
    ref = _get_reference_snapshot("xlBTC", "2026-04-14")
    assert ref is not None
    assert ref.portfolio_value == pytest.approx(0.033)


@pytest.mark.django_db
def test_get_reference_snapshot_returns_none_when_empty():
    from tracker.services.collector import _get_reference_snapshot
    ref = _get_reference_snapshot("xlBTC", "2026-04-14")
    assert ref is None


@pytest.mark.django_db
def test_get_reference_snapshot_excludes_same_day():
    from tracker.models import Snapshot
    from tracker.services.collector import _get_reference_snapshot
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 14, 8, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        portfolio_value=0.033,
    )
    ref = _get_reference_snapshot("xlBTC", "2026-04-14")
    assert ref is None


@pytest.mark.django_db
def test_finalize_daily_creates_daily_pnl():
    from tracker.models import Snapshot, DailyPnl
    from tracker.services.collector import finalize_daily
    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 13, 23, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        pnl_usdc=10.5,
        pnl_native=0.00012,
        pnl_pct=0.003,
        portfolio_value=0.033,
        annualized_return=1.095,
        funding_cumulative=5.0,
        price_diff_hl_bn=0.5,
        aave_borrow_usdc=2000.0,
        aave_profit=1.0,
        susds_eth_balance=None,
        susds_arbi_balance=None,
        cross_mtm_value=100.0,
        btc_price=84000.0,
    )
    finalize_daily("xlBTC", "2026-04-13")
    daily = DailyPnl.objects.get(segment="xlBTC", date="2026-04-13")
    assert daily.pnl_usdc == pytest.approx(10.5)
    assert daily.rolling_max == pytest.approx(0.033)
    assert daily.drawdown == pytest.approx(0.0)


@pytest.mark.django_db
def test_finalize_daily_computes_drawdown_from_prior_max():
    from tracker.models import Snapshot, DailyPnl
    from tracker.services.collector import finalize_daily

    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 12, 23, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        pnl_usdc=5.0, pnl_native=0.00006, pnl_pct=0.001,
        portfolio_value=0.040, annualized_return=0.365,
        funding_cumulative=2.0, btc_price=84000.0,
    )
    finalize_daily("xlBTC", "2026-04-12")

    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 13, 23, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        pnl_usdc=10.5, pnl_native=0.00012, pnl_pct=0.003,
        portfolio_value=0.033, annualized_return=1.095,
        funding_cumulative=5.0, btc_price=84000.0,
    )
    finalize_daily("xlBTC", "2026-04-13")

    daily = DailyPnl.objects.get(segment="xlBTC", date="2026-04-13")
    assert daily.rolling_max == pytest.approx(0.040)
    expected_drawdown = (0.033 - 0.040) / 0.040
    assert daily.drawdown == pytest.approx(expected_drawdown)


@pytest.mark.django_db
def test_finalize_daily_upserts_on_rerun():
    from tracker.models import Snapshot, DailyPnl
    from tracker.services.collector import finalize_daily

    Snapshot.objects.create(
        timestamp=datetime(2026, 4, 13, 23, 0, tzinfo=timezone.utc),
        segment="xlBTC",
        pnl_usdc=10.5, portfolio_value=0.033,
        pnl_native=0.0, pnl_pct=0.0, annualized_return=0.0,
        funding_cumulative=0.0, btc_price=84000.0,
    )
    finalize_daily("xlBTC", "2026-04-13")
    finalize_daily("xlBTC", "2026-04-13")  # second run should not raise
    assert DailyPnl.objects.filter(segment="xlBTC", date="2026-04-13").count() == 1
