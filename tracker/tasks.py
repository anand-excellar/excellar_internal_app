"""Huey periodic tasks for snapshot collection and daily finalization."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from huey.contrib.djhuey import db_periodic_task, lock_task
from huey import crontab

logger = logging.getLogger(__name__)

_interval = settings.NAV_SNAPSHOT_INTERVAL_MINUTES
_hour = settings.NAV_DAILY_PNL_HOUR
_minute = settings.NAV_DAILY_PNL_MINUTE
_report_hour_et = settings.NAV_REPORT["hour_et"]


# A cycle slower than its own interval would otherwise queue a second copy
# behind the first, forever — the backlog grows unboundedly and the daily
# report/heartbeat starve behind thousands of stale snapshot tasks.
#
# expires drops a tick that could not start within its own window: a snapshot
# is only worth collecting *now*, so a late one is waste, not work owed. That
# bounds the queue on its own. The lock additionally stops two cycles
# overlapping if the consumer is ever run with more than one worker
# (run_huey passes --flush-locks so a killed worker cannot wedge it).
@db_periodic_task(crontab(minute=f"*/{_interval}"), expires=_interval * 60 - 5)
@lock_task("nav-snapshot-cycle")
def run_snapshot_cycle():
    from tracker.services.collector import collect_snapshot_cycle
    try:
        row_ids = asyncio.run(collect_snapshot_cycle())
        logger.info("Snapshot cycle complete: %d snapshots stored", len(row_ids))
    except Exception as exc:
        logger.error("Snapshot cycle failed: %s", exc, exc_info=True)


@db_periodic_task(crontab(hour=str(_hour), minute=str(_minute)))
def run_daily_finalization():
    from tracker.services.collector import finalize_daily_all_segments
    try:
        finalize_daily_all_segments()
    except Exception as exc:
        logger.error("Daily finalization failed: %s", exc, exc_info=True)


# Fires at the top of every hour but only acts at the configured Eastern hour
# (default 5 AM ET). Guarding on America/New_York time keeps it correct across
# DST and independent of the Huey consumer's own clock/timezone.
@db_periodic_task(crontab(minute="0"))
def run_nav_report_export():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour != _report_hour_et:
        return
    from tracker.services.nav_report import build_report
    from tracker.services.gsheets import export_report_to_drive
    from tracker.services.slack_alert import send_export_failure, send_reconciliation_report
    try:
        report = build_report()
    except Exception as exc:
        logger.error("NAV catalog report build failed: %s", exc, exc_info=True)
        send_export_failure(exc)
        return

    # NAV Fund Services reconciliation is a bonus, not a dependency: their API
    # being down must not stop the report the rest of the day relies on. $1 per
    # component, pinned here rather than left to navfund.recon's own default, so
    # the daily alert's threshold can't drift out from under it independently.
    recon = None
    try:
        from navfund.client import load_credentials_env
        from navfund.recon import reconcile_report

        load_credentials_env()
        recon = reconcile_report(report, threshold_usd=1.0)
    except Exception as exc:
        logger.warning("NAV Fund Services reconciliation skipped: %s", exc)

    try:
        url = export_report_to_drive(report, interactive=False, recon=recon)
        logger.info("NAV catalog report exported: %s", url)
        send_reconciliation_report(report, url, recon=recon)
    except Exception as exc:
        logger.error("NAV catalog report export failed: %s", exc, exc_info=True)
        send_export_failure(exc)
