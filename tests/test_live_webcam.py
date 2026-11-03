import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.live_webcam import WebcamSession, analyze_webcam_window, encode_clip
from pawception.video import ffmpeg, probe_video
from tests.test_live_mock import settings
from tests.test_live_muse import ManualExecutor


class WebcamTests(unittest.TestCase):
    def make_session(self, **changes):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = ManualExecutor()
        session = WebcamSession(settings(window_seconds=3, provider="mock", **changes),
                                now=0, root=Path(temporary.name), executor=executor)
        return session, executor

    def upload(self, session, end, now=None):
        session.receive(b"\x1aE\xdf\xa3camera", sequence=session.sequence, end=end,
                        mime="video/webm", now=end if now is None else now)

    def test_webcam_jobs_use_the_video_effort_setting(self):
        session, executor = self.make_session(video_reasoning_effort="low")
        self.upload(session, 4)
        state = session.snapshot(4)
        self.assertEqual(state["video_reasoning_effort"], "low")
        self.assertEqual(executor.calls[0][2][2]["reasoning_effort"], "low")
        self.assertEqual(json.loads((session.directory / "session.json").read_text())["video_reasoning_effort"], "low")

    def test_no_windows_or_calls_without_uploaded_camera_footage(self):
        session, executor = self.make_session()
        state = session.snapshot(10)
        self.assertEqual(executor.calls, [])
        self.assertEqual(state["captured_seconds"], 0)
        self.assertEqual(state["source_type"], "webcam")
        self.assertEqual(state["collecting_start"], 0)

    def test_overlap_and_slow_processing_use_existing_bounded_queue(self):
        session, executor = self.make_session(overlap_seconds=1)
        self.upload(session, 12)
        state = session.snapshot(12)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(state["queue_size"], 2)
        self.assertEqual([(job["start_seconds"], job["end_seconds"]) for job in state["windows"]],
                         [(0, 3), (2, 5), (4, 7), (6, 9), (8, 11)])
        self.assertEqual([job["id"] for job in state["skipped_windows"]], ["0001", "0002"])
        self.assertEqual(state["active_window"]["id"], "0000")
        self.assertEqual(state["captured_seconds"], 11.75)

    def test_out_of_order_and_future_chunks_do_not_change_recording(self):
        session, _ = self.make_session()
        self.upload(session, 1)
        original = session.source.read_bytes()
        for sequence, end, now in [(0, 2, 2), (2, 2, 2), (1, 10, 2)]:
            with self.assertRaises(ValueError):
                session.receive(b"wrong", sequence=sequence, end=end, mime="video/webm", now=now)
        self.assertEqual(session.source.read_bytes(), original)
        self.assertEqual(session.sequence, 1)

    def test_upload_disconnect_stops_capture_and_cancels_queued_work(self):
        session, executor = self.make_session()
        self.upload(session, 9)
        state = session.snapshot(25)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["queue_size"], 0)
        self.assertEqual(len(executor.calls), 1)
        self.assertTrue(executor.closed)
        self.assertEqual(state["captured_seconds"], 8.75)

    def test_added_events_only_join_the_next_dispatched_request(self):
        session, executor = self.make_session()
        self.upload(session, 3.3)
        session.add_event({"name": "New event", "description": "Watch this", "threshold_seconds": 1}, now=4)
        self.upload(session, 6.3)
        first = executor.calls[0][2][3]
        self.assertEqual(len(first), 1)
        job = deepcopy(session.active)
        session.future.set_result((job, {"status": "completed", "output": {
            "event_1": {"detected": False, "intervals": [], "evidence": "Stub"}},
            "finished_after_run_seconds": 9}))
        session.snapshot(9)
        self.assertEqual(len(executor.calls[1][2][3]), 2)

    def test_review_rejects_future_negative_and_overlong_intervals(self):
        session, _ = self.make_session()
        self.upload(session, 5)
        for start, end in [(-1, 1), (0, 6), (4, 3), (float("nan"), 2)]:
            with self.assertRaises(ValueError):
                session.review_media(start=start, end=end)

    def test_unfinished_webm_is_encoded_and_mocked_through_the_real_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "capture.webm"
            # Live WebM has no finalized duration/index, like MediaRecorder chunks.
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", 8,
                   "-an", "-c:v", "libvpx", "-live", 1, source)
            target = directory / "review.mp4"
            metadata = encode_clip(source, target, 2, 5)
            self.assertAlmostEqual(metadata["duration_seconds"], 3, delta=0.2)
            self.assertLessEqual(metadata["height"], 480)
            self.assertEqual((metadata["width"], metadata["height"]), (320, 240))
            job = {"id": "0000", "start_seconds": 3, "end_seconds": 6, "input_duration_seconds": 3}
            events = [{"id": "event_1", "description": "Chewing", "active_from": 0}]
            with patch("pawception.pipeline_runner.OpenAI", side_effect=AssertionError("No API call in mock mode")):
                prepared, record = analyze_webcam_window(source, directory, job, events, "unused", None, 0, "mock")
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["output"]["event_1"]["intervals"], [{"start": 2, "end": 3}])
            self.assertTrue((directory / prepared["file"]).exists())
            with self.assertRaisesRegex(ValueError, "does not cover"):
                encode_clip(source, directory / "missing.mp4", 6, 10)

    def test_missing_camera_time_is_not_filled_with_duplicate_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "gap.webm"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", 5,
                   "-vf", "select='not(between(t,1,3))'", "-fps_mode", "passthrough",
                   "-an", "-c:v", "libvpx", "-live", 1, source)
            with self.assertRaisesRegex(ValueError, "does not cover"):
                encode_clip(source, directory / "gap.mp4", 0, 4)


if __name__ == "__main__":
    unittest.main()
