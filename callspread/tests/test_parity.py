"""Pins the ported maths to what the original Node implementation produced.

`node_input.json` is a real STS positions/trades payload; `node_expected.json`
is the snapshot, history point and alert rules the Node `buildSnapshot()` built
from it (account identifiers redacted, numbers untouched). If a change to
model.py moves any figure, this fails and names the field — which is the whole
point of keeping the fixture rather than trusting the translation.

To recapture after an intentional change to the maths, re-run the harness
against the Node source; see README, "Porting notes".
"""

import json
import math
from pathlib import Path

from django.test import SimpleTestCase

from callspread.alerts import evaluate_rules
from callspread.model import build_snapshot, to_history_point

FIXTURES = Path(__file__).resolve().parent
TOL = 1e-12


def _load(name):
    with (FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


class NodeParityTests(SimpleTestCase):
    """Every field of the snapshot must equal the Node output, not merely be close."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data = _load("node_input.json")
        cls.expected = _load("node_expected.json")
        extras = {"asOf": cls.data["asOf"], "source": "live", "trades": cls.data["trades"]}
        cls.snapshot = build_snapshot(cls.data["positions"], cls.data["config"], extras)
        cls.history = to_history_point(cls.snapshot)
        cls.rules = evaluate_rules(cls.snapshot, cls.data["config"])

    def _compare(self, path, expected, actual):
        if isinstance(expected, bool) or isinstance(actual, bool):
            self.assertEqual(bool(expected), bool(actual), path)
        elif expected is None or actual is None:
            self.assertIs(expected, actual, f"{path}: {expected!r} vs {actual!r}")
        elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            self.assertTrue(
                math.isclose(expected, actual, rel_tol=TOL, abs_tol=TOL),
                f"{path}: node={expected!r} python={actual!r}")
        elif isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            self.assertEqual(set(expected), set(actual), f"{path}: key sets differ")
            for key in expected:
                self._compare(f"{path}.{key}", expected[key], actual[key])
        elif isinstance(expected, list):
            self.assertIsInstance(actual, list, path)
            self.assertEqual(len(expected), len(actual), f"{path}: length differs")
            for i, (e, a) in enumerate(zip(expected, actual)):
                self._compare(f"{path}[{i}]", e, a)
        else:
            self.assertEqual(expected, actual, path)

    def test_snapshot_matches_node(self):
        self._compare("snapshot", self.expected["snapshot"], self.snapshot)

    def test_history_point_matches_node(self):
        self._compare("history", self.expected["history"], self.history)

    def test_alert_rules_match_node(self):
        self._compare("rules", self.expected["rules"], self.rules)

    def test_key_figures_are_present(self):
        """A guard against the fixture silently becoming all-nulls."""
        self.assertGreater(self.snapshot["spot"], 0)
        self.assertEqual(len(self.snapshot["legs"]), 2)
        self.assertIsNotNone(self.snapshot["deltaMonitor"]["blendedDelta"])
        self.assertEqual(self.snapshot["pnl"]["realizedSource"], "trades")