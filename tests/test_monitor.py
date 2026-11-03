import copy
import json
from pathlib import Path
import unittest

from pawception.monitor import monitor_format, validate_observations, validate_task

TASK = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/monitor_task.json").read_text())


def observations():
    return {"assessments": [
        {"rule_id":"bowl_visit", "outcome":"observed", "visibility":"adequate", "events":[{"start_seconds":0,"end_seconds":7.2,"evidence":"Muzzle in white bowl", "uncertainties":[]}], "coverage_gaps":[], "explanation":"Bowl contact visible"},
        {"rule_id":"drinking", "outcome":"unknown", "visibility":"limited", "events":[], "coverage_gaps":[{"start_seconds":0,"end_seconds":7.2,"reason":"Mouth hidden"}], "explanation":"Cannot assess liquid consumption"},
    ]}


class MonitorTests(unittest.TestCase):
    def test_simple_format_preserves_unknown_without_inventing_coverage(self):
        value = {
            "bowl_visit": {"assessment": "observed", "intervals": [{"start": 0, "end": 7.2}], "evidence": "Muzzle at bowl", "uncertainty": None},
            "drinking": {"assessment": "unknown", "intervals": [], "evidence": "Action ambiguous", "uncertainty": "Cannot distinguish eating from drinking"},
        }
        self.assertEqual(validate_observations(json.dumps(value), TASK, 7.2, "simple"), value)
        schema = monitor_format(TASK, "simple")
        self.assertTrue(schema["strict"])
        self.assertEqual(set(schema["schema"]["required"]), {"bowl_visit", "drinking"})
        value["drinking"]["intervals"] = [{"start": 0, "end": 1}]
        with self.assertRaisesRegex(ValueError, "Only observed"):
            validate_observations(json.dumps(value), TASK, 7.2, "simple")
        value["drinking"].update(assessment="observed", intervals=[{"start": 0, "end": 8}])
        with self.assertRaisesRegex(ValueError, "within the source clip"):
            validate_observations(json.dumps(value), TASK, 7.2, "simple")
        value["drinking"]["intervals"] = [{"start": 0, "end": 0}]
        with self.assertRaisesRegex(ValueError, "within the source clip"):
            validate_observations(json.dumps(value), TASK, 7.2, "simple")

    def test_visit_can_be_observed_while_drinking_is_unknown(self):
        value = observations()
        self.assertEqual(validate_observations(json.dumps(value),TASK,7.2), value)
        self.assertEqual(monitor_format(validate_task(TASK))["schema"]["properties"]["assessments"]["items"]["properties"]["rule_id"]["enum"], ["bowl_visit","drinking"])

    def test_unknown_cannot_become_a_negative_observation_without_coverage(self):
        value = observations()
        value["assessments"][1]["outcome"] = "not_observed"
        with self.assertRaisesRegex(ValueError,"Negative observations"):
            validate_observations(json.dumps(value),TASK,7.2)

    def test_rule_completeness_timestamps_and_event_semantics_are_validated(self):
        variants = []
        value = observations(); value["assessments"].pop(); variants.append(value)
        value = observations(); value["assessments"][1]["rule_id"] = "bowl_visit"; variants.append(value)
        value = observations(); value["assessments"][0]["events"][0]["end_seconds"] = 8; variants.append(value)
        value = observations(); value["assessments"][0]["events"][0]["start_seconds"] = float("nan"); variants.append(value)
        value = observations(); value["assessments"][0]["events"] = []; variants.append(value)
        value = observations(); value["assessments"][1]["coverage_gaps"] = []; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_observations(json.dumps(value),TASK,7.2)

    def test_duplicate_task_rule_ids_are_rejected(self):
        task = copy.deepcopy(TASK)
        task["rules"][1]["id"] = "bowl_visit"
        with self.assertRaisesRegex(ValueError,"unique"):
            validate_task(task)
