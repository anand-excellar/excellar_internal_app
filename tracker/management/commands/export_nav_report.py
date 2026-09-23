"""Build the per-xltoken NAV catalog report and push it to Google Sheets/Drive.

On-demand ("return it right now") entry point — the same code the 5 AM ET
scheduled task runs. Examples::

    # Build from the latest stored snapshot and push to the Google Sheet now:
    python manage.py export_nav_report

    # One-time interactive Google authorization (creates the token, then exits).
    # Needed only if the shared token has no Drive+Sheets scopes yet:
    python manage.py export_nav_report --authorize

    # Preview the numbers without touching Google:
    python manage.py export_nav_report --dry-run
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Build the per-xltoken NAV catalog report and push it to Google Sheets/Drive."

    def add_arguments(self, parser):
        parser.add_argument(
            "--authorize", action="store_true",
            help="Run the interactive Google OAuth flow once to create/refresh the "
                 "token, then exit. Run this once before relying on the schedule.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Build the report and print a summary to the console; do not touch Google.",
        )
        parser.add_argument(
            "--reconcile", action="store_true",
            help="Add NAV Fund Services' own figures and the differences as extra "
                 "Detail-tab columns.",
        )
        parser.add_argument(
            "--date", metavar="MM-DD-YYYY", default=None,
            help="NAV's report date to reconcile against (implies --reconcile). "
                 "Our side is rebuilt from the snapshot current when NAV published "
                 "that date — 5 AM ET the following day — and the result is written "
                 "into that day's spreadsheet.",
        )
        parser.add_argument(
            "--threshold", type=float, default=None,
            help="Flag differences whose USD equivalent exceeds this (default 1.0).",
        )
        parser.add_argument(
            "--show-detail", action="store_true",
            help="Also print the full Detail tab exactly as it would be written "
                 "(implies --dry-run unless you also pass a write). Useful for "
                 "inspecting the rows without publishing anything.",
        )

    def handle(self, *args, **options):
        if options["authorize"]:
            from tracker.services.gsheets import get_credentials
            get_credentials(interactive=True)
            self.stdout.write(self.style.SUCCESS("Google authorization complete — token saved."))
            return

        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from tracker.services.nav_report import build_report

        reconcile = options["reconcile"] or bool(options["date"])
        nav_date = options["date"]
        target_date_et = None
        as_of = None

        if nav_date:
            try:
                day = datetime.strptime(nav_date, "%m-%d-%Y").date()
            except ValueError:
                raise CommandError(f"--date must be MM-DD-YYYY, got {nav_date!r}")
            # NAV publishes a date's figures at 5 AM ET the next morning, so the
            # comparable state of our book is the snapshot current at that moment
            # — not today's positions, which would fold in days of drift.
            et = ZoneInfo("America/New_York")
            published = datetime.combine(day + timedelta(days=1),
                                         datetime.min.time(), tzinfo=et).replace(hour=5)
            as_of = published
            target_date_et = published.strftime("%Y-%m-%d")

        report = build_report(as_of=as_of)

        self.stdout.write(
            f"NAV report built: {len(report['segments'])} segment(s), "
            f"total NAV ${report['total_nav_usd']:,.2f}"
        )
        for s in report["segments"]:
            if s.get("has_data"):
                stamp = s.get("timestamp")
                using = f"  (snapshot {stamp:%Y-%m-%d %H:%M} UTC)" if stamp else ""
                self.stdout.write(f"  {s['segment']:<8} ${s['net_usd']:,.2f}{using}")
            else:
                self.stdout.write(f"  {s['segment']:<8} (no data)")

        # A snapshot far from the moment we asked for makes the comparison
        # meaningless: the difference is mostly elapsed time, not a break. This
        # happens whenever collection was down over the target date, so say it
        # loudly rather than letting the numbers imply a like-for-like match.
        if as_of is not None:
            for s in report["segments"]:
                stamp = s.get("timestamp")
                if not stamp:
                    continue
                gap = as_of - stamp
                if gap > timedelta(hours=24):
                    self.stdout.write(self.style.WARNING(
                        f"  {s['segment']:<8} WARNING: nearest snapshot is "
                        f"{gap.days}d {gap.seconds // 3600}h older than the "
                        f"{as_of:%Y-%m-%d %H:%M %Z} target — differences below are "
                        f"mostly drift, not breaks."))

        # Publishing a report with no data creates a junk spreadsheet that looks
        # like a real day. Almost always a mistyped --date, so say what is
        # actually available rather than just refusing.
        if not any(s.get("has_data") for s in report["segments"]):
            from tracker.models import Snapshot

            span = Snapshot.objects.order_by("timestamp").values_list("timestamp", flat=True)
            earliest, latest = span.first(), span.last()
            detail = (f"Snapshots run {earliest:%Y-%m-%d %H:%M} to {latest:%Y-%m-%d %H:%M} UTC."
                      if earliest else "There are no snapshots in the database at all.")
            asked = f" for --date {nav_date}" if nav_date else ""
            raise CommandError(
                f"No segment has snapshot data{asked}, so there is nothing to report "
                f"— refusing to publish an empty spreadsheet. {detail}"
                + (f" Our side is taken from the snapshot current at "
                   f"{as_of:%Y-%m-%d %H:%M %Z} (5 AM ET the day after the NAV date)."
                   if as_of else ""))

        recon = None
        if reconcile:
            from navfund.client import load_credentials_env
            from navfund.recon import DEFAULT_THRESHOLD_USD, reconcile_report

            load_credentials_env()
            threshold = options["threshold"] or DEFAULT_THRESHOLD_USD
            recon = reconcile_report(report, report_date=nav_date, threshold_usd=threshold)
            for segment, result in sorted(recon.items()):
                if result.get("error"):
                    self.stdout.write(self.style.ERROR(
                        f"  {segment:<8} reconciliation failed: {result['error'][:160]}"))
                    continue
                self.stdout.write(
                    f"  {segment:<8} NAV {result['report_date']}  "
                    f"diff ${result['diff_total_usd']:+,.2f}  "
                    f"{result['flagged_count']} break(s) over ${threshold:g}")
            missing = [s["segment"] for s in report["segments"]
                       if s.get("has_data") and s["segment"] not in recon]
            if missing:
                self.stdout.write(self.style.WARNING(
                    f"  not reconciled (absent from account_map.yaml): {', '.join(missing)}"))

        if options["show_detail"]:
            # Reuse the writer's own flattener so what is printed is exactly what
            # would be written — no second, drifting representation of the rows.
            from tracker.services.gsheets import (
                DETAIL_HEADER, RECON_HEADER, _detail_rows_for_segment)

            widths = [8, 24, 30, 16, 24, 16, 11, 14]
            header = list(DETAIL_HEADER)
            if recon is not None:
                widths += [16, 16, 14, 12, 30]
                header += RECON_HEADER

            def cell(value, width):
                # Format numbers before truncating: str() on a small float gives
                # "3.858414119976e-07", and clipping that to the column width
                # silently turns it into 3.858414119976 — a million times larger.
                if isinstance(value, float):
                    text = f"{value:.6g}"
                elif isinstance(value, int):
                    text = str(value)
                else:
                    text = str(value)
                return f"{text[:width]:<{width}}"

            def line(cells):
                padded = (list(cells) + [""] * len(widths))[:len(widths)]
                return " ".join(cell(c, w) for c, w in zip(padded, widths))

            self.stdout.write("\n" + line(header))
            self.stdout.write("-" * (sum(widths) + len(widths)))
            for s in report["segments"]:
                seg_recon = recon.get(s["segment"]) if recon is not None else None
                for row in _detail_rows_for_segment(s["segment"], s, seg_recon):
                    self.stdout.write(line(row))
            self.stdout.write("")

        if options["dry_run"] or options["show_detail"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written to Google."))
            return

        from tracker.services.gsheets import export_report_to_drive
        url = export_report_to_drive(report, interactive=True, recon=recon,
                                     date_et=target_date_et)
        self.stdout.write(self.style.SUCCESS(f"Report written: {url}"))