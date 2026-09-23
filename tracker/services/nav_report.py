"""Build a NAV report from the latest per-xltoken catalog snapshots.

Reuses the dashboard catalog builders (``_build_catalog_for_segment`` /
``compute_nav_usd``) so the report matches the dashboard exactly — the single
source of truth for a segment's NAV stays ``compute_nav_usd``. This module only
reshapes that data for a spreadsheet; it never re-derives NAV differently.

Data source is the latest stored ``Snapshot`` per segment (history-merged the
same way the dashboard does), so building a report makes no external API calls.
"""
import json
import logging
from datetime import datetime, timezone

from tracker.models import Snapshot
from tracker.config import enabled_tokens

logger = logging.getLogger(__name__)


def _latest_merged(segment: str, as_of=None):
    """Latest snapshot for a segment, history-merged (as the dashboard does).

    ``as_of`` (an aware datetime) takes the last snapshot at or before that
    moment instead of the newest one, so a report can be rebuilt as it stood at a
    past point rather than mixing old comparisons with today's positions.
    """
    qs = Snapshot.objects.filter(segment=segment)
    if as_of is not None:
        qs = qs.filter(timestamp__lte=as_of)
    latest = qs.order_by("-timestamp").first()
    if latest is None:
        return None

    from nav.views import _merge_snapshot_with_history

    return _merge_snapshot_with_history(latest)


def compute_nav_components(merged: dict, price=None) -> dict:
    """Break NAV into its additive parts, mirroring ``compute_nav_usd``.

    The parts sum to the same figure ``compute_nav_usd`` returns:
        net = native + aave_collateral - aave_loan + spark + vaults
              + exchange_equity
    Kept in lock-step with ``nav.views.compute_nav_usd`` so the report's
    component columns always reconcile to the headline NAV.
    """
    try:
        wallets = (json.loads(merged.get("bitgo_holdings_json") or "{}").get("wallets")) or []
    except (ValueError, TypeError):
        wallets = []
    if wallets:
        native = sum(a["usd"] for w in wallets for a in (w.get("assets") or [])
                     if a.get("usd") is not None)
    else:
        amt = merged.get("wallet_native_amount")
        native = (amt * price) if (amt and price is not None) else 0.0

    aave_collat = float(merged.get("aave_supply_usd") or 0.0)
    aave_loan = float(merged.get("aave_borrow_usdc") or 0.0)
    spark = (float(merged.get("susds_arbi_balance") or 0.0)
             + float(merged.get("susds_eth_balance") or 0.0))
    vault = float(merged.get("vault_usd") or 0.0)
    exchange = (float(merged.get("hl_deposit_usdc") or 0.0)
                + float(merged.get("binance_collateral_usdc") or 0.0)
                + float(merged.get("deribit_equity_usd") or 0.0))
    return {
        "native_reserve_usd": native,
        "aave_collateral_usd": aave_collat,
        "aave_loan_usd": aave_loan,
        "spark_usd": spark,
        "vault_usd": vault,
        "exchange_equity_usd": exchange,
    }


def build_report(as_of=None) -> dict:
    """Assemble the NAV report for every enabled xltoken.

    ``as_of`` rebuilds it from the snapshot current at that moment (see
    ``_latest_merged``); the default is the newest snapshot.

    Returns::

        {
          "generated_at": <aware datetime, UTC>,
          "total_nav_usd": <float>,        # sum of each segment's NAV
          "segments": [ {segment, base_asset, has_data, timestamp,
                         net_usd, net_native, gross_usd, aave_loan_usd,
                         components, sections}, ... ],
        }

    ``sections`` is the exact catalog structure the dashboard renders, so the
    detail sheet is a 1:1 flattening of the on-screen catalog.
    """
    from nav.views import _build_catalog_for_segment, _segment_price

    segments = []
    total_nav = 0.0
    for seg, token in enabled_tokens().items():
        merged = _latest_merged(seg, as_of)
        if merged is None:
            segments.append({
                "segment": seg,
                "base_asset": token.base_asset,
                "has_data": False,
                "timestamp": None,
            })
            continue

        price = _segment_price(merged, token.base_asset)
        catalog = _build_catalog_for_segment(merged, token.base_asset)
        components = compute_nav_components(merged, price)
        net = catalog.get("net")
        if net is not None:
            total_nav += net

        segments.append({
            "segment": seg,
            "base_asset": token.base_asset,
            "has_data": bool(catalog.get("has_data")),
            "timestamp": merged.get("timestamp"),
            "price": price,
            "net_usd": net,
            "net_native": catalog.get("net_native"),
            "gross_usd": catalog.get("gross"),
            "aave_loan_usd": catalog.get("aave_loan"),
            "components": components,
            "sections": catalog.get("sections", []),
        })

    return {
        "generated_at": datetime.now(timezone.utc),
        "total_nav_usd": total_nav,
        "segments": segments,
    }