"""Polling runtime — the part of the original `src/server.js` that isn't HTTP.

The Node app kept the latest snapshot in a module variable because it was a
single process. Here the poller (`manage.py callspread_poll`) and the web
workers are separate processes, so the latest snapshot is written to
`latest.json` and read back by whoever serves the request. That is also what
makes the dashboard survive a web restart with data already on screen.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from callspread import config as config_module
from callspread.alerts import process_alerts
from callspread.demo import DEMO_META, demo_positions, demo_spot_path, demo_trades
from callspread.model import build_snapshot, now_iso, parse_date, to_history_point
from callspread.store import HistoryStore
from callspread.sts import StsClient, StsError, client_from_env

log = logging.getLogger(__name__)

_runtime = None
_runtime_lock = threading.Lock()


def get_runtime():
    """Process-wide singleton. Built lazily so importing the app is cheap."""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = Runtime()
    return _runtime


class Runtime:
    def __init__(self):
        conf = settings.CALLSPREAD
        self.data_dir = Path(conf["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.client: StsClient = client_from_env()
        self.live = self.client.configured and not conf["force_demo"]

        self.config = config_module.load()

        # Demo and live history are kept in separate files — synthetic points
        # must never contaminate the real series once credentials are added.
        name = "history.jsonl" if self.live else "history-demo.jsonl"
        self.store = HistoryStore(
            self.data_dir / name,
            retention_days=self.config["polling"]["historyRetentionDays"],
        )
        self.state_file = self.data_dir / ("latest.json" if self.live else "latest-demo.json")

        self.rules: list = []
        self.last_error = None
        self.last_poll_at = None
        self._demo_spot = DEMO_META["seedSpot"]
        self._poll_lock = threading.Lock()

    # --- persisted cross-process state --------------------------------------

    @property
    def mode(self) -> str:
        return "live" if self.live else "demo"

    def read_state(self) -> dict:
        """Latest snapshot + poll metadata, as last written by any process."""
        try:
            with self.state_file.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, snapshot, rules, last_poll_at, last_error):
        tmp = self.state_file.with_suffix(".json.tmp")
        payload = {"snapshot": snapshot, "rules": rules,
                   "lastPollAt": last_poll_at, "lastError": last_error}
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(self.state_file)

    # --- polling -------------------------------------------------------------

    def _fetch_book(self):
        if not self.live:
            return demo_positions(spot=self._step_demo_spot()), demo_trades()

        # Trades give the realized (premium-exchanged) half of the P&L split. A
        # trades failure must not take down the whole poll — realized falls back
        # to the unconverted-USDT proxy inside build_snapshot.
        positions = self.client.positions()
        try:
            trades = self.client.trades(limit=1000)
        except (StsError, Exception) as exc:  # noqa: BLE001 - any failure degrades gracefully
            log.warning("trades unavailable, falling back to USDT proxy: %s", exc)
            trades = []
        return positions, trades

    def _step_demo_spot(self) -> float:
        import math
        import random
        # ~52% annualised vol on a 60s step, mean-reverting gently to the seed.
        dt = 60 / (365 * 86_400)
        sigma = 0.52 * math.sqrt(dt)
        z = math.sqrt(-2 * math.log(random.random() or 1e-9)) * math.cos(2 * math.pi * random.random())
        drift = 0.02 * math.log(DEMO_META["seedSpot"] / self._demo_spot)
        self._demo_spot = self._demo_spot * math.exp(sigma * z + drift)
        return self._demo_spot

    def poll(self, persist: bool = True):
        """Refresh from STS, evaluate alerts, publish state. Never raises."""
        with self._poll_lock:
            # Re-read config first. The Settings panel saves from a web worker,
            # but the poller is a separate long-lived process: without this its
            # in-memory copy stays at whatever it booted with and every tick
            # overwrites latest.json with stale holdings and thresholds, undoing
            # the save on screen roughly a minute later.
            self.reload_config()
            try:
                positions, trades = self._fetch_book()
                snap = build_snapshot(positions, self.config,
                                      {"source": self.mode, "trades": trades})
                snap["mode"] = self.mode
                self.last_error = None
                self.last_poll_at = now_iso()
                self.rules = process_alerts(snap, self.config)["rules"]
                if persist:
                    self.store.append(to_history_point(snap))
                self._write_state(snap, self.rules, self.last_poll_at, None)
                return snap
            except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the loop
                self.last_error = {"message": str(exc), "at": now_iso()}
                log.error("poll failed: %s", exc)
                state = self.read_state()
                self._write_state(state.get("snapshot"), state.get("rules", []),
                                  state.get("lastPollAt"), self.last_error)
                return state.get("snapshot")

    def backfill_demo_history(self):
        """Seed 7 days of synthetic history so the demo charts aren't empty."""
        if self.store.points:
            return 0
        end_ms = datetime.now(timezone.utc).timestamp() * 1000
        path = demo_spot_path(end_spot=self._demo_spot, end_time_ms=end_ms,
                              points=7 * 96, step_ms=15 * 60_000)
        trades = demo_trades()
        points = []
        for step in path:
            when = parse_date(step["t"])
            snap = build_snapshot(
                demo_positions(spot=step["spot"], now=when),
                self.config,
                {"asOf": step["t"], "source": "demo", "trades": trades},
            )
            points.append(to_history_point(snap))
        self.store.reset(points)
        return len(points)

    def reload_config(self):
        self.config = config_module.load()
        return self.config