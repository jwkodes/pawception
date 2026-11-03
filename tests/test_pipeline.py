import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.pipeline import build_pipeline_replay, normalize_window, fingerprint, perception_prompt, validate_config
from pawception.monitor import monitor_format, validate_observations
from pawception.pipeline_runner import main, process_run, save_replay
from pawception.files import checksum


def config(event_ids=("chewing",), seconds=10):
    return {"version": 3, "alert_after_seconds": {event_id: seconds for event_id in event_ids}, "task": {"name": "Test", "events": [
        {"id": event_id, "description": f"Detect {event_id}."} for event_id in event_ids
    ]}}


def window(index, overlap=1):
    start = index * (3 - overlap)
    return {"id": str(index), "index": index, "start_seconds": start,
            "end_seconds": start + 3, "input_duration_seconds": 3, "sha256": f"hash-{index}"}


def response(detected=False, intervals=None):
    return {"status": "completed", "finished_after_run_seconds": 10,
            "output": {"chewing": {"detected": detected,
                "intervals": intervals if intervals is not None else [{"start": 0.5, "end": 1}] if detected else [],
                "evidence": "Synthetic test decision"}}}


class BinaryDetectionTests(unittest.TestCase):
    def test_prompt_rounded_duration_is_clamped_without_modifying_raw_response(self):
        duration = 4.9998046875
        output = response(True, [{"start": -0.0000001, "end": 4.999804688}])["output"]
        raw = json.dumps(output)
        validated = validate_observations(raw, config()["task"], duration, "binary")
        self.assertEqual(validated["chewing"]["intervals"], [{"start": 0, "end": duration}])
        self.assertEqual(json.loads(raw), output)

    def test_binary_schema_has_no_uncertainty_visibility_or_third_answer(self):
        schema = monitor_format(config()["task"], "binary")["schema"]
        fields = schema["properties"]["chewing"]["properties"]
        self.assertEqual(set(fields), {"detected", "intervals", "evidence"})
        self.assertEqual(fields["detected"], {"type": "boolean"})
        prompt = perception_prompt(config()["task"], 3)
        self.assertIn("not enough evidence", prompt)
        self.assertNotIn("coverage", prompt)
        self.assertNotIn("uncertainty", prompt)

    def test_local_intervals_use_measured_source_offset(self):
        w = {**window(1), "start_seconds": 2.03, "end_seconds": 5.03}
        detection = normalize_window(w, response(True), config()["task"])[0]
        self.assertAlmostEqual(detection["intervals"][0]["start"], 2.53)
        self.assertAlmostEqual(detection["intervals"][0]["end"], 3.03)

    def test_same_ten_second_threshold_for_different_window_lengths_and_overlap(self):
        for length, overlap in ((1, 0), (3, 0), (3, 1), (6, 2), (12, 0), (97, 0)):
            with self.subTest(length=length, overlap=overlap):
                windows, records = [], {}
                start = 0
                while start < 20:
                    w = {"id": str(len(windows)), "index": len(windows), "start_seconds": start,
                         "end_seconds": start + length, "input_duration_seconds": length}
                    windows.append(w)
                    records[w["id"]] = response(True, [{"start": 0, "end": length}])
                    start += length - overlap
                replay = build_pipeline_replay(config(), windows, records)
                self.assertEqual(len(replay["alerts"]), 1)
                alert = replay["alerts"][0]
                self.assertEqual(alert["at"], 10)
                self.assertEqual(alert["threshold_seconds"], 10)
                self.assertGreaterEqual(alert["confirmed_through_seconds"], 10)

    def test_single_long_window_triggers_at_interval_start_plus_threshold(self):
        w = {**window(0), "end_seconds": 97, "input_duration_seconds": 97}
        replay = build_pipeline_replay(config(), [w], {"0": response(True, [{"start": 9, "end": 45}])})
        alert = replay["alerts"][0]
        self.assertEqual(alert["at"], 19)
        self.assertEqual(alert["confirmed_through_seconds"], 97)
        self.assertEqual(alert["available_after_run_seconds"], 10)
        self.assertEqual(alert["supporting_window_ids"], ["0"])

    def test_positive_windows_measure_event_intervals_not_window_durations(self):
        windows = [window(i, 0) for i in range(5)]
        replay = build_pipeline_replay(config(), windows, {str(i): response(True) for i in range(5)})
        self.assertEqual(replay["alerts"], [])
        self.assertEqual(replay["snapshots"][-1]["longest_detected_seconds"]["chewing"], 0.5)

    def test_overlapping_positive_duration_is_not_double_counted(self):
        records = {str(i): response(True, [{"start": 0, "end": 3}]) for i in range(3)}
        replay = build_pipeline_replay(config(seconds=8), [window(i) for i in range(3)], records)
        self.assertEqual(replay["events"][0]["end"], 7)
        self.assertEqual(replay["alerts"], [])

    def test_separate_short_bursts_do_not_accumulate(self):
        w = {**window(0), "end_seconds": 30, "input_duration_seconds": 30}
        record = response(True, [{"start": 0, "end": 6}, {"start": 8, "end": 14}, {"start": 20, "end": 26}])
        self.assertEqual(build_pipeline_replay(config(), [w], {"0": record})["alerts"], [])

    def test_separate_qualifying_bursts_each_alert(self):
        w = {**window(0), "end_seconds": 30, "input_duration_seconds": 30}
        record = response(True, [{"start": 0, "end": 10}, {"start": 15, "end": 30}])
        replay = build_pipeline_replay(config(), [w], {"0": record})
        self.assertEqual([a["at"] for a in replay["alerts"]], [10, 25])
        self.assertEqual(len({a["id"] for a in replay["alerts"]}), 2)

    def test_real_window_boundary_gap_merges_without_a_second_alert(self):
        windows = [{"id": "0", "start_seconds": 0, "end_seconds": 30.012976656517328, "input_duration_seconds": 30.012976656517328},
                   {"id": "1", "start_seconds": 30.013049151805134, "end_seconds": 60.02602580832246, "input_duration_seconds": 30.012976656517328}]
        records = {"0": response(True, [{"start": 6.5, "end": 29.5}]),
                   "1": response(True, [{"start": 0, "end": 18}])}
        strict = build_pipeline_replay(config(), windows, records)
        merged = build_pipeline_replay({**config(), "merge_gap_seconds": 1}, windows, records)
        first = build_pipeline_replay({**config(), "merge_gap_seconds": 1}, windows[:1], records)
        self.assertEqual(len(strict["alerts"]), 2)
        self.assertEqual(len(merged["alerts"]), 1)
        self.assertEqual(merged["alerts"][0]["id"], first["alerts"][0]["id"])
        self.assertEqual(merged["alerts"][0]["at"], 16.5)
        self.assertEqual(len(merged["events"]), 1)
        self.assertEqual(merged["events"][0]["start"], 6.5)
        self.assertAlmostEqual(merged["events"][0]["end"], 48.013049151805134)
        self.assertAlmostEqual(merged["events"][0]["detected_seconds"], 41)

    def test_gap_tolerance_is_inclusive_and_can_be_disabled(self):
        w = {**window(0), "end_seconds": 20, "input_duration_seconds": 20}
        for gap, tolerance, count in ((0.5, 0.5, 1), (0.5001, 0.5, 2), (0.5, 0, 2), (2, 1, 2)):
            with self.subTest(gap=gap, tolerance=tolerance):
                record = response(True, [{"start": 0, "end": 4}, {"start": 4+gap, "end": 10}])
                replay = build_pipeline_replay({**config(seconds=2), "merge_gap_seconds": tolerance}, [w], {"0": record})
                self.assertEqual(len(replay["events"]), count)
                self.assertEqual(len(replay["alerts"]), count)

    def test_merged_gap_is_excluded_from_alert_time_and_detected_duration(self):
        w = {**window(0), "end_seconds": 20, "input_duration_seconds": 20}
        task = {**config(), "merge_gap_seconds": 1}
        record = response(True, [{"start": 0, "end": 6}, {"start": 6.5, "end": 10.5}])
        replay = build_pipeline_replay(task, [w], {"0": record})
        self.assertEqual(replay["alerts"][0]["at"], 10.5)
        self.assertEqual(replay["alerts"][0]["detected_duration_seconds"], 10)
        record["output"]["chewing"]["intervals"][-1]["end"] = 10.4
        self.assertEqual(build_pipeline_replay(task, [w], {"0": record})["alerts"], [])

    def test_overlap_and_bridged_gap_are_not_double_counted(self):
        w = {**window(0), "end_seconds": 20, "input_duration_seconds": 20}
        record = response(True, [{"start": 0, "end": 4}, {"start": 3, "end": 6}, {"start": 6.5, "end": 10.5}])
        replay = build_pipeline_replay({**config(), "merge_gap_seconds": 1}, [w], {"0": record})
        self.assertEqual(replay["events"][0]["detected_seconds"], 10)
        self.assertEqual(replay["alerts"][0]["at"], 10.5)

    def test_event_gap_tolerance_must_be_finite_and_within_range(self):
        for gap in (-1, 30.1, True, "1", float("nan"), float("inf")):
            with self.subTest(gap=gap), self.assertRaises(ValueError):
                validate_config({**config(), "merge_gap_seconds": gap})

    def test_negative_overlap_does_not_erase_positive_or_reset_duration(self):
        windows = [window(i) for i in range(3)]
        records = {"0": response(True, [{"start": 0, "end": 3}]), "1": response(),
                   "2": response(True, [{"start": 0, "end": 3}])}
        replay = build_pipeline_replay(config(seconds=3), windows, records)
        self.assertEqual([a["at"] for a in replay["alerts"]], [3, 7])
        self.assertEqual(len(replay["events"]), 2)
        self.assertNotIn("coverage_gaps", replay)
        self.assertNotIn("uncertainties", replay)

    def test_no_or_error_does_not_bridge_an_unobserved_gap(self):
        for middle in (response(), {"status": "error", "error": "API unavailable"}):
            with self.subTest(middle=middle):
                records = {"0": response(True, [{"start": 0, "end": 3}]), "1": middle,
                           "2": response(True, [{"start": 0, "end": 3}])}
                replay = build_pipeline_replay(config(seconds=5), [window(i, 0) for i in range(3)], records)
                self.assertEqual(replay["alerts"], [])
                if middle["status"] == "error":
                    self.assertIsNone(replay["windows"][1]["detections"][0]["detected"])

    def test_error_overlap_does_not_erase_valid_positive_evidence(self):
        windows = [window(i, 2) for i in range(3)]
        records = {"0": response(True, [{"start": 0, "end": 3}]), "1": {"status": "error"},
                   "2": response(True, [{"start": 0, "end": 3}])}
        replay = build_pipeline_replay(config(seconds=5), windows, records)
        self.assertEqual([a["at"] for a in replay["alerts"]], [5])

    def test_threshold_is_inclusive_and_shorter_interval_does_not_alert(self):
        w = {**window(0), "end_seconds": 20, "input_duration_seconds": 20}
        for duration, expected in ((9.99, 0), (10, 1), (10.01, 1)):
            with self.subTest(duration=duration):
                replay = build_pipeline_replay(config(), [w], {"0": response(True, [{"start": 0, "end": duration}])})
                self.assertEqual(len(replay["alerts"]), expected)

    def test_out_of_order_result_waits_for_earlier_window(self):
        replay = build_pipeline_replay(config(seconds=0.5), [window(0), window(1)], {"1": response(True)})
        self.assertEqual(replay["summary"]["finalized_windows"], 0)
        self.assertEqual(replay["alerts"], [])
        self.assertTrue(replay["events"])
        records = {"0": response(), "1": {**response(True), "finished_after_run_seconds": 4}}
        replay = build_pipeline_replay(config(seconds=0.5), [window(0), window(1)], records)
        self.assertEqual(replay["alerts"][0]["available_after_run_seconds"], 10)

    def test_each_event_has_its_own_duration_threshold(self):
        task = config(("chewing", "drinking"), seconds=2)
        task["alert_after_seconds"]["drinking"] = 4
        records = {"0": response(True, [{"start": 0, "end": 3}]),
                   "1": response(True, [{"start": 0, "end": 3}])}
        for record in records.values():
            record["output"]["drinking"] = dict(record["output"]["chewing"])
        replay = build_pipeline_replay(task, [window(i) for i in range(2)], records)
        self.assertEqual([(a["event_id"], a["at"]) for a in replay["alerts"]], [("chewing", 2), ("drinking", 4)])

    def test_extending_an_alerted_interval_backwards_does_not_alert_again(self):
        records = {"0": response(True, [{"start": 1, "end": 3}]),
                   "1": response(True, [{"start": 0, "end": 3}])}
        windows = [window(0), {**window(1), "start_seconds": 0.5, "end_seconds": 3.5}]
        replay = build_pipeline_replay(config(seconds=2), windows, records)
        self.assertEqual(len(replay["alerts"]), 1)
        self.assertEqual(replay["alerts"][0]["at"], 3)

    def test_duration_config_requires_each_event_and_positive_finite_seconds(self):
        for value in (0, -1, True, "10", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(config(seconds=value))
        missing = config()
        missing["alert_after_seconds"] = {}
        with self.assertRaises(ValueError):
            validate_config(missing)

    def test_invalid_binary_answers_are_processing_errors_not_negatives(self):
        variants = [response(True, [{"start": 0, "end": 0}]), response(True, [{"start": 0, "end": 4}]),
                    response(False, [{"start": 0, "end": 1}]), response(True, [])]
        wrong_type = response()
        wrong_type["output"]["chewing"]["detected"] = "yes"
        variants.append(wrong_type)
        for record in variants:
            with self.subTest(record=record):
                item = normalize_window(window(0), record, config()["task"])[0]
                self.assertEqual(item["status"], "invalid")
                self.assertIsNone(item["detected"])

    def test_legacy_uncertainty_runs_are_not_silently_reinterpreted(self):
        with self.assertRaises(ValueError):
            validate_config({"version": 1, "task": {}, "temporal_rules": []})
        with self.assertRaises(ValueError):
            validate_config({"version": 2, "task": config()["task"]})

    def test_saved_replay_is_deterministic_without_duplicate_alerts(self):
        run = {"config": config(seconds=0.5), "windows": [window(0), window(1)], "records": {"0": response(True, [{"start": 0, "end": 3}]), "1": response(True, [{"start": 0, "end": 3}])}}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = save_replay(run, directory)
            second = save_replay(run, directory)
            self.assertEqual(first, second)
            self.assertEqual(len(second["alerts"]), 1)


class RunnerTests(unittest.TestCase):
    def test_new_run_requires_explicit_configuration_before_reading_media_or_credentials(self):
        error = io.StringIO()
        with patch("pawception.pipeline_runner.probe_video", side_effect=AssertionError("No media read")), \
             patch("pawception.pipeline_runner.load_dotenv", side_effect=AssertionError("No credentials")), \
             patch("pawception.pipeline_runner.OpenAI", side_effect=AssertionError("No API calls")), \
             contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as result:
            main(["video.mp4", "--provider", "muse"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("--config is required", error.getvalue())

    def test_resume_reuses_saved_configuration_without_a_config_argument(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            source.write_bytes(b"cached source")
            saved = {"id": "test", "version": 3, "provider": "muse", "status": "completed",
                     "config": config(seconds=0.5),
                     "source": {"file": str(source), "sha256": checksum(source)},
                     "request": {"model": "test"}, "window_seconds": 3, "overlap_seconds": 1,
                     "windows": [window(0)], "records": {"0": response(True)}}
            (directory / "run.json").write_text(json.dumps(saved))
            with patch("pawception.pipeline_runner.OpenAI", side_effect=AssertionError("No API calls")), \
                 patch("pawception.pipeline_runner.load_dotenv", side_effect=AssertionError("No credentials")):
                self.assertEqual(main(["--resume", str(directory), "--replay-only"]), 0)
            self.assertEqual(json.loads((directory / "run.json").read_text())["config"], saved["config"])

    def test_new_mock_run_requires_explicit_fixture_before_reading_media(self):
        error = io.StringIO()
        with patch("pawception.pipeline_runner.probe_video", side_effect=AssertionError("No media read")), \
             contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as result:
            main(["video.mp4", "--config", "config.json"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("--mock-fixture is required", error.getvalue())

    def test_explicit_duration_replay_preserves_originals_without_credentials_or_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            source.write_bytes(b"cached source")
            original = {"id": "test", "version": 2, "provider": "muse", "status": "completed",
                        "config": {"version": 2, "task": config()["task"]},
                        "source": {"file": str(source), "sha256": checksum(source)},
                        "request": {"model": "test"}, "window_seconds": 3, "overlap_seconds": 1,
                        "windows": [window(0)], "records": {"0": response(True)}}
            (directory / "run.json").write_text(json.dumps(original))
            (directory / "replay.json").write_text('{"old_policy": true}')
            settings = directory / "config.json"
            settings.write_text(json.dumps(config(seconds=0.5)))
            with patch("pawception.pipeline_runner.OpenAI", side_effect=AssertionError("No API calls")), \
                 patch("pawception.pipeline_runner.load_dotenv", side_effect=AssertionError("No credentials")):
                self.assertEqual(main(["--resume", str(directory), "--config", str(settings), "--replay-only"]), 0)
            self.assertEqual(json.loads((directory / "run.before-duration-alerts.json").read_text()), original)
            self.assertEqual(json.loads((directory / "replay.before-duration-alerts.json").read_text()), {"old_policy": True})
            updated = json.loads((directory / "run.json").read_text())
            self.assertEqual(updated["records"], original["records"])
            self.assertEqual(updated["version"], 3)
            self.assertEqual(len(json.loads((directory / "replay.json").read_text())["alerts"]), 1)

    def make_run(self):
        return {"config": config(), "provider": "muse", "request": {"model": "test"},
                "windows": [window(0), window(1)], "records": {}}

    def cached_record(self, run, w):
        record = response()
        prompt = perception_prompt(run["config"]["task"], w["input_duration_seconds"])
        record["cache_key"] = fingerprint([w["sha256"], prompt, run["request"]])
        return record

    def test_successful_cached_windows_do_not_invoke_the_model_again(self):
        run = self.make_run()
        run["records"] = {w["id"]: self.cached_record(run, w) for w in run["windows"]}
        with tempfile.TemporaryDirectory() as directory, patch("pawception.pipeline_runner.analyze_window") as analyze:
            replay = process_run(run, Path(directory))
            analyze.assert_not_called()
            self.assertEqual(replay["summary"]["finalized_windows"], 2)

    def test_interrupted_call_is_processing_error_until_explicit_retry(self):
        run = self.make_run()
        run["records"] = {"0": {"status": "running"}, "1": {"status": "error"}}
        with tempfile.TemporaryDirectory() as directory, patch("pawception.pipeline_runner.analyze_window") as analyze:
            replay = process_run(run, Path(directory))
            analyze.assert_not_called()
            self.assertEqual(run["records"]["0"]["status"], "interrupted")
            self.assertTrue(all(d["detected"] is None for d in replay["detections"]))

    def test_crash_after_saved_receipt_recovers_without_another_call(self):
        run = self.make_run()
        run["windows"] = [window(0)]
        run["records"] = {"0": {"status": "running"}}
        with tempfile.TemporaryDirectory() as directory, patch("pawception.pipeline_runner.analyze_window") as analyze:
            folder = Path(directory) / "windows/0"
            folder.mkdir(parents=True)
            (folder / "observation.json").write_text(json.dumps(self.cached_record(run, window(0))))
            process_run(run, Path(directory))
            analyze.assert_not_called()
            self.assertEqual(run["records"]["0"]["status"], "completed")

    def test_modified_request_is_not_mixed_with_cached_predictions(self):
        run = self.make_run()
        run["records"] = {"0": self.cached_record(run, window(0))}
        run["request"]["model"] = "different"
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "Cached response"):
            process_run(run, Path(directory))

    def test_access_failure_stops_new_paid_calls(self):
        run = self.make_run()
        with tempfile.TemporaryDirectory() as directory:
            for w in run["windows"]:
                (Path(directory) / "windows" / w["id"]).mkdir(parents=True)
            with patch("pawception.pipeline_runner.analyze_window", return_value={"status": "error", "error": {"status_code": 402}}) as analyze:
                replay = process_run(run, Path(directory), workers=1)
            self.assertEqual(analyze.call_count, 1)
            self.assertEqual(replay["summary"]["finalized_windows"], 1)
            self.assertEqual(run["status"], "has_errors")

    def test_paced_replay_does_not_release_future_frames(self):
        run = self.make_run()
        clock = [0.0]
        released = []

        def analyze(w, *_):
            released.append((w["id"], clock[0]))
            self.assertGreaterEqual(clock[0], w["end_seconds"])
            return response()

        with tempfile.TemporaryDirectory() as directory:
            for w in run["windows"]:
                (Path(directory) / "windows" / w["id"]).mkdir(parents=True)
            with patch("pawception.pipeline_runner.time.monotonic", side_effect=lambda: clock[0]), \
                 patch("pawception.pipeline_runner.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                 patch("pawception.pipeline_runner.analyze_window", side_effect=analyze):
                process_run(run, Path(directory), workers=1, paced=True)
        self.assertEqual([window_id for window_id, _ in released], ["0", "1"])


if __name__ == "__main__":
    unittest.main()
