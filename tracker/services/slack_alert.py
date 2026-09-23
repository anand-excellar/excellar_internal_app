"""Slack delivery for the daily NAV reconciliation report.

Posts to an incoming webhook (``NAV_REPORT["slack_webhook_url"]``), the same
plain-``requests`` transport already used for delivery in
``callspread/alerts.py``. No webhook configured means no post — the Google
Sheet export this rides on is unaffected either way.
"""
import logging
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _post(text: str) -> None:
    url = settings.NAV_REPORT.get("slack_webhook_url")
    if not url:
        logger.info("SLACK_WEBHOOK_URL not set — skipping Slack post.")
        return
    try:
        response = requests.post(url, json={"text": text}, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Slack post failed: %s", exc)


_MAX_BREACH_ROWS = 5


def _breach_lines(result: dict) -> list:
    """One line per flagged component, capped so a bad day can't blow up the post."""
    from navfund.recon import BOTH

    flagged = [r for r in result.get("rows", []) if r.get("flagged")]
    lines = []
    for r in flagged[:_MAX_BREACH_ROWS]:
        tag = "" if r["presence"] == BOTH else f" [{r['presence']}]"
        lines.append(f"    – {r['category']} / {r['location']} / {r['asset']}: "
                     f"${r['diff_usd']:+,.2f}{tag}")
    if len(flagged) > _MAX_BREACH_ROWS:
        lines.append(f"    – …and {len(flagged) - _MAX_BREACH_ROWS} more")
    return lines


def send_reconciliation_report(report: dict, sheet_url: str, recon: dict | None = None) -> None:
    """Post the day's per-segment NAV breakdown with a link to the sheet it came from.

    ``recon`` (from ``navfund.recon.reconcile_report``, $1 per-component threshold)
    appends each segment's diff against NAV Fund Services. A segment with any
    component over that threshold is called out as a Reconciliation issue, with
    the breaching components listed so the alert is actionable, not just a count.
    """
    date_et = report["generated_at"].astimezone(ET).strftime("%Y-%m-%d")
    lines = [f"*Daily NAV Reconciliation — {date_et}*"]
    for s in report["segments"]:
        seg = s["segment"]
        if not s.get("has_data"):
            lines.append(f"• {seg}: _no data_")
            continue

        line = f"• {seg}: ${s['net_usd']:,.2f}"
        result = (recon or {}).get(seg)
        if result and result.get("error"):
            line += "  (NAV Fund Services reconciliation failed)"
        elif result and result["flagged_count"]:
            line += (f"  — :warning: *Reconciliation issue*: diff ${result['diff_total_usd']:+,.2f} "
                      f"vs NAV fund, {result['flagged_count']} component "
                      f"break(s) over ${result['threshold_usd']:g}")
            lines.append(line)
            lines.extend(_breach_lines(result))
            continue
        elif result:
            line += (f"  — reconciled vs NAV fund (diff ${result['diff_total_usd']:+,.2f}, "
                      f"no component over ${result['threshold_usd']:g})")
        lines.append(line)

    lines.append(f"*Total NAV:* ${report['total_nav_usd']:,.2f}")
    lines.append(f"<{sheet_url}|Open reconciliation sheet>")
    _post("\n".join(lines))


def send_export_failure(exc: Exception) -> None:
    """Flag a failed run so a broken daily job is noticed the same day, not days later."""
    _post(f":rotating_light: Daily NAV reconciliation report failed: `{exc}`")
