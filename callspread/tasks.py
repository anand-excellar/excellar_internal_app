"""Huey periodic tasks for the Covered Call Spread Risk Monitor.

Runs on the same Huey consumer as NAV's scheduled tasks (deploy/excellar-
scheduler.service) — no separate process needed. This reads whatever the
independent callspread_poll process last wrote (Runtime.read_state()), so a
missed heartbeat here never triggers an extra STS call.
"""
import logging

from huey import crontab
from huey.contrib.djhuey import db_periodic_task

logger = logging.getLogger(__name__)


@db_periodic_task(crontab(minute="0", hour="*/6"))
def send_callspread_heartbeat():
    from callspread.alerts import send_heartbeat
    from callspread.runtime import get_runtime

    try:
        runtime = get_runtime()
        send_heartbeat(runtime.read_state(), runtime)
    except Exception as exc:
        logger.error("CallSpread status heartbeat failed: %s", exc, exc_info=True)
