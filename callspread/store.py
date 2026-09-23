"""Append-only JSONL snapshot history — port of the original `src/store.js`.

One line per poll, kept in memory as well so range queries never touch disk.
The on-disk format is unchanged, so the history written by the Node version
loads here as-is.
"""

import json
import os
import threading
from datetime import datetime, timezone

from callspread.model import parse_date

_SPANS_MS = {
    "1h": 3.6e6,
    "6h": 2.16e7,
    "24h": 8.64e7,
    "7d": 6.048e8,
    "30d": 2.592e9,
}


def _epoch_ms(iso_text) -> float:
    dt = parse_date(iso_text)
    return dt.timestamp() * 1000 if dt else 0.0


class HistoryStore:
    def __init__(self, file: str, retention_days: float = 30):
        self.file = str(file)
        self.retention_ms = retention_days * 86_400_000
        self.points: list = []
        # The web process and the poller can both touch this; a lock keeps an
        # append from interleaving with a prune rewrite.
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.file) or ".", exist_ok=True)
        self._load()

    def _now_ms(self) -> float:
        return datetime.now(timezone.utc).timestamp() * 1000

    def _load(self):
        if not os.path.exists(self.file):
            return
        cutoff = self._now_ms() - self.retention_ms
        with open(self.file, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    p = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a hard kill — skip it rather than
                    # refuse to boot.
                    continue
                if _epoch_ms(p.get("t")) >= cutoff:
                    self.points.append(p)
        self.points.sort(key=lambda p: _epoch_ms(p.get("t")))

    def append(self, point: dict) -> dict:
        with self._lock:
            self.points.append(point)
            with open(self.file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(point) + "\n")
            self._prune()
        return point

    def reset(self, points: list):
        """Bulk seed (demo backfill) — replaces whatever is on disk."""
        with self._lock:
            self.points = sorted(points, key=lambda p: _epoch_ms(p.get("t")))
            self._rewrite()

    def _rewrite(self):
        with open(self.file, "w", encoding="utf-8") as fh:
            for p in self.points:
                fh.write(json.dumps(p) + "\n")

    def _prune(self):
        cutoff = self._now_ms() - self.retention_ms
        if self.points and _epoch_ms(self.points[0].get("t")) < cutoff:
            self.points = [p for p in self.points if _epoch_ms(p.get("t")) >= cutoff]
            self._rewrite()

    def reload(self):
        """Re-read from disk — the web process uses this to pick up the poller's writes."""
        with self._lock:
            self.points = []
            self._load()

    def range(self, range_key: str = "24h", max_points: int = 400) -> list:
        """:param range_key: '1h' | '6h' | '24h' | '7d' | '30d' | 'all'
        :param max_points: downsample target (keeps first & last)
        """
        pts = self.points
        if range_key != "all" and range_key in _SPANS_MS:
            cutoff = self._now_ms() - _SPANS_MS[range_key]
            pts = [p for p in pts if _epoch_ms(p.get("t")) >= cutoff]
        if len(pts) <= max_points:
            return pts
        step = len(pts) / max_points
        out = [pts[int(i * step)] for i in range(max_points - 1)]
        out.append(pts[-1])
        return out

    @property
    def latest(self):
        return self.points[-1] if self.points else None