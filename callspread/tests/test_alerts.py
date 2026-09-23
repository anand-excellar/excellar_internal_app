"""A flat book (no open option legs) must never generate a Slack notification."""

from unittest.mock import patch

from django.test import SimpleTestCase

from callspread import alerts
from callspread.model import build_snapshot

CONFIG = {
    "book": {"baseCurrency": "CC", "quoteCurrency": "USDT", "label": "test"},
    "externalHoldings": {"custodyCcUnits": 9000, "venue": "Custody"},
    "policy": {"coverageRatio": 1, "coverageWarnBuffer": 0.005, "rollDaysBeforeExpiry": 14,
               "rollApproachDays": 3, "hardFloor": 0.7, "warningBuffer": 0.02,
               "target": 0.8, "upperBand": 0.95},
    "polling": {"intervalSeconds": 60, "historyRetentionDays": 30},
    "display": {"quoteSymbol": "$", "spotPrecision": 6},
}


class FlatBookAlertTests(SimpleTestCase):
    def setUp(self):
        alerts._last_state.clear()
        alerts._last_notified.clear()

    def _flat_snapshot(self):
        return build_snapshot([], CONFIG)

    def test_process_alerts_sends_nothing_for_a_flat_book(self):
        snap = self._flat_snapshot()
        self.assertEqual(snap["legs"], [])

        with patch.object(alerts, "deliver") as deliver:
            result = alerts.process_alerts(snap, CONFIG)

        self.assertEqual(result["sent"], [])
        deliver.assert_not_called()

    def test_flat_book_does_not_poison_state_for_the_next_real_position(self):
        """A prior real alert's state must survive a flat interval untouched,
        so the position that reopens is diffed against it, not against a
        flat-book rule state that was never actually alerted on."""
        snap = self._flat_snapshot()
        alerts._last_state["roll-window"] = "critical"

        alerts.process_alerts(snap, CONFIG)

        self.assertEqual(alerts._last_state["roll-window"], "critical")

    def test_heartbeat_stays_silent_for_a_flat_book(self):
        state = {"snapshot": self._flat_snapshot(), "rules": [], "lastPollAt": None}

        with patch.object(alerts, "_dispatch") as dispatch:
            alerts.send_heartbeat(state, runtime=None)

        dispatch.assert_not_called()
