from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from pawception.alert_images import AlertImages, evidence_request
from pawception.monitor import monitor_format, validate_binary_observations
from pawception.sam_evidence import crop_bounds, create_evidence, render_snapshots
from pawception.safety import identity
from tests.test_safety import hazard
from tests import test_live_muse


class VisualEvidenceTests(unittest.TestCase):
    def test_cancelled_thumbnail_does_not_extract_or_submit_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            cancelled=Event(); cancelled.set()
            with patch("pawception.sam_evidence.ffmpeg") as extract, patch("pawception.sam_evidence.OpenAI") as client:
                result=create_evidence(Path(temp)/"clip.mp4",1,{"pet":"cat","object":"can"},Path(temp),"secret",cancelled)
            self.assertEqual(result["status"],"unavailable")
            extract.assert_not_called();client.assert_not_called()

    def test_image_route_serves_only_local_alert_images_and_rejects_symlinks(self):
        from pawception.server import ReplayHandler
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_id, alert_id = "a"*32, "b"*24
            folder = root / "data/live_sessions" / session_id / "evidence" / alert_id
            folder.mkdir(parents=True)
            image = folder / "zoom.jpg"
            image.write_bytes(b"jpeg")
            request = SimpleNamespace(path=f"/alert-images/{session_id}/{alert_id}/zoom.jpg", respond=Mock())
            with patch("pawception.server.ROOT", root):
                ReplayHandler.do_GET(request)
                request.respond.assert_called_with(200, b"jpeg", "image/jpeg")
                image.unlink()
                outside = root / "private.jpg"
                outside.write_bytes(b"private")
                image.symlink_to(outside)
                ReplayHandler.do_GET(request)
                self.assertEqual(request.respond.call_args.args[0], 404)

    def test_running_job_exposes_thumbnail_before_all_concepts_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); executor=test_live_muse.ManualExecutor()
            images=AlertImages(root,"secret",executor=executor)
            alert={"id":"a"*24,"event_id":"event_1","supporting_window_ids":["0"],"evidence_span":{"start":12,"end":13}}
            state={"id":"b"*32,"status":"live","alerts":[alert]}
            windows=[{"id":"0","file":"windows/0/clip.mp4","start_seconds":10}]
            records={"0":{"output":{"event_1":{"intervals":[{"start":2,"end":3}],
                "pet_segmentation_prompt":"tabby cat","object_segmentation_prompt":"legacy can"}}}}
            images.update(state,windows,records)
            self.assertEqual(executor.calls[0][2][2],{"pet":"tabby cat","object":"legacy can"})
            folder=root/"evidence"/alert["id"]; folder.mkdir(parents=True)
            (folder/"result.json").write_text(json.dumps({"status":"processing","frame_seconds":0.5,"zoom":"snapshots/000001/zoom.jpg"}))
            images.update(state,windows,records)
            self.assertTrue(state["evidence_pending"])
            self.assertEqual(alert["visual_evidence"]["source_seconds"],10.5)
            self.assertTrue(alert["visual_evidence"]["snapshot_url"].endswith("/snapshots/000001/zoom.jpg"))
            self.assertNotIn("tracking",alert["visual_evidence"])
            self.assertFalse(executor.calls[0][0].done())
            images.close()

    def test_tracking_routes_serve_json_and_png_but_reject_parent_symlinks(self):
        from pawception.server import ReplayHandler
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); base=root/"data/live_sessions"/("a"*32)/"evidence"/("b"*24)
            frames=base/"pet/frames"; frames.mkdir(parents=True)
            (base/"pet/tracking.json").write_text('{"frames": []}')
            (frames/"000000.png").write_bytes(b"png")
            snapshots=base/"snapshots/000000"; snapshots.mkdir(parents=True)
            (snapshots/"zoom.jpg").write_bytes(b"jpeg")
            for filename,mime in [("pet/tracking.json","application/json"),("pet/frames/000000.png","image/png"),
                                  ("snapshots/000000/zoom.jpg","image/jpeg")]:
                request=SimpleNamespace(path=f"/alert-images/{'a'*32}/{'b'*24}/{filename}",respond=Mock())
                with patch("pawception.server.ROOT",root): ReplayHandler.do_GET(request)
                self.assertEqual(request.respond.call_args.args[0],200)
                self.assertEqual(request.respond.call_args.args[2],mime)
            (frames/"000000.png").unlink(); frames.rmdir()
            outside=root/"outside"; outside.mkdir(); (outside/"000000.png").write_bytes(b"private")
            frames.symlink_to(outside,target_is_directory=True)
            request=SimpleNamespace(path=f"/alert-images/{'a'*32}/{'b'*24}/pet/frames/000000.png",respond=Mock())
            with patch("pawception.server.ROOT",root): ReplayHandler.do_GET(request)
            self.assertEqual(request.respond.call_args.args[0],404)

    def test_visual_schema_requires_pet_and_object_prompts(self):
        task = {"name": "Test", "events": [{"id": "a", "description": "Chewing"}],
                "safety_watch": True, "visual_evidence": True}
        answer = {"detected": False, "intervals": [], "evidence": "No chewing",
                  "pet_segmentation_prompt": None, "object_segmentation_prompt": None}
        value = {"a": answer, "hazards": [hazard(pet_segmentation_prompt="dog", object_segmentation_prompt="power cable")]}
        validate_binary_observations(value, task, 3)
        schema = monitor_format(task, "binary")["schema"]
        self.assertIn("pet_segmentation_prompt", schema["properties"]["a"]["required"])
        self.assertIn("object_segmentation_prompt", schema["properties"]["a"]["required"])
        for changes in [{"pet_segmentation_prompt": "dog"}, {"pet_segmentation_prompt": ""}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_binary_observations({**value, "a": {**answer, **changes}}, task, 3)
        del value["hazards"][0]["pet_segmentation_prompt"]
        with self.assertRaises(ValueError):
            validate_binary_observations(value, task, 3)

    def test_evidence_frame_is_in_a_positive_interval_and_uses_local_window_time(self):
        h = hazard(pet_segmentation_prompt="dog", object_segmentation_prompt="power cable")
        alert = {"source": "safety", "event_id": identity(h), "supporting_window_ids": ["1"],
                 "evidence_span": {"start": 12, "end": 13}}
        window = {"id": "1", "start_seconds": 10, "file": "windows/1/clip.mp4"}
        record = {"output": {"hazards": [h]}}
        request = evidence_request(alert, [window], {"1": record})
        self.assertEqual(request["frame_seconds"], 2.5)
        self.assertEqual(request["source_seconds"], 12.5)
        self.assertEqual(request["prompts"]["pet"], "dog")

    def test_crop_retains_object_and_stays_inside_frame_at_every_edge(self):
        for box in [(0, 0, 15, 20), (90, 70, 100, 80), (0, 0, 100, 80), (30, 35, 40, 45)]:
            left, top, right, bottom = crop_bounds((100, 80), [box])
            self.assertTrue(0 <= left <= box[0] < box[2] <= right <= 100)
            self.assertTrue(0 <= top <= box[1] < box[3] <= bottom <= 80)
        self.assertEqual(crop_bounds((100, 80), []), (0, 0, 100, 80))

    def test_render_uses_pet_for_zoom_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (100, 80), "white").save(root / "original.jpg")
            Image.new("L", (100, 80), 255).save(root / "mask.png")
            original = (root / "original.jpg").read_bytes()
            result = render_snapshots(root / "original.jpg", {"pet": {"segments": [
                {"box": [20, 20, 30, 30], "mask_file": str(root / "mask.png")}] }}, root)
            self.assertEqual(result["matches"], {"pet": 1, "object": 0})
            with Image.open(root / "zoom.jpg") as zoom:
                self.assertLess(zoom.width, 100)
            self.assertEqual((root / "original.jpg").read_bytes(), original)

    def test_sam_failure_leaves_source_frame_and_never_raises_into_monitoring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "clip.mp4").write_bytes(b"fixture")
            def extract(*args, **kwargs):
                Image.new("RGB", (20, 20)).save(root / "original.jpg")
            with patch("pawception.sam_evidence.ffmpeg", side_effect=extract), patch("pawception.sam_evidence.OpenAI"), \
                 patch("pawception.sam_evidence.segment_image", side_effect=ValueError("bad mask")):
                result = create_evidence(root / "clip.mp4", 1, {"pet": "cat"}, root, "secret")
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["original"], "original.jpg")
            self.assertNotIn("secret", json.dumps(result))

    def test_queue_is_bounded_and_continuing_alert_does_not_repeat_sam_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            executor = test_live_muse.ManualExecutor()
            images = AlertImages(Path(temp), "secret", executor=executor)
            h = hazard(pet_segmentation_prompt="dog", object_segmentation_prompt="cable")
            alert = {"id": "a"*24, "source": "safety", "event_id": identity(h),
                     "supporting_window_ids": ["0"], "evidence_span": {"start": 0, "end": 3}}
            state = {"id": "b"*32, "status": "live", "alerts": [deepcopy(alert)]}
            windows = [{"id": "0", "file": "windows/0/clip.mp4", "start_seconds": 0}]
            records = {"0": {"output": {"hazards": [h]}}}
            images.update(state, windows, records)
            images.update(state, windows, records)
            self.assertEqual(len(executor.calls), 1)
            self.assertTrue(state["evidence_pending"])
            state["alerts"] = [{**alert, "id": str(i)*24} for i in range(5)]
            images.update(state, windows, records)
            self.assertEqual(len(executor.calls), 3)
            images.close()
            self.assertTrue(images.cancelled.is_set())

    def test_added_object_fields_do_not_break_custom_event_alerts(self):
        session, executor = test_live_muse.MuseSessionTests.make_session(self)
        session.snapshot(3)
        output = {"event_1": {"detected": True, "intervals": [{"start": 0, "end": 3}],
                              "evidence": "Chewing", "pet_segmentation_prompt": "dog",
                              "object_segmentation_prompt": "sofa"}}
        session.future.set_result((deepcopy(session.active), {"status": "completed", "output": output,
                                   "finished_after_run_seconds": 4}))
        state = session.snapshot(4)
        self.assertTrue(state["detections"])
        self.assertEqual(state["events"][0]["longest_detected_seconds"], 3)

    def test_finished_session_delivers_image_and_shuts_worker_without_repeating_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            executor = test_live_muse.ManualExecutor()
            images = AlertImages(Path(temp), "secret", executor=executor)
            alert = {"id": "a"*24, "event_id": "event_1", "supporting_window_ids": ["0"],
                     "evidence_span": {"start": 0, "end": 3}}
            state = {"id": "b"*32, "status": "completed", "alerts": [alert]}
            windows = [{"id": "0", "file": "windows/0/clip.mp4", "start_seconds": 0}]
            records = {"0": {"output": {"event_1": {"intervals": [{"start": 0, "end": 3}],
                        "pet_segmentation_prompt": "dog", "object_segmentation_prompt": "sofa"}}}}
            images.update(state, windows, records)
            self.assertTrue(state["evidence_pending"])
            executor.calls[0][0].set_result({"status": "ready", "original": "original.jpg", "zoom": "zoom.jpg"})
            images.update(state, windows, records)
            self.assertFalse(state["evidence_pending"])
            self.assertTrue(executor.closed)
            self.assertEqual(alert["visual_evidence"]["snapshot_url"], f"/alert-images/{'b'*32}/{'a'*24}/zoom.jpg")
            images.update(state, windows, records)
            self.assertEqual(len(executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
