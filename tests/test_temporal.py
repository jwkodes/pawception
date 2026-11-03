import json
import tempfile
import unittest
from pathlib import Path

from pawception.temporal import Event, Observation, Rule, TemporalEngine, evaluate_rule


def repeat_rule(**overrides):
    return Rule(**{"id": "r", "kind": "repeated", "window_seconds": 30, "threshold": 3, "subject": "golden retriever", "behavior": "chewing sofa", "merge_gap_seconds": 1, **overrides})


def missing_rule(**overrides):
    return Rule(**{"id": "m", "kind": "missing", "window_seconds": 10, "subject": "golden retriever", "behavior": "water bowl visit", **overrides})


def observation(id, start, end, spans=(), outcome=None, rule_id="r", **kwargs):
    events = tuple(Event(left, right, f"mock://{id}/{index}") for index, (left, right) in enumerate(spans))
    return Observation(id, rule_id, start, end, outcome or ("observed" if events else "not_observed"), events, **kwargs)


class RepeatedTests(unittest.TestCase):
    def test_overlapping_reports_count_as_one_episode(self):
        items = [observation("a", 0, 10, [(2, 7)]), observation("b", 5, 15, [(5, 9)])]
        result = evaluate_rule(repeat_rule(), items, 15)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["episodes"][0]["evidence"]), 2)

    def test_continuous_behavior_is_one_episode(self):
        items = [observation("a", 0, 10, [(0, 10)]), observation("b", 10, 20, [(10, 20)])]
        self.assertEqual(evaluate_rule(repeat_rule(), items, 20)["count"], 1)

    def test_three_distinct_episodes_meet_threshold(self):
        items = [observation("a", 0, 30, [(2, 4), (12, 14), (22, 24)])]
        result = evaluate_rule(repeat_rule(), items, 30)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["count"], 3)

    def test_episodes_at_left_boundary_expire(self):
        items = [observation("a", 0, 10, [(0, 1), (8, 9)])]
        result = evaluate_rule(repeat_rule(window_seconds=10, threshold=2), items, 10)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["state"], "clear")

    def test_blind_gap_does_not_create_new_episode(self):
        items = [observation("a", 0, 5, [(2, 4)]), observation("gap", 5, 10, outcome="unknown"), observation("b", 10, 15, [(11, 13)])]
        self.assertEqual(evaluate_rule(repeat_rule(), items, 15)["count"], 1)

    def test_known_pause_can_separate_episodes_despite_a_later_blind_gap(self):
        items = [observation("a", 0, 5, [(1, 2)]),
                 observation("blind", 5, 8, outcome="unknown"),
                 observation("b", 8, 12, [(8, 10)])]
        self.assertEqual(evaluate_rule(repeat_rule(), items, 12)["count"], 2)

    def test_short_visible_pause_inside_blind_gap_does_not_prove_restart(self):
        items = [observation("a", 0, 2.5, [(1, 2)]),
                 observation("blind", 2.5, 8, outcome="unknown"),
                 observation("b", 8, 12, [(8, 10)])]
        self.assertEqual(evaluate_rule(repeat_rule(), items, 12)["count"], 1)

    def test_positive_evidence_can_satisfy_despite_other_coverage_gaps(self):
        items = [observation("a", 10, 30, [(11, 12), (17, 18), (25, 26)])]
        self.assertEqual(evaluate_rule(repeat_rule(), items, 30)["state"], "satisfied")


class MissingTests(unittest.TestCase):
    def evaluate(self, items, at):
        return evaluate_rule(missing_rule(), items, at)

    def test_initial_full_covered_interval_without_any_visit(self):
        items = [observation("a", 0, 5, rule_id="m"), observation("b", 5, 10, rule_id="m")]
        self.assertEqual(self.evaluate(items, 5)["state"], "pending")
        self.assertEqual(self.evaluate(items, 10)["state"], "satisfied")

    def test_visit_resets_timer_to_event_end(self):
        items = [observation("visit", 0, 10, [(4, 6)], rule_id="m"), observation("quiet", 10, 16, rule_id="m")]
        self.assertEqual(self.evaluate(items, 10)["state"], "clear")
        self.assertEqual(self.evaluate(items, 16)["state"], "satisfied")
        self.assertEqual(self.evaluate(items, 16)["absence_since"], 6)

    def test_future_window_cannot_provide_early_visit_or_coverage(self):
        items = [observation("future", 0, 20, [(17, 18)], rule_id="m")]
        result = self.evaluate(items, 10)
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["last_seen"])

    def test_clip_end_or_wall_clock_does_not_prove_absence(self):
        items = [observation("a", 0, 5, rule_id="m")]
        self.assertEqual(self.evaluate(items, 7200)["state"], "unknown")

    def test_unknown_resets_continuous_coverage(self):
        items = [observation("a", 0, 5, rule_id="m"), observation("gap", 5, 10, outcome="unknown", rule_id="m"), observation("b", 10, 15, rule_id="m"), observation("c", 15, 20, rule_id="m")]
        self.assertEqual(self.evaluate(items, 15)["state"], "pending")
        result = self.evaluate(items, 20)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["absence_since"], 10)

    def test_unreported_gap_also_breaks_coverage(self):
        items = [observation("a", 0, 5, rule_id="m"), observation("b", 10, 15, rule_id="m")]
        self.assertEqual(self.evaluate(items, 15)["state"], "pending")

    def test_unknown_overrides_conflicting_negative_coverage(self):
        items = [observation("a", 0, 10, rule_id="m"), observation("gap", 5, 10, outcome="unknown", rule_id="m")]
        self.assertEqual(self.evaluate(items, 10)["state"], "unknown")


class PersistenceTests(unittest.TestCase):
    def test_new_event_at_previous_watermark_keeps_its_actual_onset(self):
        with TemporalEngine() as engine:
            engine.add_rule(repeat_rule(threshold=1))
            engine.ingest(observation("quiet", 0, 2))
            engine.advance(2)
            engine.ingest(observation("new", 2, 4, [(2, 3)]))
            notices = engine.advance(4)["notifications"]
            self.assertEqual([notice["at"] for notice in notices], [2])
            self.assertEqual(engine.advance(4)["notifications"], [])

    def test_restart_and_retry_do_not_duplicate_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            item = observation("a", 0, 10, rule_id="m")
            with TemporalEngine(database) as engine:
                engine.add_rule(missing_rule())
                engine.ingest(item)
                engine.ingest(item)
                self.assertEqual(len(engine.advance(10)["notifications"]), 1)
            with TemporalEngine(database) as engine:
                self.assertEqual(engine.watermark, 10)
                self.assertEqual(engine.advance(10)["notifications"], [])
                self.assertEqual(len(engine.incidents()), 1)

    def test_absence_incident_resolves_on_visit_then_rearms(self):
        with TemporalEngine() as engine:
            engine.add_rule(missing_rule())
            for item in [observation("a", 0, 10, rule_id="m"), observation("visit", 10, 15, [(12, 13)], rule_id="m"), observation("b", 15, 23, rule_id="m")]:
                engine.ingest(item)
            self.assertEqual(len(engine.advance(10)["notifications"]), 1)
            engine.advance(15)
            self.assertEqual(engine.incidents()[0]["status"], "resolved")
            self.assertEqual(len(engine.advance(23)["notifications"]), 1)
            self.assertEqual(len(engine.incidents()), 2)

    def test_coverage_loss_marks_incident_uncertain_without_false_resolution(self):
        with TemporalEngine() as engine:
            engine.add_rule(missing_rule())
            for item in [observation("a", 0, 10, rule_id="m"), observation("gap", 10, 15, outcome="unknown", rule_id="m"), observation("b", 15, 25, rule_id="m")]:
                engine.ingest(item)
            engine.advance(10)
            engine.advance(15)
            self.assertEqual(engine.incidents()[0]["status"], "uncertain")
            self.assertEqual(engine.advance(25)["notifications"], [])
            self.assertEqual(engine.incidents()[0]["status"], "active")

    def test_repeated_incident_resolves_and_can_open_again(self):
        with TemporalEngine() as engine:
            engine.add_rule(repeat_rule(window_seconds=10, threshold=2))
            for item in [observation("a", 0, 10, [(1, 2), (6, 7)]), observation("b", 10, 20), observation("c", 20, 30, [(21, 22), (26, 27)])]:
                engine.ingest(item)
            self.assertEqual(len(engine.advance(10)["notifications"]), 1)
            engine.advance(20)
            self.assertEqual(engine.incidents()[0]["status"], "resolved")
            self.assertEqual(len(engine.advance(30)["notifications"]), 1)

    def test_late_results_and_backward_watermarks_are_rejected(self):
        with TemporalEngine() as engine:
            engine.add_rule(missing_rule())
            engine.ingest(observation("a", 0, 10, rule_id="m"))
            engine.advance(10)
            with self.assertRaisesRegex(ValueError, "Late"):
                engine.ingest(observation("late", 5, 15, rule_id="m"))
            with self.assertRaisesRegex(ValueError, "monotonic"):
                engine.advance(9)

    def test_conflicting_ids_rules_and_streams_are_rejected(self):
        with TemporalEngine() as engine:
            engine.add_rule(repeat_rule())
            engine.ingest(observation("a", 0, 10))
            with self.assertRaises(ValueError):
                engine.ingest(observation("a", 0, 11))
            with self.assertRaises(ValueError):
                engine.add_rule(repeat_rule(threshold=2))
            with self.assertRaises(ValueError):
                engine.ingest(observation("other", 0, 10, stream_id="camera_2"))

    def test_fixture_produces_expected_alerts(self):
        fixture = json.loads((Path(__file__).parents[1] / "data/fixtures/temporal_demo.json").read_text())
        with TemporalEngine() as engine:
            for rule in fixture["rules"]:
                engine.add_rule(Rule(**rule))
            for item in fixture["observations"]:
                engine.ingest(Observation.from_dict(item))
            notices = [notice for at in fixture["evaluation_times"] for notice in engine.advance(at)["notifications"]]
            self.assertEqual([(n["rule_id"], n["at"], n["detected_at"]) for n in notices], [("water_visit", 10, 12), ("sofa_chewing", 24, 30), ("water_visit", 49, 50), ("sofa_chewing", 72, 80)])

    def test_pending_overlap_prevents_premature_finalization(self):
        with TemporalEngine() as engine:
            engine.add_rule(missing_rule())
            engine.ingest(observation("known", 0, 10, rule_id="m"))
            engine.ingest(observation("pending", 5, 15, [(6, 7)], rule_id="m"))
            with self.assertRaisesRegex(ValueError, "overlapping window"):
                engine.advance(10)
            self.assertIsNone(engine.watermark)
            self.assertEqual(engine.advance(15)["notifications"], [])

    def test_large_replay_jump_preserves_transient_repetition_crossings(self):
        with TemporalEngine() as engine:
            engine.add_rule(repeat_rule(window_seconds=10, threshold=2))
            engine.ingest(observation("batch", 0, 100, [(1, 2), (4, 5), (31, 32), (34, 35)]))
            report = engine.advance(100)
            self.assertEqual([n["at"] for n in report["notifications"]], [4, 34])
            self.assertTrue(all(n["detected_at"] == 100 for n in report["notifications"]))
            self.assertTrue(all(i["status"] == "resolved" for i in engine.incidents()))

    def test_large_replay_jump_checks_missing_event_deadlines(self):
        with TemporalEngine() as engine:
            engine.add_rule(missing_rule())
            engine.ingest(observation("batch", 0, 60, [(22, 23)], rule_id="m"))
            self.assertEqual([n["at"] for n in engine.advance(60)["notifications"]], [10, 33])

    def test_multiple_rules_report_crossings_in_source_time_order(self):
        fixture = json.loads((Path(__file__).parents[1] / "data/fixtures/temporal_demo.json").read_text())
        with TemporalEngine() as engine:
            for rule in fixture["rules"]:
                engine.add_rule(Rule(**rule))
            for item in fixture["observations"]:
                engine.ingest(Observation.from_dict(item))
            notices = engine.advance(80)["notifications"]
            self.assertEqual([n["at"] for n in notices], [10, 24, 49, 72])
            self.assertTrue(all(n["observation_ids"] and n["coverage"] for n in notices))

    def test_notification_intent_survives_restart_until_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "nested/state.sqlite3"
            with TemporalEngine(database) as engine:
                engine.add_rule(missing_rule())
                engine.ingest(observation("a", 0, 10, rule_id="m"))
                notice = engine.advance(10)["notifications"][0]
            with TemporalEngine(database) as engine:
                self.assertEqual(engine.pending_notifications(), [notice])
                engine.acknowledge_notification(notice["incident_id"])
                engine.acknowledge_notification(notice["incident_id"])
                self.assertEqual(engine.pending_notifications(), [])


class ValidationTests(unittest.TestCase):
    def test_invalid_observations_and_rules_fail_before_storage(self):
        with self.assertRaises(ValueError):
            observation("bad", 0, 10, [(9, 12)])
        with self.assertRaises(ValueError):
            observation("bad", 0, 10, [(1, 2)], outcome="unknown")
        with self.assertRaises(ValueError):
            repeat_rule(window_seconds=float("nan"))
        with self.assertRaises(ValueError):
            Event(1, 2, "")


if __name__ == "__main__":
    unittest.main()
