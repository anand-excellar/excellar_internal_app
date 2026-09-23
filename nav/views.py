import json
from datetime import date, timedelta, datetime, timezone

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from tracker.models import Snapshot, DailyPnl
from tracker.config import enabled_tokens


def _enabled_segments():
    # Derive visible dashboard segments from enabled tokens only.
    return list(enabled_tokens().keys())


def _enabled_token_map():
    return enabled_tokens()


@login_required
def index(request):
    segments = _enabled_segments()
    return render(request, "nav/index.html", {
        "segments": segments,
        "default_segment": segments[0] if segments else None,
    })


# Fields whose absence means a whole data source failed to collect. When one of
# these is back-filled from an older snapshot the figures on screen are not
# current, and the dashboard has to say so — a NAV that silently reuses last
# week's custody balances reads as a real, smaller NAV.
_SOURCE_FIELDS = {
    "wallet_balance_usd": "custody (BitGo)",
    "wallet_native_amount": "custody (BitGo)",
    "bitgo_holdings_json": "custody (BitGo)",
    "aave_supply_usd": "Aave",
    "aave_net_usd": "Aave",
    "binance_collateral_usdc": "Binance",
    "hl_deposit_usdc": "Hyperliquid",
    "deribit_equity_usd": "Deribit",
    "btc_price": "prices",
    "eth_price": "prices",
}


def _merge_snapshot_with_history(latest: Snapshot) -> dict:
    """Fill gaps in the latest snapshot from earlier ones for the same segment.

    Also records what had to be back-filled, under "carried_forward": a mapping
    of source label -> timestamp the value actually came from. Callers surface
    that so a stale figure is never presented as a fresh one.
    """
    merged = {}
    for field in latest._meta.fields:
        name = field.name
        merged[name] = getattr(latest, name)

    carried: dict[str, object] = {}
    missing_fields = [name for name, value in merged.items()
                      if value is None and name not in {"timestamp", "segment"}]
    if not missing_fields:
        merged["carried_forward"] = carried
        return merged

    for previous in Snapshot.objects.filter(segment=latest.segment, timestamp__lt=latest.timestamp).order_by("-timestamp"):
        for name in missing_fields[:]:
            prev_value = getattr(previous, name)
            if prev_value is not None:
                merged[name] = prev_value
                missing_fields.remove(name)
                label = _SOURCE_FIELDS.get(name)
                # Keep the oldest contributing timestamp per source: that is the
                # age the viewer needs to judge, not the newest.
                if label and (label not in carried or previous.timestamp < carried[label]):
                    carried[label] = previous.timestamp
        if not missing_fields:
            break

    merged["carried_forward"] = carried
    return merged


@login_required
def partial_snapshots(request):
    snapshots = {}
    for seg in _enabled_segments():
        # snapshots[seg] = Snapshot.objects.filter(segment=seg).order_by("-timestamp").first()
        latest = Snapshot.objects.filter(segment=seg).order_by("-timestamp").first()
        snapshots[seg] = None if latest is None else _merge_snapshot_with_history(latest)
    return render(request, "nav/partials/snapshots.html", {"snapshots": snapshots})


def _segment_price(merged: dict, base_asset: str):
    """Spot price of the segment's base asset (USDC pegged at 1)."""
    base_asset = (base_asset or "").upper()
    if base_asset == "ETH":
        return merged.get("eth_price")
    if base_asset == "BTC":
        return merged.get("btc_price")
    if base_asset == "USDC":
        return 1.0
    return None


def _perp_symbol_base(symbol) -> str:
    """'BTC/USDC:USDC' or 'BTC-PERP' -> 'BTC'."""
    if not symbol:
        return "Perp"
    return str(symbol).split("/")[0].split("-")[0].split(":")[0].upper()


def _shared_mark(venue: str, base: str, merged: dict):
    """A consistent per-cycle mark for a perp leg, from the shared price feed.

    Each segment's positions are fetched in separate sequential calls, so their
    per-position marks drift a few dollars. Using the cycle's shared BTC/ETH
    price makes every catalog show the same mark. Display-only — uPnL/NAV use the
    exchange's exact per-position figures. HL basis: price_diff_hl_bn = btc_hl −
    btc_bn (all segments run direction 1).
    """
    base = (base or "").upper()
    if base == "BTC":
        btc = merged.get("btc_price")
        if btc is None:
            return None
        return btc + (merged.get("price_diff_hl_bn") or 0.0) if venue == "Hyperliquid" else btc
    if base == "ETH":
        return merged.get("eth_price")
    return None


def compute_nav_usd(merged: dict, price=None) -> float:
    """The single source of truth for a segment's NAV in USD.

    NAV = Native reserve (priced custody assets) + Aave collateral − Aave loan
        + Spark Savings (sUSDS) + ERC-4626 vaults + exchange equity (HL +
        Binance + Deribit, each = cash + its own uPnL).
    All components are already USD; ``price`` is only used for the legacy
    fallback (snapshots without per-wallet custody detail).
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
    susds = (float(merged.get("susds_arbi_balance") or 0.0)
             + float(merged.get("susds_eth_balance") or 0.0))
    vault = float(merged.get("vault_usd") or 0.0)
    exchange = (float(merged.get("hl_deposit_usdc") or 0.0)
                + float(merged.get("binance_collateral_usdc") or 0.0)
                + float(merged.get("deribit_equity_usd") or 0.0))
    return native + aave_collat - aave_loan + susds + vault + exchange


def _build_catalog_for_segment(merged: dict, base_asset: str) -> dict:
    """Turn a (history-merged) snapshot dict into grouped catalog sections.

    Mirrors the asset-breakdown layout: Native reserve / Aave position /
    Exchange collateral & perp exposure, plus a netted footer. Only rows with
    real data are emitted — nothing is fabricated.
    """
    price = _segment_price(merged, base_asset)
    sections = []

    # --- Net components accumulate as we build rows ---
    native_usd = aave_collat_usd = 0.0
    aave_loan = float(merged.get("aave_borrow_usdc") or 0.0)

    # 1. Native reserve (BitGo custody, broken out per wallet) -------------
    try:
        bitgo_wallets = (json.loads(merged.get("bitgo_holdings_json") or "{}")
                         .get("wallets")) or []
    except (ValueError, TypeError):
        bitgo_wallets = []

    native_rows = []
    native_note = ""
    if bitgo_wallets:
        # One header row per wallet, then a row per asset it holds. Every valued
        # asset contributes to the net (per the "all custody assets" choice).
        for wallet in bitgo_wallets:
            native_rows.append({
                "wallet_header": wallet.get("label") or "(unlabeled wallet)",
                "wallet_net": wallet.get("coin") or "",
            })
            for asset in wallet.get("assets", []):
                asset_usd = asset.get("usd")
                if asset_usd is not None:
                    native_usd += asset_usd
                native_rows.append({
                    "asset": asset.get("symbol"), "indent": True,
                    "qty": asset.get("amount"), "price": asset.get("price"),
                    "value": asset_usd,
                })
        native_note = f"{len(bitgo_wallets)} custody wallet{'s' if len(bitgo_wallets) != 1 else ''}"
    else:
        # Fallback for snapshots taken before per-wallet detail was stored.
        native_amt = merged.get("wallet_native_amount")
        if native_amt:
            value = native_amt * price if price is not None else None
            native_usd = value or 0.0
            native_rows.append({
                "asset": base_asset, "sub": "Wallet",
                "qty": native_amt, "price": price, "value": value,
            })
            native_note = f"{native_amt:.4f} {base_asset}"

    if native_rows:
        sections.append({
            "title": "Native reserve",
            "note": native_note,
            "rows": native_rows,
        })

    # 2. Aave position -----------------------------------------------------
    aave_rows = []
    supply_amt = merged.get("aave_supply_amount")
    supply_usd = merged.get("aave_supply_usd")
    if supply_amt or supply_usd:
        value = supply_usd if supply_usd is not None else (
            supply_amt * price if (supply_amt and price is not None) else None)
        aave_collat_usd = value or 0.0
        aave_rows.append({
            "asset": f"{base_asset} (collateral)", "sub": "Locked in Aave",
            "qty": supply_amt, "price": price, "value": value,
        })
    health = merged.get("aave_health_rate")
    if aave_loan:
        ltv = (aave_loan / aave_collat_usd * 100) if aave_collat_usd else None
        sub_parts = []
        if ltv is not None:
            sub_parts.append(f"LTV {ltv:.1f}%")
        if health is not None:
            sub_parts.append(f"HF {health:.2f}")
        sub = "Aave · " + " · ".join(sub_parts) if sub_parts else "Aave"
        aave_rows.append({
            "asset": "USDC (borrowed)", "sub": sub,
            "qty": aave_loan, "price": 1.0, "value": -aave_loan,
            "negative": True,
        })
    if aave_rows:
        # Health factor: <1 = liquidatable, so surface it prominently on the header.
        note = f"health factor {health:.2f}" if health is not None else ""
        sections.append({"title": "Aave position", "note": note, "rows": aave_rows})

    # 2b. Spark Savings (sUSDS) --------------------------------------------
    # sUSDS held in the wallet, valued at the live sUSDS->USDS rate by the
    # on-chain Spark reader. Stored per chain as a USD value.
    susds_arbi = merged.get("susds_arbi_balance")
    susds_eth = merged.get("susds_eth_balance")
    spark_rows = []
    if susds_arbi:
        spark_rows.append({
            "asset": "sUSDS", "sub": "Arbitrum · Spark Savings",
            "value": float(susds_arbi),
        })
    if susds_eth:
        spark_rows.append({
            "asset": "sUSDS", "sub": "Ethereum · Spark Savings",
            "value": float(susds_eth),
        })
    if spark_rows:
        sections.append({"title": "Spark Savings", "note": "sUSDS", "rows": spark_rows})

    # 2c. ERC-4626 vaults ---------------------------------------------------
    # The share token held in the wallet IS the position; the on-chain reader
    # converts shares to the underlying at the live rate. Listed per vault so a
    # row here lines up with the counterparty's own line item at reconciliation.
    try:
        vault_positions = json.loads(merged.get("vault_positions_json") or "[]")
    except (ValueError, TypeError):
        vault_positions = []
    vault_rows = [
        {
            "asset": (p.get("symbol") or "").upper() or p.get("name", ""),
            "sub": f"{(p.get('chain') or '').title()} · {p.get('name', '')}".strip(" ·"),
            "value": float(p["usd"]),
        }
        for p in vault_positions if p.get("usd") is not None
    ]
    if vault_rows:
        sections.append({"title": "Vaults", "note": "ERC-4626", "rows": vault_rows})

    # 3. Exchange funds & perp exposure (per venue, report-style) ----------
    # Mirrors the official report: each venue shows CASH rows + a futures row
    # valued at its unrealized PnL, summing to the venue's EQUITY (= what's in
    # NAV). Futures are valued at uPnL, never notional.
    try:
        legs = json.loads(merged.get("perp_positions_json") or "[]")
    except (ValueError, TypeError):
        legs = []
    try:
        exch_balances = json.loads(merged.get("exchange_balances_json") or "{}")
    except (ValueError, TypeError):
        exch_balances = {}

    _venue_available = {
        "Binance": merged.get("binance_available_usdc"),
        "Hyperliquid": merged.get("hl_available_usdc"),
        "Deribit": merged.get("deribit_available_usd"),
    }
    _venue_equity = {
        "Binance": merged.get("binance_collateral_usdc"),
        "Hyperliquid": merged.get("hl_deposit_usdc"),
        "Deribit": merged.get("deribit_equity_usd"),
    }
    legs_by_venue: dict[str, list] = {}
    pnl_by_venue: dict[str, float] = {}
    for leg in legs:
        v = leg.get("venue") or ""
        legs_by_venue.setdefault(v, []).append(leg)
        pnl_by_venue[v] = pnl_by_venue.get(v, 0.0) + float(leg.get("unrealized_pnl") or 0.0)

    exch_rows = []
    total_exch_equity = 0.0
    any_venue = False
    for venue in ("Binance", "Hyperliquid", "Deribit"):
        assets = exch_balances.get(venue) or []
        v_legs = legs_by_venue.get(venue, [])
        if not assets and not v_legs:
            continue
        any_venue = True
        eq = _venue_equity.get(venue)
        pnl = pnl_by_venue.get(venue, 0.0)
        avail = _venue_available.get(venue)
        if eq is not None:
            total_exch_equity += float(eq)
        # Venue header: name + its equity (what enters NAV).
        exch_rows.append({
            "wallet_header": venue,
            "wallet_net": f"${float(eq):,.2f}" if eq is not None else "",
        })

        # CASH rows (spot, excl. uPnL). HL's spot balance folds in uPnL, so scale
        # cash down to (equity − uPnL); for Binance the ratio is ~1 (already cash).
        asset_sum = sum(a["usd"] for a in assets if a.get("usd") is not None)
        cash_total = (float(eq) - pnl) if eq is not None else asset_sum
        ratio = (cash_total / asset_sum) if asset_sum else 1.0
        for a in assets:
            usd = a.get("usd")
            amt = a.get("amount")
            exch_rows.append({
                "asset": a.get("coin"), "indent": True,
                "qty": (amt * ratio) if amt is not None else None,
                "price": a.get("price"),
                "value": (usd * ratio) if usd is not None else None,
            })

        # PERP rows, valued at unrealized PnL, with a liquidation-buffer badge.
        for leg in v_legs:
            size = leg.get("size") or 0
            side = "long" if (leg.get("side") or "").lower() == "long" else "short"
            lpnl = float(leg.get("unrealized_pnl") or 0.0)
            base_sym = _perp_symbol_base(leg.get("symbol"))
            leg_price = (_shared_mark(venue, base_sym, merged)
                         or leg.get("mark_price") or leg.get("entry_price") or price)
            lev = leg.get("leverage")
            sub = f"{side} {abs(size):g}" + (f" @ {lev:g}x" if lev else "")
            # Liq buffer = how far the mark can move before liquidation.
            liq = leg.get("liquidation_price")
            liq_pct = liq_level = None
            if liq and leg_price:
                liq_pct = abs(leg_price - liq) / leg_price * 100
                liq_level = ("safe" if liq_pct >= 40
                             else "warn" if liq_pct >= 20 else "danger")
            exch_rows.append({
                "perp": True, "asset": f"{base_sym} perp", "sub": sub,
                "value": lpnl, "negative": lpnl < 0,
                "liq_pct": liq_pct, "liq_price": liq, "liq_level": liq_level,
            })

        # Available to withdraw (free collateral).
        if avail is not None:
            exch_rows.append({"kv_row": True, "label": "Available to withdraw", "value": float(avail)})

    if any_venue:
        exch_rows.append({"total_row": True, "label": "Total exchange equity",
                          "value": total_exch_equity})

    if exch_rows:
        sections.append({
            "title": "Exchange funds & perp exposure",
            "note": "", "rows": exch_rows,
        })

    # --- Net position (after Aave loan) -----------------------------------
    # Exchange NAV = each venue's ACCOUNT EQUITY (cash + its own unrealized PnL),
    # matching the official report (futures valued at uPnL, not notional). We use
    # the stored equities — HL account_value already folds in uPnL, Binance
    # actualEquity = collateral + uPnL — so we must NOT add perp_pnl again
    # (that double-counted HL's uPnL).
    net = compute_nav_usd(merged, price)   # single source of truth (== stored/debug NAV)
    gross = net + aave_loan
    has_data = bool(sections)

    # Express the net in the segment's native unit (BTC/ETH/USDC) for the
    # headline; USD stays as the secondary figure since components are in USD.
    net_native = (net / price) if (has_data and price) else None

    return {
        "has_data": has_data,
        "sections": sections,
        "net": net if has_data else None,
        "net_native": net_native,
        "base_asset": base_asset,
        "gross": gross,
        "aave_loan": aave_loan,
        "timestamp": merged.get("timestamp"),
    }


@login_required
def partial_catalog(request):
    """Per-segment asset catalog (Native reserve / Aave / Exchange & perps)."""
    catalog = {}
    tokens = _enabled_token_map()
    for seg, token in tokens.items():
        latest = Snapshot.objects.filter(segment=seg).order_by("-timestamp").first()
        merged = None if latest is None else _merge_snapshot_with_history(latest)
        entry = (
            None if merged is None
            else _build_catalog_for_segment(merged, token.base_asset)
        )
        if entry is not None:
            # Sorted so the banner lists sources in a stable order.
            entry["carried_forward"] = sorted(
                (merged.get("carried_forward") or {}).items(), key=lambda kv: kv[0])
        catalog[seg] = entry
    # Total NAV across all xltokens. Safe to sum because each segment reads its
    # own exchange accounts (no shared balance double-counted).
    total_nav = sum(
        c["net"] for c in catalog.values()
        if c and c.get("net") is not None
    )
    return render(request, "nav/partials/catalog.html", {
        "catalog": catalog,
        "total_nav": total_nav,
    })


@login_required
def partial_bitgo_holdings(request):
    """Aggregated BitGo custody holdings across all wallets (single combined view)."""
    latest = (
        Snapshot.objects
        .filter(segment__in=_enabled_segments())
        .exclude(bitgo_holdings_json__isnull=True)
        .order_by("-timestamp")
        .first()
    )
    holdings, total_usd, timestamp = [], None, None
    if latest and latest.bitgo_holdings_json:
        try:
            agg = json.loads(latest.bitgo_holdings_json)
            holdings = agg.get("holdings", []) or []
            total_usd = agg.get("total_usd")
            timestamp = latest.timestamp
        except (ValueError, TypeError):
            pass
    return render(request, "nav/partials/bitgo_holdings.html", {
        "holdings": holdings,
        "total_usd": total_usd,
        "timestamp": timestamp,
    })


@login_required
def partial_portfolio(request):
    summary = []
    for seg in _enabled_segments():
        latest = DailyPnl.objects.filter(segment=seg).order_by("-date").first()
        if latest:
            summary.append({
                "segment": latest.segment,
                "date": latest.date,
                "portfolio_value": latest.portfolio_value,
                "rolling_max": latest.rolling_max,
                "drawdown_pct": round((latest.drawdown or 0) * 100, 4),
                "annualized_pct": round((latest.annualized_return or 0) * 100, 2),
            })
    return render(request, "nav/partials/portfolio.html", {"summary": summary})


@login_required
def chart_data(request):
    segments = _enabled_segments()
    segment = request.GET.get("segment") or (segments[0] if segments else None)
    if not segment:
        return JsonResponse({"labels": [], "pnl_usdc": [], "portfolio_value": []})
    if segment not in segments:
        return JsonResponse({"error": "invalid segment"}, status=400)
    try:
        days = int(request.GET.get("days", 30))
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid days"}, status=400)
    days = max(1, min(days, 365))
    since = date.today() - timedelta(days=days)

    rows = list(
        DailyPnl.objects
        .filter(segment=segment, date__gte=since)
        .order_by("date")
        .values("date", "pnl_usdc", "portfolio_value")
    )

    return JsonResponse({
        "labels": [str(r["date"]) for r in rows],
        "pnl_usdc": [r["pnl_usdc"] for r in rows],
        "portfolio_value": [r["portfolio_value"] for r in rows],
    })


@login_required
def partial_pnl_debug(request):
    """[DEBUG] Per-segment PnL breakdown: reference vs current snapshot with deltas."""
    now = datetime.now(timezone.utc)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = []
    tokens = _enabled_token_map()
    for seg, token in tokens.items():
        current = Snapshot.objects.filter(segment=seg).order_by("-timestamp").first()
        reference = (
            Snapshot.objects
            .filter(segment=seg, timestamp__lt=today_midnight)
            .order_by("-timestamp")
            .first()
        )

        def val(snap, field):
            return getattr(snap, field, None) if snap else None

        def delta(curr_val, ref_val, invert=False):
            if curr_val is None or ref_val is None:
                return None
            d = curr_val - ref_val
            return -d if invert else d

        aave_delta     = delta(val(reference, "aave_borrow_usdc"), val(current, "aave_borrow_usdc"))  # inverted
        susds_eth_d    = delta(val(current, "susds_eth_balance"), val(reference, "susds_eth_balance"))
        susds_arbi_d   = delta(val(current, "susds_arbi_balance"), val(reference, "susds_arbi_balance"))
        susds_component = (susds_eth_d or 0) + (susds_arbi_d or 0) if seg == "xlUSD" else None

        # Cross-MtM delta
        cross_mtm_cfg = token.cross_mtm or {"entry_cost": 0.0, "position_size": 0.0, "direction": 1}
        cross_mtm_direction = cross_mtm_cfg.get("direction", 1)
        cross_mtm_direction_label = (
            "HL − Binance" if cross_mtm_direction == 1 else
            "Binance − HL" if cross_mtm_direction == -1 else
            str(cross_mtm_direction)
        )
        cross_mtm_now = None
        cross_mtm_delta = None
        if current and current.cross_mtm_value is not None:
            cross_mtm_now = current.cross_mtm_value
            if reference and reference.cross_mtm_value is not None:
                cross_mtm_delta = cross_mtm_now - reference.cross_mtm_value

        funding = val(current, "funding_cumulative")

        components = [
            c for c in [aave_delta, susds_component, funding, cross_mtm_delta]
            if c is not None
        ]
        net_pnl = sum(components) if components else None

        # --- NAV reconciliation: ΔNAV vs component PnL ---
        # NAV is the same figure the catalog shows. ΔNAV should ≈ component PnL;
        # any gap is capital flows (deposits/withdrawals) or price moves on
        # holdings that the component formula doesn't capture.
        def _snap_dict(snap):
            return {f.name: getattr(snap, f.name) for f in snap._meta.fields} if snap else {}

        nav_now = (compute_nav_usd(_snap_dict(current),
                                   _segment_price(_snap_dict(current), token.base_asset))
                   if current else None)
        nav_ref = (compute_nav_usd(_snap_dict(reference),
                                   _segment_price(_snap_dict(reference), token.base_asset))
                   if reference else None)
        delta_nav = (nav_now - nav_ref) if (nav_now is not None and nav_ref is not None) else None
        nav_gap = (delta_nav - net_pnl) if (delta_nav is not None and net_pnl is not None) else None

        rows.append({
            "segment": seg,
            # NAV reconciliation
            "nav_now": nav_now,
            "nav_ref": nav_ref,
            "delta_nav": delta_nav,
            "nav_gap": nav_gap,
            # Reference snapshot
            "ref_ts": reference.timestamp if reference else None,
            "ref_aave_borrow": val(reference, "aave_borrow_usdc"),
            "ref_aave_supply": val(reference, "aave_supply_usd"),
            "ref_health": val(reference, "aave_health_rate"),
            "ref_susds_eth": val(reference, "susds_eth_balance"),
            "ref_susds_arbi": val(reference, "susds_arbi_balance"),
            "ref_cross_mtm": val(reference, "cross_mtm_value"),
            "ref_price_diff": val(reference, "price_diff_hl_bn"),
            # Current snapshot
            "cur_ts": current.timestamp if current else None,
            "cur_aave_borrow": val(current, "aave_borrow_usdc"),
            "cur_aave_supply": val(current, "aave_supply_usd"),
            "cur_health": val(current, "aave_health_rate"),
            "cur_susds_eth": val(current, "susds_eth_balance"),
            "cur_susds_arbi": val(current, "susds_arbi_balance"),
            "cur_cross_mtm": cross_mtm_now,
            "cur_price_diff": val(current, "price_diff_hl_bn"),
            "cross_mtm_direction": cross_mtm_direction,
            "cross_mtm_direction_label": cross_mtm_direction_label,
            # Deltas / components
            "aave_delta": aave_delta,
            "susds_eth_delta": susds_eth_d,
            "susds_arbi_delta": susds_arbi_d,
            "susds_component": susds_component,
            "funding_today": funding,
            "cross_mtm_delta": cross_mtm_delta,
            # Result
            "net_pnl": net_pnl,
            "snap_pnl": val(current, "pnl_usdc"),
        })

    return render(request, "nav/partials/pnl_debug.html", {"rows": rows})
