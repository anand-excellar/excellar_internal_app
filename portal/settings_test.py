"""Test settings: everything real except the task queue and outbound alerts.

MemoryHuey(immediate=True) runs scheduled work inline instead of needing a Redis
to be up, so the suite has no external dependency. Blanking the Slack webhooks
here — after NAV_REPORT/CALLSPREAD are already built from the real .env — means
a test run can never post to a real channel, however many snapshots or state
transitions a test drives through the alert/report code.
"""
from portal.settings import *  # noqa: F401,F403

import huey

HUEY = huey.MemoryHuey("excellar", immediate=True)

NAV_REPORT = {**NAV_REPORT, "slack_webhook_url": ""}
CALLSPREAD = {**CALLSPREAD, "slack_webhook_url": ""}
