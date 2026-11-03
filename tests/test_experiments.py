import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.experiments import estimated_cost, experiment_detail, experiment_index
from pawception.server import ReplayHandler


class ExperimentViewerTests(unittest.TestCase):
    def test_old_experiment_uses_archived_hash_and_original_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "20261027T120000_123456Z"
            clip = root / "data/experiments" / run_id / "01_example"
            clip.mkdir(parents=True)
            (clip.parent / "run.json").write_text(json.dumps({"model": "test", "modes": ["monitor"]}))
            (clip / "result.json").write_text(json.dumps({"video": "example.mp4", "sha256": "original-hash", "analyses": {}}))
            for path in ("videos/example.mp4", "archive/high_res/example.mp4"):
                file = root / "data" / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"video")
            (root / "data/sources.json").write_text(json.dumps({"videos": [{"file": "videos/example.mp4", "sha256": "480p-hash", "original": {"file": "archive/high_res/example.mp4", "sha256": "original-hash", "metadata": {"width": 3840, "height": 2160}}}]}))
            (root / "data/video_metadata.json").write_text(json.dumps([{"file": "videos/example.mp4", "width": 854, "height": 480}]))
            value = experiment_detail(root, run_id)["clips"][0]
            self.assertEqual(value["media_url"], "/media/archive/example.mp4")
            self.assertEqual(value["metadata"]["width"], 3840)
            (clip / "result.json").write_text(json.dumps({"video": "example.mp4", "sha256": "unknown-hash", "analyses": {}}))
            self.assertIsNone(experiment_detail(root, run_id)["clips"][0]["media_url"])

    def test_estimate_counts_cached_input_and_reasoning_once(self):
        usage = {"input_tokens":1000,"input_tokens_details":{"cached_tokens":400},"output_tokens":2000,"output_tokens_details":{"reasoning_tokens":1500}}
        self.assertAlmostEqual(estimated_cost(usage,"muse-spark-1.3-contributor"), 0.0004608)
        self.assertIsNone(estimated_cost(None,"muse-spark-1.3-contributor"))
        self.assertIsNone(estimated_cost(usage,"unknown-model"))
        self.assertIsNone(estimated_cost({"input_tokens":-1,"output_tokens":0},"muse-spark-1.3-contributor"))

    def test_partial_run_reads_per_clip_files_without_exposing_unlisted_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_id = "20261027T120000_123456Z"
            run = root / "data/experiments" / run_id
            clip = run / "01_example"
            clip.mkdir(parents=True)
            (run / "run.json").write_text(json.dumps({"model":"muse-spark-1.3-contributor","prompts":{"describe":"Exact prompt"},"modes":["describe","hazards"],"planned_videos":["example.mp4"],"api_key":"must-not-export","results":[]}))
            (clip / "result.json").write_text(json.dumps({"video":"example.mp4","api_key":"must-not-export","status":"analyzing","analyses":{"describe":{"status":"completed","usage":{"input_tokens":1962,"output_tokens":1743},"api_key":"must-not-export"},"hazards":{"status":"running"}}}))
            (clip / "describe.txt").write_text("Exact model output")
            detail = experiment_detail(root,run_id)
            self.assertEqual(detail["clips"][0]["analyses"]["describe"]["output_text"],"Exact model output")
            self.assertEqual(detail["summary"]["completed"],1)
            self.assertEqual(detail["summary"]["planned_analyses"],2)
            self.assertAlmostEqual(detail["summary"]["estimated_cost_usd"],0.0005448)
            self.assertEqual(detail["summary"]["usage_records"],1)
            self.assertNotIn("must-not-export",json.dumps(detail))
            self.assertIsNone(detail["clips"][0]["media_url"])
            self.assertEqual(len(experiment_index(root)["runs"]),1)

    def test_invalid_run_ids_and_incomplete_json_are_not_served(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(experiment_detail(root,"../../.env"))
            self.assertIsNone(experiment_detail(root,"/etc/passwd"))
            run = root / "data/experiments/20261027T120000_123456Z"
            run.mkdir(parents=True)
            (run / "run.json").write_text('{"model":')
            self.assertEqual(experiment_index(root)["runs"],[])

    def test_video_seeking_serves_only_curated_files_and_valid_byte_ranges(self):
        class Request:
            def __init__(self, value):
                self.headers = {"Range":value}
                self.wfile = io.BytesIO()
                self.response_headers = {}
            def send_response(self, status):
                self.status = status
            def send_header(self, name, value):
                self.response_headers[name] = value
            def end_headers(self):
                pass
            def respond(self, status, _):
                self.status = status
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data/videos").mkdir(parents=True)
            (root / "data/videos/clip.mp4").write_bytes(b"0123456789")
            (root / "data/sources.json").write_text(json.dumps({"videos":[{"file":"videos/clip.mp4"}]}))
            for value, code, body in (("bytes=2-5",206,b"2345"),("bytes=-3",206,b"789"),("bytes=50-",416,b""),("bytes=0-1,4-5",416,b"")):
                with self.subTest(value=value), patch("pawception.server.ROOT",root):
                    request = Request(value)
                    ReplayHandler.serve_video(request,"clip.mp4")
                    self.assertEqual(request.status,code)
                    self.assertEqual(request.wfile.getvalue(),body)
            with patch("pawception.server.ROOT",root):
                request = Request("bytes=0-")
                ReplayHandler.serve_video(request,"../../.env")
                self.assertEqual(request.status,404)


if __name__ == "__main__":
    unittest.main()
