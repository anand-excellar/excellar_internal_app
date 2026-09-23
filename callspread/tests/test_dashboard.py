"""The CallSpread dashboard's own behaviour: auth, endpoints, store, demo book."""

import json
import re
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from callspread.demo import demo_positions, demo_spot_path, demo_trades, mulberry32
from callspread.model import build_snapshot, iso, parse_date, to_history_point
from callspread.store import HistoryStore

CONFIG = {
    "book": {"baseCurrency": "CC", "quoteCurrency": "USDT", "label": "test"},
    "externalHoldings": {"custodyCcUnits": 9000, "venue": "Custody"},
    "policy": {"coverageRatio": 1, "coverageWarnBuffer": 0.005, "rollDaysBeforeExpiry": 14,
               "rollApproachDays": 3, "hardFloor": 0.7, "warningBuffer": 0.02,
               "target": 0.8, "upperBand": 0.95},
    "polling": {"intervalSeconds": 60, "historyRetentionDays": 30},
    "display": {"quoteSymbol": "$", "spotPrecision": 6},
}


class AccessTests(TestCase):
    """This dashboard had no login of its own as a standalone service."""

    PATHS = ["/d/callspread/", "/d/callspread/api/state", "/d/callspread/api/history",
             "/d/callspread/api/snapshot", "/d/callspread/api/health"]

    def test_every_route_requires_a_session(self):
        anon = Client()
        for path in self.PATHS:
            response = anon.get(path)
            self.assertEqual(response.status_code, 302, f"{path} was not gated")
            self.assertIn("/login/", response.url, path)

    def test_writes_require_a_session(self):
        anon = Client()
        for path in ["/d/callspread/api/refresh", "/d/callspread/api/config"]:
            self.assertEqual(anon.post(path).status_code, 302, path)

    def test_signed_in_user_gets_the_page(self):
        User.objects.create_user("viewer", password="pw-for-tests-1234")
        c = Client()
        c.force_login(User.objects.get(username="viewer"))
        response = c.get("/d/callspread/")
        self.assertEqual(response.status_code, 200)
        # The original app.js must be the script the page loads.
        self.assertContains(response, "callspread/app.js")


class BrowserWriteTests(TestCase):
    """The Settings panel and Refresh button, exercised the way a browser does.

    Both POST. app.js came unchanged from the Node server, which had no CSRF
    protection, so it sends no token of its own — the page template's shim adds
    one. These use enforce_csrf_checks=True because the default test client
    skips CSRF entirely, which is precisely why "Save does nothing" (a silent
    403) reached the browser unnoticed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"

        overrides = override_settings(CALLSPREAD={
            "data_dir": self.tmp.name,
            "config_path": str(self.config_path),
            "force_demo": True,
        })
        overrides.enable()
        self.addCleanup(overrides.disable)

        # The runtime is a process-wide singleton; drop it so it rebuilds
        # against the temp directory instead of the real data dir.
        from callspread import runtime as runtime_module
        runtime_module._runtime = None
        self.addCleanup(setattr, runtime_module, "_runtime", None)

        User.objects.create_user("writer", password="pw-for-tests-1234")
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(User.objects.get(username="writer"))

    def _token(self):
        """The token as the browser gets it — read out of the rendered page."""
        page = self.client.get("/d/callspread/").content.decode()
        match = re.search(r'var TOKEN = "([^"]+)"', page)
        self.assertIsNotNone(match, "the page served no CSRF token to app.js")
        return match.group(1)

    def test_the_page_hands_app_js_a_csrf_token(self):
        self.assertTrue(self._token())

    def test_saving_settings_persists_to_disk(self):
        token = self._token()
        response = self.client.post(
            "/d/callspread/api/config",
            data=json.dumps({"custodyCcUnits": 4242, "hardFloor": 0.55}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)

        # Assert against the file, not the response: the bug was that the write
        # never happened, while the panel closed as though it had.
        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["externalHoldings"]["custodyCcUnits"], 4242)
        self.assertEqual(written["policy"]["hardFloor"], 0.55)

    def test_refresh_button_is_accepted(self):
        response = self.client.post("/d/callspread/api/refresh",
                                    HTTP_X_CSRFTOKEN=self._token())
        self.assertEqual(response.status_code, 200)

    def test_a_write_without_the_token_is_still_rejected(self):
        """The shim must not become a way around CSRF for a forged request."""
        response = self.client.post(
            "/d/callspread/api/config",
            data=json.dumps({"custodyCcUnits": 1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            self.config_path.exists()
            and json.loads(self.config_path.read_text(
                encoding="utf-8"))["externalHoldings"]["custodyCcUnits"] == 1)

    def test_the_poller_does_not_revert_a_saved_setting(self):
        """A save must survive the next poll.

        The poller is a separate process from the web worker that handled the
        save, so it holds its own Runtime with its own copy of the config. It
        used to keep the copy it booted with and overwrite latest.json a minute
        later, which looked exactly like the save being ignored. A single-process
        test cannot see this, so the poller's Runtime is built explicitly here.
        """
        from callspread.runtime import Runtime

        poller = Runtime()                      # boots holding the old value
        original = poller.config["externalHoldings"]["custodyCcUnits"]

        self.client.post(
            "/d/callspread/api/config",
            data=json.dumps({"custodyCcUnits": 7777}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self._token(),
        )
        self.assertNotEqual(original, 7777, "fixture must differ from the saved value")

        poller.poll()                           # the tick that used to revert it

        served = self.client.get("/d/callspread/api/state").json()
        self.assertEqual(served["config"]["externalHoldings"]["custodyCcUnits"], 7777)
        self.assertEqual(poller.config["externalHoldings"]["custodyCcUnits"], 7777)


class DemoBookTests(TestCase):
    def test_demo_book_produces_a_complete_snapshot(self):
        now = parse_date("2026-08-01T12:00:00Z")
        snap = build_snapshot(demo_positions(spot=0.1194, now=now), CONFIG,
                              {"asOf": iso(now), "source": "demo", "trades": demo_trades()})
        self.assertEqual(len(snap["legs"]), 2)
        self.assertEqual(snap["spread"]["contracts"], 10_000)
        self.assertAlmostEqual(snap["spread"]["width"], 0.015, places=9)
        self.assertEqual(snap["pnl"]["realizedSource"], "trades")
        # Sold the 0.135 and bought the 0.15: net premium in.
        self.assertGreater(snap["pnl"]["realized"], 0)

    def test_prng_is_deterministic(self):
        """Two runs of the seeded generator must agree, or history redraws itself."""
        a = mulberry32(20260821)
        b = mulberry32(20260821)
        self.assertEqual([a() for _ in range(5)], [b() for _ in range(5)])

    def test_prng_matches_the_node_implementation(self):
        """The 32-bit arithmetic must reproduce Math.imul exactly.

        Reference values captured by running the original mulberry32(20260821)
        from src/demo.js under Node. If these drift, the demo's synthetic history
        stops being the same series the Node version drew.
        """
        expected = [
            0.7162383929826319, 0.8401872285176069, 0.005741450237110257,
            0.587558510247618, 0.005740602733567357, 0.6638553000520915,
        ]
        rnd = mulberry32(20260821)
        self.assertEqual([rnd() for _ in range(len(expected))], expected)

    def test_spot_path_is_ordered_and_seeded(self):
        pts = demo_spot_path(end_spot=0.12, end_time_ms=1785000000000, points=20,
                             step_ms=900_000)
        self.assertEqual(len(pts), 20)
        times = [parse_date(p["t"]) for p in pts]
        self.assertEqual(times, sorted(times), "history must be oldest-first")
        self.assertAlmostEqual(pts[-1]["spot"], 0.12, places=12)
        again = demo_spot_path(end_spot=0.12, end_time_ms=1785000000000, points=20,
                              step_ms=900_000)
        self.assertEqual([p["spot"] for p in pts], [p["spot"] for p in again])


class HistoryStoreTests(TestCase):
    def _store(self, **kwargs):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return HistoryStore(Path(self.tmp.name) / "history.jsonl", **kwargs)

    def _point(self, when, spot=0.1):
        return {"t": when, "spot": spot}

    def test_append_survives_a_reload(self):
        store = self._store()
        store.append(self._point("2026-08-01T00:00:00.000Z"))
        store.append(self._point("2026-08-01T00:15:00.000Z"))
        store.reload()
        self.assertEqual(len(store.points), 2)

    def test_points_are_sorted_oldest_first(self):
        store = self._store()
        store.reset([self._point("2026-08-02T00:00:00.000Z"),
                     self._point("2026-08-01T00:00:00.000Z")])
        self.assertEqual([p["t"] for p in store.points],
                         ["2026-08-01T00:00:00.000Z", "2026-08-02T00:00:00.000Z"])

    def test_a_torn_line_does_not_stop_the_store_loading(self):
        store = self._store()
        store.append(self._point("2026-08-01T00:00:00.000Z"))
        with open(store.file, "a", encoding="utf-8") as fh:
            fh.write('{"t": "2026-08-01T00:05:00.000Z", "spo')  # killed mid-write
        store.reload()
        self.assertEqual(len(store.points), 1)

    def test_range_downsamples_but_keeps_the_latest(self):
        store = self._store()
        from datetime import datetime, timedelta, timezone
        base = datetime.now(timezone.utc) - timedelta(hours=20)
        store.reset([self._point(iso(base + timedelta(minutes=i)), spot=i)
                     for i in range(1000)])
        got = store.range("24h", 100)
        self.assertEqual(len(got), 100)
        self.assertEqual(got[-1]["spot"], 999, "the newest point must never be dropped")

    def test_range_all_ignores_the_time_window(self):
        store = self._store()
        store.reset([self._point("2020-01-01T00:00:00.000Z")])
        self.assertEqual(len(store.range("all", 400)), 1)
        self.assertEqual(len(store.range("24h", 400)), 0)


class HistoryPointTests(TestCase):
    def test_history_point_carries_the_series_the_chart_reads(self):
        now = parse_date("2026-08-01T12:00:00Z")
        snap = build_snapshot(demo_positions(spot=0.1194, now=now), CONFIG,
                             {"asOf": iso(now), "source": "demo", "trades": demo_trades()})
        point = to_history_point(snap)
        for key in ("t", "spot", "delta", "gamma", "vega", "theta", "blended",
                    "realizedPnl", "unrealizedPnl", "totalPnl", "legs"):
            self.assertIn(key, point)
        self.assertEqual(len(point["legs"]), 2)
        # spotDelta must reconcile the split the chart draws.
        self.assertAlmostEqual(point["delta"] - point["optionsDelta"], point["spotDelta"],
                               places=9)