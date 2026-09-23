"""Background poller for the CallSpread dashboard.

    python manage.py callspread_poll            # loop forever at the configured interval
    python manage.py callspread_poll --once     # single poll, useful from cron or a test

Runs as its own process (deploy/callspread-poll.service) rather than a thread in
the web app: with several gunicorn workers, an in-process timer would poll and
append history once per worker.
"""

import signal
import time

from django.core.management.base import BaseCommand

from callspread.runtime import get_runtime


class Command(BaseCommand):
    help = "Poll STS and publish the CallSpread snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Poll once and exit.")
        parser.add_argument("--interval", type=float, default=None,
                            help="Override the poll interval in seconds.")

    def handle(self, *args, **options):
        runtime = get_runtime()
        interval = options["interval"] or runtime.config["polling"]["intervalSeconds"]
        interval = max(10, float(interval))

        if runtime.live:
            self.stdout.write(self.style.SUCCESS(
                f"[mode] LIVE — {runtime.client.api_url}"))
        else:
            self.stdout.write(self.style.WARNING(
                "[mode] DEMO — no STS_CLIENT_ID/STS_CLIENT_SECRET in the environment"))
            seeded = runtime.backfill_demo_history()
            if seeded:
                self.stdout.write(f"[demo] backfilled {seeded} history points (7d @ 15m)")

        stopping = {"now": False}

        def _stop(signum, frame):
            stopping["now"] = True
            self.stdout.write("\nstopping after the current poll…")

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        snap = runtime.poll()
        self._report(runtime, snap)
        if options["once"]:
            return

        self.stdout.write(f"polling every {interval:g}s — Ctrl-C to stop")
        while not stopping["now"]:
            # Sleep in short slices so a stop signal is honoured promptly rather
            # than after a whole interval.
            waited = 0.0
            while waited < interval and not stopping["now"]:
                time.sleep(min(1.0, interval - waited))
                waited += 1.0
            if stopping["now"]:
                break
            self._report(runtime, runtime.poll())

    def _report(self, runtime, snap):
        if runtime.last_error:
            self.stdout.write(self.style.ERROR(f"  poll failed: {runtime.last_error['message']}"))
            return
        if not snap:
            return
        blended = snap["deltaMonitor"]["blendedDelta"]
        blended_text = f"{blended * 100:.2f}%" if blended is not None else "—"
        self.stdout.write(
            f"  {snap['asOf']}  spot={snap['spot']:.6g}  blended={blended_text}"
            f"  pnl={snap['pnl']['total']:+.2f}"
        )