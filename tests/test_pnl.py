import pytest
from tracker.calculator.pnl import compute_pnl, compute_cross_mtm


def test_compute_cross_mtm():
    result = compute_cross_mtm(price_diff=0.5, entry_cost=100.0, position_size=10.0)
    assert result == pytest.approx(95.0)  # 100 - (0.5 * 10)


def test_compute_cross_mtm_zero():
    assert compute_cross_mtm(0.0, 0.0, 0.0) == 0.0


def test_compute_pnl_no_reference():
    result = compute_pnl(
        segment="xlBTC",
        base_asset="BTC",
        current={
            "aave_borrow_usdc": 2000.0,
            "susds_eth_balance": 0.0,
            "susds_arbi_balance": 0.0,
            "btc_price": 84000.0,
            "eth_price": 3000.0,
            "price_diff_hl_bn": 0.5,
        },
        reference=None,
        funding_today=0.0,
        cross_mtm_params={"entry_cost": 0.0, "position_size": 0.0},
    )
    assert result["pnl_usdc"] == pytest.approx(0.0)
    assert result["pnl_native"] == pytest.approx(0.0)
    assert "portfolio_value" in result
    assert "annualized_return" in result


def test_compute_pnl_with_reference():
    reference = {
        "aave_borrow_usdc": 2100.0,
        "susds_eth_balance": 0.0,
        "susds_arbi_balance": 0.0,
        "btc_price": 83000.0,
        "price_diff_hl_bn": 0.4,
        "cross_mtm_value": 96.0,
    }
    current = {
        "aave_borrow_usdc": 2000.0,
        "susds_eth_balance": 0.0,
        "susds_arbi_balance": 0.0,
        "btc_price": 84000.0,
        "eth_price": 3000.0,
        "price_diff_hl_bn": 0.5,
    }
    result = compute_pnl(
        segment="xlBTC",
        base_asset="BTC",
        current=current,
        reference=reference,
        funding_today=5.0,
        cross_mtm_params={"entry_cost": 100.0, "position_size": 10.0},
    )
    # aave_delta=100, funding=5, cross_mtm_delta=(95-96)=-1 → 104
    assert result["pnl_usdc"] == pytest.approx(104.0)
    assert result["pnl_native"] == pytest.approx(104.0 / 84000.0)


def test_compute_pnl_usdc_segment():
    """USDC segment: native_price=1.0, no conversion."""
    result = compute_pnl(
        segment="xlUSD",
        base_asset="USDC",
        current={"aave_borrow_usdc": 1900.0,
                 "susds_eth_balance": 0.0, "susds_arbi_balance": 0.0,
                 "btc_price": 84000.0, "eth_price": 3000.0, "price_diff_hl_bn": 0.0},
        reference={"aave_borrow_usdc": 2000.0,
                   "susds_eth_balance": 0.0, "susds_arbi_balance": 0.0,
                   "price_diff_hl_bn": 0.0, "cross_mtm_value": 0.0},
        funding_today=0.0,
        cross_mtm_params={"entry_cost": 0.0, "position_size": 0.0},
    )
    assert result["pnl_usdc"] == pytest.approx(100.0)
    assert result["pnl_native"] == pytest.approx(100.0)  # USDC: no conversion


def test_susds_included_only_for_xlusd():
    """SUSDS yield counts toward PnL for xlUSD, not for xlBTC or xlETH."""
    base_snap = {
        "aave_borrow_usdc": 0.0,
        "btc_price": 84000.0, "eth_price": 3000.0, "price_diff_hl_bn": 0.0,
    }
    ref = {**base_snap, "susds_eth_balance": 1000.0, "susds_arbi_balance": 500.0,
           "cross_mtm_value": 0.0}
    cur = {**base_snap, "susds_eth_balance": 1010.0, "susds_arbi_balance": 510.0}

    # xlUSD: +10 +10 = +20 USDC included
    result_usd = compute_pnl(
        segment="xlUSD", base_asset="USDC",
        current=cur, reference=ref,
        funding_today=0.0,
        cross_mtm_params={"entry_cost": 0.0, "position_size": 0.0},
    )
    assert result_usd["pnl_usdc"] == pytest.approx(20.0)

    # xlBTC: SUSDS delta ignored
    result_btc = compute_pnl(
        segment="xlBTC", base_asset="BTC",
        current=cur, reference=ref,
        funding_today=0.0,
        cross_mtm_params={"entry_cost": 0.0, "position_size": 0.0},
    )
    assert result_btc["pnl_usdc"] == pytest.approx(0.0)

    # xlETH: SUSDS delta ignored
    result_eth = compute_pnl(
        segment="xlETH", base_asset="ETH",
        current=cur, reference=ref,
        funding_today=0.0,
        cross_mtm_params={"entry_cost": 0.0, "position_size": 0.0},
    )
    assert result_eth["pnl_usdc"] == pytest.approx(0.0)
