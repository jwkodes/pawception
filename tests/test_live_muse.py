from concurrent.futures import Future
from copy import deepcopy
from fractions import Fraction
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.live_mock import LiveMonitor
from pawception.live_muse import MuseSession, analyze_live_window
from tests.test_live_mock import settings


class ManualExecutor:
    def __init__(self):
        self.calls = []
        self.closed = False

    def submit(self, worker, *args):
        future = Future()
        self.calls.append((future, worker, args))
        return future

    def shutdown(self, **kwargs):
        self.closed = True


class MuseSessionTests(unittest.TestCase):
    def make_session(self, **changes):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        executor = ManualExecutor()
        timeline = [(Fraction(i), Fraction(1)) for i in range(30)]
        with patch("pawception.live_muse.probe_video", return_value=({"duration_seconds": 30}, timeline, Fraction(1))):
            session = MuseSession(settings(window_seconds=3, **changes), now=0, root=directory,
                                  source=directory / "source.mp4", api_key="test-secret", executor=executor)
        return session, executor

    def finish(self, session, at, *, error=None):
        job = deepcopy(session.active)
        if error:
            record = {"status": "error", "error": error, "finished_after_run_seconds": at}
        else:
            record = {"status": "completed", "finished_after_run_seconds": at, "output": {
                event_id: {"detected": True, "intervals": [{"start": 0, "end": job["input_duration_seconds"]}],
                           "evidence": "Stubbed API worker"} for event_id in job["event_ids"]}}
        session.future.set_result((job, record))
        return session.snapshot(at)

    def test_live_sam_is_disabled_even_when_an_old_browser_setting_enables_it(self):
        session, _ = self.make_session(sam_snapshots=True)
        self.assertFalse(hasattr(session,"shared_uploads"))
        self.assertIsNone(session.alert_images)
        self.assertFalse(session.sam_snapshots)

    def test_video_effort_is_saved_and_snapshotted_per_job(self):
        session, executor = self.make_session(video_reasoning_effort="high")
        state = session.snapshot(3)
        self.assertEqual(state["video_reasoning_effort"], "high")
        self.assertEqual(executor.calls[0][2][2]["reasoning_effort"], "high")
        self.assertEqual(json.loads((session.directory / "session.json").read_text())["video_reasoning_effort"], "high")

    def test_max_is_only_allowed_for_standard_spark(self):
        with self.assertRaisesRegex(ValueError, "Reasoning effort"):
            self.make_session(video_reasoning_effort="max")
        session, _ = self.make_session(model="muse-spark-1.3", video_reasoning_effort="max")
        self.assertEqual(session.snapshot(0)["video_reasoning_effort"], "max")

    def test_real_worker_waits_for_capture_and_keeps_a_bounded_queue(self):
        session, executor = self.make_session()
        session.snapshot(2)
        self.assertEqual(executor.calls, [])
        state = session.snapshot(15)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(state["provider"], "muse")
        self.assertEqual(state["queue_size"], 2)
        self.assertEqual([job["id"] for job in state["skipped_windows"]], ["0001", "0002"])
        self.assertEqual(state["active_window"]["id"], "0000")
        state = self.finish(session, 16)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(state["active_window"]["id"], "0003")

    def test_task_snapshot_is_immutable_and_next_request_gets_added_event(self):
        session, executor = self.make_session()
        session.snapshot(3)
        session.add_event({"name": "Playing", "description": "Pets playing", "threshold_seconds": 2}, now=4)
        session.snapshot(6)
        self.finish(session, 7)
        first_events = executor.calls[0][2][3]
        second_events = executor.calls[1][2][3]
        self.assertEqual([event["id"] for event in first_events], ["event_1"])
        self.assertEqual([event["id"] for event in second_events], ["event_1", "event_2"])

    def test_fatal_access_error_stops_future_submissions_and_is_not_a_negative(self):
        session, executor = self.make_session()
        session.snapshot(3)
        state = self.finish(session, 4, error={"status_code": 402})
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["alerts"], [])
        self.assertEqual(state["failed_windows"][0]["error"], {"status_code": 402})
        session.snapshot(100)
        self.assertEqual(len(executor.calls), 1)
        self.assertTrue(executor.closed)

    def test_stop_does_not_submit_or_retry_any_more_calls(self):
        session, executor = self.make_session()
        session.snapshot(6)
        session.stop(7)
        session.snapshot(100)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(session.status, "stopped")
        self.assertTrue(executor.closed)

    def test_session_json_and_public_snapshot_never_contain_the_api_key(self):
        session, _ = self.make_session()
        state = session.snapshot(6)
        saved = (session.directory / "session.json").read_text()
        self.assertNotIn("test-secret", saved)
        self.assertNotIn("test-secret", json.dumps(state))
        self.assertNotIn("mock_delay_seconds", state)

    def test_muse_ignores_mock_timing_and_waits_for_the_real_future(self):
        session, executor = self.make_session(mock_delay_seconds=-100)
        state = session.snapshot(20)
        self.assertEqual(state["active_window"]["status"], "running")
        self.assertEqual(state["analyzed_through"], 0)
        self.assertNotIn("mock_delay_seconds", state)
        self.assertEqual(len(executor.calls), 1)

    def test_new_event_intervals_are_clipped_to_activation_without_a_paid_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            folder = directory / "windows/0000"
            folder.mkdir(parents=True)
            job = {"id": "0000", "start_seconds": 10, "end_seconds": 15, "input_duration_seconds": 5}
            events = [{"id": "event_1", "description": "Chewing", "active_from": 12, "threshold_seconds": 2}]
            record = {"status": "completed", "output": {"event_1": {
                "detected": True, "intervals": [{"start": 0, "end": 5}], "evidence": "Stub"}}}
            with patch("pawception.pipeline_runner.prepare_windows"), patch("pawception.pipeline_runner.analyze_window", return_value=record) as analyze:
                _, actual = analyze_live_window(directory / "source.mp4", directory, job, events, "test-model", "secret", 0)
            self.assertEqual(actual["output"]["event_1"]["intervals"], [{"start": 2, "end": 5}])
            self.assertEqual(analyze.call_args.args[1]["request"]["model"], "test-model")
            self.assertEqual(json.loads((folder / "task.json").read_text())["events"], events)

    def test_live_bridge_executes_the_real_request_adapter_before_mocked_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "windows/0000").mkdir(parents=True)
            job = {"id": "0000", "start_seconds": 0, "end_seconds": 3, "input_duration_seconds": 3,
                   "sha256": "test-sha", "file": "windows/0000/clip.mp4", "reasoning_effort": "low"}
            events = [{"id": "event_1", "description": "Chewing furniture", "active_from": 0}]
            output = {"event_1": {"detected": True, "intervals": [{"start": 0, "end": 3}], "evidence": "Transport stub"}}
            transport_result = {"analyses": {"monitor": {"status": "completed", "structured_output": output,
                                "inference_seconds": 1}}, "upload_cleanup": "deleted", "upload_seconds": 1}
            with patch("pawception.pipeline_runner.prepare_windows"), patch("pawception.pipeline_runner.OpenAI") as client, \
                 patch("pawception.pipeline_runner.analyze_video", return_value=transport_result) as transport:
                _, record = analyze_live_window(directory / "source.mp4", directory, job, events,
                                                "muse-spark-1.3-contributor", "test-secret", 0)
            client.assert_called_once()
            transport.assert_called_once()
            self.assertEqual(transport.call_args.kwargs["response_options"]["reasoning"], {"effort": "low"})
            self.assertEqual(transport.call_args.kwargs["monitor_output_format"], "binary")
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["output"], output)
            self.assertTrue((directory / "windows/0000/observation.json").exists())

    def test_mock_mode_never_loads_credentials(self):
        with patch("dotenv.load_dotenv", side_effect=AssertionError("No credentials in Mock mode")):
            self.assertEqual(LiveMonitor(clock=lambda: 0).create(settings())["provider"], "mock")

    def test_missing_key_is_a_clear_start_error(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv"):
            with self.assertRaisesRegex(ValueError, "MODEL_API_KEY"):
                LiveMonitor(clock=lambda: 0).create(settings(provider="muse"), root=Path(temporary), source=Path(temporary) / "source.mp4")


if __name__ == "__main__":
    unittest.main()
