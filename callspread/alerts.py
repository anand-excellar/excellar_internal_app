"""Rule evaluation + alert transport — port of the original `src/alerts.js`.

Rules are evaluated on every poll; nothing is *sent* yet. To turn alerting on,
implement `deliver()` below (Slack webhook, Telegram bot, SMTP, PagerDuty) and
set `enabled: True`.

Alerts fire on STATE TRANSITIONS, not on every poll, so a position sitting below
the floor pages you once rather than every 60 seconds. `resolve` transitions
(back to good) are sent too, so a channel never sits on a stale red.
"""

import logging
import time
from datetime import datetime, timezone

from django.conf import settings as django_settings

from callspread.model import fin, parse_date

log = logging.getLogger(__name__)

# Read via Django settings, not os.environ directly, so portal/settings_test.py
# can blank this out and guarantee a test run never fires a real Slack message.
_SLACK_WEBHOOK_URL = (django_settings.CALLSPREAD.get("slack_webhook_url") or "").strip()

ALERT_CONFIG = {
    # Enabled automatically once a webhook is configured — shares the same
    # SLACK_WEBHOOK_URL as the NAV reconciliation alerts.
    "enabled": bool(_SLACK_WEBHOOK_URL),
    "transport": {"kind": "slack", "url": _SLACK_WEBHOOK_URL} if _SLACK_WEBHOOK_URL else None,
    # Re-notify an unresolved critical this often (0 = never re-notify).
    "reminderMinutes": 0,
}


def _pct(v):
    return f"{v * 100:.2f}%" if fin(v) else "—"


def _n0(v):
    return f"{round(v):,}" if fin(v) else "—"


def _usd(v, decimals=1):
    return f"${v:,.{decimals}f}" if fin(v) else "—"


def _money(v, decimals=2):
    """Signed dollar amount — the ``+`` makes a P&L component's direction explicit."""
    return f"${v:+,.{decimals}f}" if fin(v) else "—"


def evaluate_rules(snap: dict, config: dict) -> list:
    """:returns: [{id, name, state, detail, value}] where state ∈ good|warning|critical"""
    dm = snap["deltaMonitor"]
    cov = snap["coverage"]
    sp = snap["spread"]
    d = sp["distanceToShortStrike"]
    blended = dm["blendedDelta"]

    # JS compares `null < x` as `0 < x`; keep that behaviour so a book with no
    # CC holdings grades the same way it does today.
    blended_cmp = 0 if blended is None else blended
    if blended_cmp < dm["hardFloor"]:
        delta_state = "critical"
    elif blended_cmp < dm["hardFloor"] + dm["warningBuffer"]:
        delta_state = "warning"
    else:
        delta_state = "good"

    warn_buffer = config["policy"].get("coverageWarnBuffer")
    warn_buffer = 0.005 if warn_buffer is None else warn_buffer
    if cov["availableToWrite"] < 0:
        coverage_state = "critical"
    elif cov["availableToWrite"] < cov["ccReserved"] * warn_buffer:
        coverage_state = "warning"
    else:
        coverage_state = "good"

    d_cmp = 0 if d is None else d
    strike_state = "critical" if d_cmp > 0 else ("warning" if d_cmp > -0.05 else "good")

    roll = snap["roll"]
    if roll["status"] == "unknown":
        roll_state = "warning"
    elif roll["status"] == "serious":
        roll_state = "warning"
    else:
        roll_state = roll["status"]

    if fin(roll["daysToRoll"]):
        roll_value = (
            f"{roll['daysToRoll']:.1f}d to roll" if roll["daysToRoll"] > 0
            else f"{abs(roll['daysToRoll']):.1f}d overdue"
        )
    else:
        roll_value = "—"

    roll_detail = f"roll {roll['rollDaysBeforeExpiry']:g}d before expiry"
    if roll["rollDate"]:
        roll_detail += f" — by {roll['rollDate'][:10]}"

    return [
        {
            "id": "delta-floor",
            "name": "Blended delta vs hard floor",
            "state": delta_state,
            "detail": f"floor {_pct(dm['hardFloor'])} · target {_pct(dm['target'])}",
            "value": _pct(blended),
        },
        {
            "id": "coverage",
            "name": "Written calls covered 1:1",
            "state": coverage_state,
            "detail": f"{_n0(cov['ccReserved'])} reserved of {_n0(cov['totalCcHeld'])} held",
            "value": f"{_n0(cov['availableToWrite'])} {snap['baseCurrency']} free",
        },
        {
            "id": "short-strike",
            "name": "Spot approaching the short strike",
            "state": strike_state,
            "detail": f"short strike {_fmt_strike(sp['shortStrike'])}",
            "value": _pct(d),
        },
        {
            "id": "roll-window",
            "name": "Roll window",
            # Policy rolls `rollDaysBeforeExpiry` out, so the roll date is the
            # date that matters — expiry itself should never be reached.
            "state": roll_state,
            "detail": roll_detail,
            "value": roll_value,
        },
        {
            "id": "mark-sanity",
            "name": "Option marks self-consistent",
            "state": "warning" if sp["marksInverted"] else "good",
            "detail": "lower strike must mark above the higher strike",
            "value": "crossed" if sp["marksInverted"] else "ok",
        },
    ]


def _fmt_strike(v):
    """Render a strike the way JS string interpolation does (0.135, not 0.135000)."""
    if v is None:
        return "null"
    return f"{v:g}"


def _explain_issue(rule: dict, snap: dict) -> str:
    """Plain-language reason a rule is in this state — not just its raw detail/value.

    Only meaningful for warning/critical; callers filter out "good" before this
    is ever called.
    """
    rid, state = rule["id"], rule["state"]
    dm, cov, sp, roll = snap["deltaMonitor"], snap["coverage"], snap["spread"], snap["roll"]
    base = snap.get("baseCurrency", "")

    if rid == "delta-floor":
        if state == "critical":
            return (f"Blended delta ({_pct(dm['blendedDelta'])}) is below the "
                    f"{_pct(dm['hardFloor'])} hard floor — the book is under-hedged.")
        return (f"Blended delta ({_pct(dm['blendedDelta'])}) is only "
                f"{_pct(dm.get('distanceToFloor'))} above the {_pct(dm['hardFloor'])} hard floor.")

    if rid == "coverage":
        if state == "critical":
            return (f"availableToWrite is negative ({_n0(cov['availableToWrite'])} {base}) — "
                    f"more calls are written than {base} held to cover them.")
        return (f"Only {_n0(cov['availableToWrite'])} {base} free against "
                f"{_n0(cov['ccReserved'])} reserved — the coverage buffer is thin.")

    if rid == "short-strike":
        d = sp["distanceToShortStrike"]
        if state == "critical":
            return (f"Spot ({snap['spot']}) has reached or crossed the short strike "
                    f"({_fmt_strike(sp['shortStrike'])}) — the written call is in the money.")
        return (f"Spot ({snap['spot']}) is {_pct(abs(d)) if fin(d) else '—'} from the short "
                f"strike ({_fmt_strike(sp['shortStrike'])}) — approaching in-the-money.")

    if rid == "roll-window":
        if roll["status"] == "unknown":
            return "Roll status can't be determined from the current marks."
        days = roll.get("daysToRoll")
        if fin(days) and days < 0:
            return f"The roll window opened {abs(days):.1f}d ago and hasn't been actioned."
        return roll.get("statusText") or rule["detail"]

    if rid == "mark-sanity":
        return ("The short strike is marking above the long strike — impossible for a real "
                "call spread. Treat mark-derived P&L as unreliable until the surface refreshes.")

    return rule["detail"]


_last_state: dict = {}
_last_notified: dict = {}


def process_alerts(snap: dict, config: dict) -> dict:
    """Diff against the previous poll and deliver only what changed."""
    rules = evaluate_rules(snap, config)

    if not snap.get("legs"):
        # No open option positions: distanceToShortStrike and daysToExpiry are
        # both None with nothing to spread, which the rules above read as "0"
        # and "unknown" — a flat book would otherwise misreport as an active
        # short-strike/roll breach. Leave `_last_state` untouched so the next
        # real position's first evaluation is diffed against the last real
        # state, not this flat one.
        return {"rules": rules, "sent": []}

    to_send = []

    for r in rules:
        prev = _last_state.get(r["id"])
        changed = prev is not None and prev != r["state"]
        stale_critical = (
            ALERT_CONFIG["reminderMinutes"] > 0
            and r["state"] == "critical"
            and (time.time() - _last_notified.get(r["id"], 0)) > ALERT_CONFIG["reminderMinutes"] * 60
        )

        if changed or stale_critical:
            to_send.append({**r, "previous": prev,
                            "kind": "resolve" if r["state"] == "good" else "trigger"})
            _last_notified[r["id"]] = time.time()
        _last_state[r["id"]] = r["state"]

    if ALERT_CONFIG["enabled"] and to_send:
        for alert in to_send:
            try:
                deliver(alert, snap)
            except Exception as exc:  # a broken webhook must not kill the poll
                log.error("alert delivery failed: %s", exc)

    return {"rules": rules, "sent": to_send}


def _dispatch(text: str, *, alert: dict | None = None, snap: dict | None = None) -> None:
    """Send ``text`` through whatever transport ``ALERT_CONFIG`` names.

    Shared by per-rule alerts (``deliver``) and the periodic status heartbeat,
    so both honour the same enabled/transport switch and fail the same way — a
    broken webhook logs and moves on rather than taking the poll down with it.
    """
    import requests

    t = ALERT_CONFIG["transport"]
    if not t:
        log.info("[alert:dry-run] %s", text)
        return

    if t["kind"] == "slack":
        requests.post(t["url"], json={"text": text}, timeout=10)
    elif t["kind"] == "telegram":
        requests.post(
            f"https://api.telegram.org/bot{t['botToken']}/sendMessage",
            json={"chat_id": t["chatId"], "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    elif t["kind"] == "webhook":
        requests.post(t["url"], json={"text": text, "alert": alert, "snapshot": snap}, timeout=10)
    else:
        log.info("[alert:unrouted] %s", text)


def deliver(alert: dict, snap: dict):
    """Post one rule's state change (or resolution) to the configured transport."""
    if alert["state"] == "critical":
        headline = "🚨 *SERIOUS — CallSpread*"
        body = _explain_issue(alert, snap)
    elif alert["state"] == "warning":
        headline = "🟠 *WARNING — CallSpread*"
        body = _explain_issue(alert, snap)
    else:
        headline = "🟢 *RESOLVED — CallSpread*"
        body = f"{alert['name']} is back to normal ({alert['value']}, {alert['detail']})."

    text = (
        f"{headline}: {alert['name']}\n"
        f"{body}\n"
        f"spot {snap['spot']} · blended delta {_pct(snap['deltaMonitor']['blendedDelta'])}"
        f" · {_n0(snap['coverage']['totalCcHeld'])} {snap['baseCurrency']} held"
    )
    _dispatch(text, alert=alert, snap=snap)


def send_heartbeat(state: dict, runtime) -> None:
    """Post a full status summary on a fixed schedule, mirroring the dashboard's own
    tiles (spot, blended delta, greeks, P&L, coverage, roll), regardless of whether
    anything changed — and spell out the reason for every open issue.

    ``state`` is ``Runtime.read_state()`` — whatever the poller last wrote — so
    this never triggers its own STS call. A poller that has stopped shows up as
    a stale "last poll" age rather than silence.
    """
    snap = state.get("snapshot")
    if not snap:
        _dispatch(":grey_question: *CallSpread status* — no snapshot yet.")
        return
    if not snap.get("legs"):
        # Flat book: nothing open to report on, and the rule states in `state`
        # may be the flat-book false positives process_alerts already refused
        # to alert on (see process_alerts). Stay silent rather than repeat them.
        return

    rules = state.get("rules") or evaluate_rules(snap, runtime.config)
    severity = {"good": 0, "warning": 1, "critical": 2}
    worst_state = max((r["state"] for r in rules), key=lambda s: severity.get(s, 0), default="good")
    icon = {"good": "🟢", "warning": "🟠", "critical": "🔴"}[worst_state]

    stale_note = ""
    last_poll = parse_date(state.get("lastPollAt"))
    if last_poll:
        age_h = (datetime.now(timezone.utc) - last_poll).total_seconds() / 3600
        if age_h > 1:
            stale_note = f"  ⚠️ last poll {age_h:.1f}h ago"

    dm = snap.get("deltaMonitor") or {}
    cov = snap.get("coverage") or {}
    pnl = snap.get("pnl") or {}
    roll = snap.get("roll") or {}
    greeks = (snap.get("aggregation") or {}).get("portfolio") or {}
    base = snap.get("baseCurrency", "")

    lines = [
        f"{icon} *CallSpread status* — {runtime.mode.upper()}{stale_note}",
        f"Spot {snap.get('spot')}  ·  Blended delta {_pct(dm.get('blendedDelta'))} "
        f"(floor {_pct(dm.get('hardFloor'))} · target {_pct(dm.get('target'))})",
        f"Greeks — Δ {_usd(greeks.get('delta'), 0)} · Γ {_usd(greeks.get('gamma'))} "
        f"· V {_usd(greeks.get('vega'))} · Θ {_usd(greeks.get('theta'))}/day",
        f"P&L {_money(pnl.get('total'))}  (premium {_money(pnl.get('realized'))} "
        f"· MtM {_money(pnl.get('unrealized'))})",
        f"Available to write: {_n0(cov.get('availableToWrite'))} {base} "
        f"({_n0(cov.get('ccReserved'))} reserved of {_n0(cov.get('totalCcHeld'))} held)",
        f"Roll: {roll.get('statusText') or '—'}",
    ]

    last_error = state.get("lastError")
    if last_error:
        lines.append(f":rotating_light: last poll error: {last_error.get('message', last_error)}")

    open_issues = [r for r in rules if r["state"] != "good"]
    if open_issues:
        lines.append(f"🚨 *{len(open_issues)} open issue(s):*")
        for r in open_issues:
            bullet = "🔴" if r["state"] == "critical" else "🟠"
            lines.append(f"  {bullet} *{r['name']}* — {_explain_issue(r, snap)}")
    else:
        lines.append("✅ no open issues")

    _dispatch("\n".join(lines))