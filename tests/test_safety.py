from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.live_mock import MockSession
from pawception.monitor import monitor_format, validate_binary_observations, validate_event_task
from pawception.safety import DEFAULT_HAZARD_PROMPT, SafetyWatch, safety_prompt, validate_hazards
from tests.test_live_mock import settings
from tests import test_live_muse


def hazard(**changes):
    return {"id": None, "kind": "electrical", "subject": "dog", "target": "power cable beside sofa",
            "title": "Dog chewing a power cable", "evidence": "Dog grips and chews the power cable.",
            "intervals": [{"start": 0, "end": 3}], **changes}


def observe(watch, start=0, end=3, hazards=None, gap=1):
    window = {"id": str(start), "start_seconds": start, "end_seconds": end,
              "input_duration_seconds": end-start, "safety": watch.context()}
    record = {"status": "completed", "finished_after_run_seconds": end+1,
              "output": {"hazards": [hazard()] if hazards is None else hazards}}
    watch.observe(window, record, gap)
    return watch.snapshot(gap)


class SafetyTests(unittest.TestCase):
    def test_general_household_guidance_does_not_require_a_pet(self):
        from pawception.pipeline import perception_prompt
        prompt=perception_prompt({"events":[],"safety_watch":True,"visual_evidence":True},30)
        self.assertIn("No pet or person needs to be visible",prompt)
        self.assertIn("uncontrolled fire",prompt)
        self.assertIn("ordinary steam",prompt)
        self.assertIn("both prompts may be null",prompt)
        self.assertNotIn("subject (the pet",prompt)
        custom=safety_prompt([],"Watch for sparks near the outlet.","Ignore the fireplace.")
        self.assertIn("A pet does not need to be visible",custom)
        self.assertIn("Ignore the fireplace.",custom)

    def test_household_only_observations_validate_and_continuing_fire_alerts_merge(self):
        task={"name":"Household safety","events":[],"safety_watch":True,"visual_evidence":True}
        fire=hazard(kind="fire",subject="kitchen",target="toaster on counter",
                    title="Fire spreading from toaster",evidence="Flames spread above the toaster; no pets or people are visible.",
                    pet_segmentation_prompt=None,object_segmentation_prompt="silver toaster")
        validate_binary_observations({"hazards":[fire]},task,3)
        kinds=monitor_format(task,"binary")["schema"]["properties"]["hazards"]["items"]["properties"]["kind"]["enum"]
        self.assertTrue({"fire","smoke","flooding"}.issubset(kinds))
        watch=SafetyWatch(True)
        first=observe(watch,hazards=[fire])
        continued=observe(watch,3,6,[{**fire,"id":first["hazards"][0]["id"],"title":"Continuing kitchen fire"}])
        self.assertEqual(len(continued["alerts"]),1)
        self.assertEqual(continued["alerts"][0]["evidence_span"],{"start":0,"end":6})
        self.assertEqual(continued["alerts"][0]["id"],first["alerts"][0]["id"])

    def test_smoke_with_no_segmentable_target_still_creates_an_alert(self):
        smoke=hazard(kind="smoke",subject="living room",target="smoke spreading through room",
                     title="Room filling with smoke",evidence="A spreading cloud of smoke obscures the room.",
                     pet_segmentation_prompt=None,object_segmentation_prompt=None)
        task={"events":[],"safety_watch":True,"visual_evidence":True}
        validate_binary_observations({"hazards":[smoke]},task,3)
        state=observe(SafetyWatch(True),hazards=[smoke])
        self.assertEqual(len(state["alerts"]),1)
        self.assertEqual(state["alerts"][0]["event_name"],smoke["title"])

    def test_prompt_rounded_duration_keeps_hazard_and_exact_alert_bounds(self):
        duration = 4.9998046875
        state = observe(SafetyWatch(True), end=duration,
                        hazards=[hazard(intervals=[{"start": 0, "end": 4.999804688}])])
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["alerts"][0]["evidence_span"], {"start": 0, "end": duration})

    def test_boundary_tolerance_does_not_accept_bad_or_empty_hazard_intervals(self):
        for interval in [{"start": 0, "end": 3.00001}, {"start": -0.00001, "end": 2},
                         {"start": 3, "end": 3.0000001}, {"start": -0.0000001, "end": 0},
                         {"start": 0, "end": float("nan")}, {"start": False, "end": 2}]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                validate_hazards([hazard(intervals=[interval])], 3)

    def test_custom_guidance_and_exclusions_replace_only_hazard_policy(self):
        from pawception.pipeline import perception_prompt
        task = {"events": [{"id": "event_1", "description": "Chewing furniture"}], "safety_watch": True,
                "safety_prompt": "Look for pets pulling exposed upholstery staples.",
                "safety_exclusions": "Ignore chewing the training cushion."}
        prompt = perception_prompt(task, 30)
        self.assertIn(task["safety_prompt"], prompt)
        self.assertIn(task["safety_exclusions"], prompt)
        self.assertIn("event_1: Chewing furniture", prompt)
        self.assertIn("not custom events", prompt)
        self.assertIn("reuse its id, kind, subject and target EXACTLY", prompt)
        self.assertNotIn(DEFAULT_HAZARD_PROMPT, prompt)
        self.assertIn(DEFAULT_HAZARD_PROMPT, safety_prompt([]))

    def test_editing_policy_preserves_continuing_alert_and_restores_default_cleanly(self):
        watch = SafetyWatch(True)
        old = {"id": "0", "start_seconds": 0, "end_seconds": 3,
               "input_duration_seconds": 3, "safety": watch.context()}
        watch.configure("Watch for exposed staples.", "Ignore cushion chewing.")
        watch.observe(old, {"status": "completed", "output": {"hazards": [hazard()]}}, 1)
        self.assertEqual(len(watch.alerts), 1)
        self.assertIsNone(old["safety"]["safety_prompt"])
        self.assertEqual(watch.context()["safety_prompt"], "Watch for exposed staples.")
        first = observe(watch, 3, 6)["alerts"][0]
        watch.configure(None, "")
        restored = watch.context()
        self.assertIsNone(restored["safety_prompt"])
        self.assertEqual(restored["safety_exclusions"], "")
        continued = observe(watch, 6, 9)
        self.assertEqual(len(continued["alerts"]), 1)
        self.assertEqual(continued["alerts"][0]["id"], first["id"])

    def test_invalid_policy_cannot_mutate_current_instructions(self):
        watch = SafetyWatch(True, "Existing guidance", "Existing exclusion")
        original = watch.context()
        for prompt, exclusions in [("", ""), (" " , ""), ("x"*6001, ""), ({}, ""), (None, None), (None, "x"*2001)]:
            with self.subTest(prompt=type(prompt), exclusions=type(exclusions)), self.assertRaises(ValueError):
                watch.configure(prompt, exclusions)
            self.assertEqual(watch.context(), original)

    def test_session_settings_reach_next_window_and_reject_invalid_updates(self):
        from pawception.live_mock import LiveMonitor
        manager = LiveMonitor(clock=lambda: 0)
        state = manager.create(settings(safety_watch=True, safety_prompt="Watch for staples.", safety_exclusions="Ignore cushion chewing."))
        self.assertEqual(state["safety_prompt"], "Watch for staples.")
        with self.assertRaises(ValueError):
            manager.update(state["id"], "safety", {})
        state = manager.update(state["id"], "safety", {"safety_prompt": None, "safety_exclusions": ""})
        self.assertIsNone(state["safety_prompt"])
        self.assertEqual(state["safety_exclusions"], "")

    def test_immediate_alert_and_continuation_keep_one_identity_despite_changed_title(self):
        watch = SafetyWatch(True)
        first = observe(watch)
        alert_id = first["alerts"][0]["id"]
        key = first["hazards"][0]["id"]
        second = observe(watch, 3, 6, [hazard(id=key, title="Puppy tugging the electrical cord")])
        self.assertEqual(len(second["alerts"]), 1)
        self.assertEqual(second["alerts"][0]["id"], alert_id)
        self.assertEqual(second["alerts"][0]["at"], 0)
        self.assertEqual(second["alerts"][0]["threshold_seconds"], 0)
        self.assertEqual(second["alerts"][0]["available_after_run_seconds"], 4)
        self.assertEqual(second["alerts"][0]["evidence_span"], {"start": 0, "end": 6})

    def test_canonical_identity_is_used_when_model_does_not_repeat_id(self):
        watch = SafetyWatch(True)
        original = observe(watch)["alerts"][0]["id"]
        second = observe(watch, 3, 6, [hazard(subject="DOG", target="Power cable beside sofa.", title="Different wording")])
        self.assertEqual(len(second["alerts"]), 1)
        self.assertEqual(second["alerts"][0]["id"], original)

    def test_tolerated_gap_and_overlap_do_not_alert_again_or_double_count(self):
        watch = SafetyWatch(True)
        observe(watch)
        state = observe(watch, 3.5, 6.5)
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["detections"][0]["detected_seconds"], 6)
        state = observe(watch, 5, 8)
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["detections"][0]["detected_seconds"], 7.5)

    def test_genuine_recurrence_after_large_gap_can_alert_again(self):
        watch = SafetyWatch(True)
        observe(watch)
        state = observe(watch, 5, 8)
        self.assertEqual(len(state["alerts"]), 2)
        self.assertEqual(len({alert["id"] for alert in state["alerts"]}), 2)

    def test_different_pets_objects_and_hazard_kinds_remain_distinct(self):
        state = observe(SafetyWatch(True), hazards=[hazard(), hazard(subject="cat"),
                        hazard(target="second cable beside chair"), hazard(kind="entanglement")])
        self.assertEqual(len(state["alerts"]), 4)

    def test_overlapping_later_window_can_extend_backwards_without_another_alert(self):
        watch = SafetyWatch(True)
        first = observe(watch, 0, 5, [hazard(intervals=[{"start": 3, "end": 5}])])
        state = observe(watch, 2, 8, [hazard(intervals=[{"start": 0, "end": 6}])])
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["alerts"][0]["id"], first["alerts"][0]["id"])

    def test_off_and_reenable_discard_old_inflight_result_and_clip_new_capture(self):
        watch = SafetyWatch(True)
        old = {"id": "0", "start_seconds": 0, "end_seconds": 30, "input_duration_seconds": 30, "safety": watch.context()}
        record = {"status": "completed", "output": {"hazards": [hazard()]}}
        watch.set_enabled(False, 4)
        watch.set_enabled(True, 20)
        watch.observe(old, record, 1)
        self.assertEqual(watch.snapshot(1)["alerts"], [])
        state = observe(watch, 10, 40, [hazard(intervals=[{"start": 0, "end": 15}])])
        self.assertEqual(state["alerts"][0]["at"], 20)
        self.assertEqual(state["detections"][0]["start"], 20)

    def test_empty_or_failed_observations_never_create_hazards(self):
        watch = SafetyWatch(True)
        self.assertEqual(observe(watch, hazards=[])["alerts"], [])
        watch.observe({"safety": watch.context()}, {"status": "error"}, 1)
        self.assertEqual(watch.snapshot(1)["alerts"], [])

    def test_schema_validates_safety_only_and_combined_output_in_one_request(self):
        task = {"name": "Safety only", "events": [], "safety_watch": True}
        validate_event_task(task)
        self.assertEqual(set(monitor_format(task, "binary")["schema"]["properties"]), {"hazards"})
        self.assertEqual(validate_binary_observations({"hazards": []}, task, 3), {"hazards": []})
        task["events"] = [{"id": "event_1", "description": "Chewing furniture"}]
        output = {"hazards": [hazard()], "event_1": {"detected": False, "intervals": [], "evidence": "None"}}
        validate_binary_observations(output, task, 3)
        self.assertEqual(set(monitor_format(task, "binary")["schema"]["properties"]), {"hazards", "event_1"})

    def test_invalid_unknown_or_changed_hazard_identity_is_rejected(self):
        watch = SafetyWatch(True)
        known = observe(watch)["hazards"][0]
        for value in [hazard(intervals=[]), hazard(intervals=[{"start": 0, "end": 4}]),
                      hazard(id="unknown"), hazard(id=known["id"], subject="another pet"), hazard(kind="invalid")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_hazards([value], 3, [known])

    def test_mock_safety_only_runs_and_continuing_hazard_has_just_one_alert(self):
        session = MockSession(settings(events=[], safety_watch=True, window_seconds=3, mock_delay_seconds=0.1))
        state = session.snapshot(100)
        self.assertEqual(state["events"], [])
        self.assertEqual(len(state["alerts"]), 2)
        electrical = [alert for alert in state["alerts"] if "electrical" in alert["event_name"]]
        self.assertEqual(len(electrical), 1)
        self.assertEqual(electrical[0]["at"], 3)
        self.assertEqual(electrical[0]["evidence_span"], {"start": 3, "end": 38})

    def test_live_off_suppresses_inflight_hazards_and_custom_events_continue(self):
        session = MockSession(settings(safety_watch=True, mock_delay_seconds=10))
        session.snapshot(11)
        session.set_safety(False, now=15)
        state = session.snapshot(100)
        self.assertTrue(state["alerts"])
        self.assertTrue(all(alert.get("source") != "safety" for alert in state["alerts"]))
        self.assertTrue(session.records["0000"]["output"]["hazards"])

    def test_turning_off_safety_only_skips_analysis_until_monitoring_resumes(self):
        session = MockSession(settings(events=[], safety_watch=True))
        session.set_safety(False, now=1)
        state = session.snapshot(20)
        self.assertIsNone(state["active_window"])
        self.assertEqual(state["queue_size"], 0)
        self.assertTrue(all(job["skip_reason"] == "No monitoring enabled" for job in state["windows"]))
        session.set_safety(True, now=21)
        self.assertTrue(session.snapshot(36)["alerts"])

    def test_viewed_safety_alert_stays_viewed_when_extended(self):
        session = MockSession(settings(safety_watch=True, window_seconds=3, mock_delay_seconds=0.1))
        first = session.snapshot(10)["alerts"][0]
        session.view_alert(first["id"], 10)
        state = session.snapshot(14)
        self.assertIn(first["id"], state["viewed_alert_ids"])
        self.assertEqual(len([alert for alert in state["alerts"] if alert["id"] == first["id"]]), 1)


class MuseSafetyTests(unittest.TestCase):
    def test_live_edit_reaches_next_muse_window_without_splitting_continuing_hazard(self):
        import json
        session, executor = test_live_muse.MuseSessionTests.make_session(self, safety_watch=True)
        session.snapshot(3)
        old = deepcopy(session.active)
        session.configure_safety({"safety_prompt": "Look for exposed staples.", "safety_exclusions": "Ignore the training cushion."}, now=3.5)
        self.assertIsNone(old["safety"]["safety_prompt"])
        output = {"event_1": {"detected": False, "intervals": [], "evidence": "None"}, "hazards": [hazard()]}
        session.future.set_result((old, {"status": "completed", "finished_after_run_seconds": 4, "output": output}))
        session.snapshot(6)
        next_job = executor.calls[1][2][2]
        self.assertEqual(next_job["safety"]["safety_prompt"], "Look for exposed staples.")
        self.assertEqual(next_job["safety"]["safety_exclusions"], "Ignore the training cushion.")
        session.future.set_result((deepcopy(session.active), {"status": "completed", "finished_after_run_seconds": 7, "output": output}))
        state = session.snapshot(7)
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["alerts"][0]["evidence_span"], {"start": 0, "end": 6})
        saved = json.loads((session.directory/"session.json").read_text())
        self.assertEqual(saved["safety"]["safety_exclusions"], "Ignore the training cushion.")

    def test_combined_schema_uses_one_existing_video_request_adapter(self):
        from pawception.live_muse import analyze_prepared_window
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory/"windows/0000").mkdir(parents=True)
            watch = SafetyWatch(True, "Look for pets pulling exposed staples.", "Ignore the training cushion.")
            job = {"id": "0000", "start_seconds": 0, "end_seconds": 3, "input_duration_seconds": 3,
                   "sha256": "stub", "file": "windows/0000/clip.mp4", "safety": watch.context()}
            events = [{"id": "event_1", "description": "Chewing furniture", "active_from": 0}]
            output = {"event_1": {"detected": False, "intervals": [], "evidence": "None"}, "hazards": [hazard()]}
            transport_result = {"analyses": {"monitor": {"status": "completed", "structured_output": output}}, "upload_cleanup": "deleted"}
            with patch("pawception.pipeline_runner.OpenAI"), patch("pawception.pipeline_runner.analyze_video", return_value=transport_result) as transport:
                prepared, record = analyze_prepared_window(directory,job,events,"test-model","stub-key",0)
            transport.assert_called_once()
            self.assertTrue(transport.call_args.kwargs["monitor_task"]["safety_watch"])
            self.assertEqual(transport.call_args.kwargs["monitor_task"]["safety_prompt"], watch.settings["safety_prompt"])
            self.assertEqual(transport.call_args.kwargs["monitor_task"]["safety_exclusions"], watch.settings["safety_exclusions"])
            self.assertIn("hazards",transport.call_args.kwargs["response_options"]["text"]["format"]["schema"]["properties"])
            watch.observe(prepared,record,1)
            self.assertEqual(len(watch.snapshot(1)["alerts"]),1)

    def test_next_muse_window_receives_known_hazard_identity(self):
        session, executor = test_live_muse.MuseSessionTests.make_session(self, safety_watch=True)
        session.snapshot(3)
        job = deepcopy(session.active)
        session.future.set_result((job, {"status": "completed", "finished_after_run_seconds": 4,
            "output": {"event_1": {"detected": False, "intervals": [], "evidence": "None"}, "hazards": [hazard()]}}))
        session.snapshot(6)
        context = executor.calls[1][2][2]["safety"]
        self.assertEqual(len(context["known_hazards"]), 1)
        self.assertEqual(context["known_hazards"][0]["id"], session.safety.snapshot(1)["hazards"][0]["id"])
