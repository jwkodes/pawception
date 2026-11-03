import copy
import json
from pathlib import Path
import unittest

from pawception.replay import build_replay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).resolve().parents[1] / "data/fixtures/temporal_demo.json").read_text())

    def test_default_replay_uses_engine_and_does_not_reveal_future_alerts(self):
        run = build_replay(self.fixture)
        self.assertEqual(run["summary"]["alerts"], 4)
        self.assertEqual([len(s["alerts"]) for s in run["snapshots"]], [0, 1, 1, 2, 2, 3, 3, 3, 4])
        self.assertEqual([(a["at"], a["detected_at"]) for a in run["snapshots"][-1]["alerts"]], [(10,12),(24,30),(49,50),(72,80)])
        self.assertNotIn("chew_03", run["snapshots"][1]["available_observations"])

    def test_rerun_is_fresh_and_does_not_mutate_fixture(self):
        original = copy.deepcopy(self.fixture)
        run = build_replay(self.fixture, {"threshold": 4})
        self.assertEqual(run["summary"]["alerts"], 2)
        self.assertEqual(self.fixture, original)
        self.assertEqual(build_replay(self.fixture), build_replay(self.fixture))

    def test_gap_toggle_changes_coverage_and_uncertainty(self):
        normal = build_replay(self.fixture)
        changed = build_replay(self.fixture, {"include_gap": False})
        water = lambda s: next(e for e in s["evaluations"] if e["kind"] == "missing")
        self.assertEqual(water(normal["snapshots"][2])["state"], "unknown")
        self.assertEqual(water(changed["snapshots"][2])["state"], "satisfied")
        self.assertEqual(water(changed["snapshots"][-1])["coverage"], [{"start": 0, "end": 80}])
        self.assertEqual(water(normal["snapshots"][-1])["coverage"], [{"start":0,"end":15},{"start":20,"end":80}])

    def test_missing_window_changes_alert_deadline(self):
        run = build_replay(self.fixture, {"missing_window_seconds": 20})
        alerts = [a for a in run["snapshots"][-1]["alerts"] if a["rule_id"] == "water_visit"]
        self.assertEqual([(a["at"],a["detected_at"]) for a in alerts], [(59,60)])

    def test_invalid_settings_rejected(self):
        for settings in ([], {"threshold":True}, {"threshold":1.5}, {"unknown":1}, {"include_gap":1}, {"merge_gap_seconds":float("nan")}, {"missing_window_seconds":0}, {"repeated_window_seconds":float("inf")}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                build_replay(self.fixture, settings)


if __name__ == "__main__":
    unittest.main()
