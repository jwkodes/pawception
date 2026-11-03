import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
from openai import OpenAI

from pawception import muse as experiment


class ExperimentTests(unittest.TestCase):
    def run_clip(self, handler, prompts=None, **options):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "dog_chewing_expected_answer.mp4"
            video.write_bytes(b"test video bytes")
            output = root / "results"
            output.mkdir()
            with OpenAI(
                api_key="test-secret",
                base_url=experiment.BASE_URL,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                max_retries=0,
            ) as client:
                result = experiment.analyze_video(
                    client, video, prompts or {"describe": "Describe the visible actions."},
                    output, "muse-spark-1.3", 4096, **options,
                )
            artifacts = {path.name: path.read_text() for path in output.iterdir()}
            return result, artifacts

    @staticmethod
    def response(status="completed"):
        return {
            "id": "resp-test", "object": "response", "created_at": 0,
            "model": "muse-spark-1.3", "status": status,
            "output": [{
                "id": "msg-test", "type": "message", "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "A dog moves.", "annotations": []}],
            }] if status == "completed" else [],
            "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
            "incomplete_details": {"reason": "max_output_tokens"} if status != "completed" else None,
        }

    def test_neutral_upload_independent_prompts_and_cleanup(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/v1/files":
                body = request.read()
                self.assertIn(b'filename="clip.mp4"', body)
                self.assertNotIn(b"dog_chewing_expected_answer", body)
                return httpx.Response(200, json={"id": "file-test", "object": "file"})
            if request.method == "DELETE":
                return httpx.Response(200, json={"id": "file-test", "object": "file", "deleted": True})
            payload = json.loads(request.read())
            self.assertEqual(len(payload["input"]), 1)
            self.assertNotIn("previous_response_id", payload)
            self.assertFalse(payload["store"])
            self.assertEqual(payload["input"][0]["content"][1]["file_id"], "file-test")
            return httpx.Response(200, json=self.response())

        result, files = self.run_clip(handler, {"describe": "Describe.", "hazards": "Assess hazards."})
        self.assertEqual(len(requests), 4)
        self.assertEqual(result["upload_cleanup"], "deleted")
        self.assertTrue(all(item["status"] == "completed" for item in result["analyses"].values()))
        self.assertEqual(result["analyses"]["describe"]["usage"]["total_tokens"], 20)
        self.assertIn("hazards.response.json", files)

    def test_failed_analysis_still_deletes_upload_and_omits_error_body(self):
        deleted = []

        def handler(request):
            if request.method == "DELETE":
                deleted.append(True)
                return httpx.Response(200, json={"id": "file-test", "object": "file", "deleted": True})
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-test", "object": "file"})
            return httpx.Response(401, json={"error": {"message": "Bad key test-secret", "type": "invalid_api_key"}})

        result, files = self.run_clip(handler)
        self.assertTrue(deleted)
        self.assertEqual(result["analyses"]["describe"]["error"]["status_code"], 401)
        self.assertNotIn("test-secret", json.dumps(files))

    def test_incomplete_response_is_not_reported_as_success(self):
        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(200, json={"id": "file-test", "object": "file", "deleted": True})
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-test", "object": "file"})
            return httpx.Response(200, json=self.response("incomplete"))

        result, files = self.run_clip(handler)
        self.assertEqual(result["analyses"]["describe"]["status"], "incomplete")
        self.assertIn("describe.response.json", files)

    def test_monitor_sends_task_and_schema_and_saves_validated_observations(self):
        from test_monitor import TASK, observations
        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(200,json={"id":"file-test","object":"file","deleted":True})
            if request.url.path == "/v1/files":
                return httpx.Response(200,json={"id":"file-test","object":"file"})
            payload = json.loads(request.read())
            self.assertTrue(payload["text"]["format"]["strict"])
            prompt = payload["input"][0]["content"][0]["text"]
            self.assertIn(TASK["rules"][0]["owner_request"],prompt)
            self.assertIn("7.2 seconds",prompt)
            response = self.response()
            response["output"][0]["content"][0]["text"] = json.dumps(observations())
            return httpx.Response(200,json=response)
        result, files = self.run_clip(handler,{"monitor":experiment.PROMPT_FILES["monitor"].read_text()},monitor_task=TASK,clip_duration=7.2)
        self.assertEqual(result["analyses"]["monitor"]["validation_status"],"valid")
        self.assertEqual(json.loads(files["monitor.observations.json"]),observations())
        self.assertEqual(result["upload_cleanup"],"deleted")

    def test_dry_run_never_creates_an_api_client_or_results(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"test")
            output = Path(directory) / "results"
            with patch.object(experiment, "OpenAI", side_effect=AssertionError("Network client created")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = experiment.main([str(video), "--dry-run", "--output-dir", str(output)])
            self.assertEqual(code, 0)
            self.assertFalse(output.exists())

    def test_simple_monitor_sends_new_schema_and_retains_exact_prompt(self):
        from test_monitor import TASK
        value = {rule["id"]: {"assessment": "observed", "intervals": [{"start": 0, "end": 7.2}], "evidence": "Behavior in sequence", "uncertainty": None} for rule in TASK["rules"]}
        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(200, json={"id": "file-test", "object": "file", "deleted": True})
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-test", "object": "file"})
            payload = json.loads(request.read())
            self.assertEqual(payload["text"]["format"]["type"], "json_schema")
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertIn("drinking", payload["text"]["format"]["schema"]["properties"])
            response = self.response()
            response["output"][0]["content"][0]["text"] = json.dumps(value)
            return httpx.Response(200, json=response)
        result, files = self.run_clip(handler, {"monitor": "Analyze 0 to {{DURATION_SECONDS}} seconds."}, monitor_task=TASK, clip_duration=7.2, monitor_output_format="simple")
        self.assertEqual(result["analyses"]["monitor"]["validation_status"], "valid")
        self.assertEqual(json.loads(files["monitor.observations.json"]), value)
        self.assertEqual(files["monitor.prompt.txt"].strip(), "Analyze 0 to 7.2 seconds.")

    def test_streamed_reproduction_preserves_options_and_saves_final_response(self):
        from test_monitor import TASK
        from pawception.monitor import monitor_format
        value = {rule["id"]: {"assessment": "observed", "intervals": [{"start": 0, "end": 7.2}], "evidence": "Behavior in sequence", "uncertainty": None} for rule in TASK["rules"]}
        schema = monitor_format(TASK, "simple")
        schema.pop("strict")  # The playground export did not set this parameter.
        options = {"stream": True, "temperature": 1, "top_p": 1, "reasoning": {"effort": "medium"}, "text": {"format": schema}}
        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(200, json={"id": "file-test", "object": "file", "deleted": True})
            if request.url.path == "/v1/files":
                return httpx.Response(200, json={"id": "file-test", "object": "file"})
            payload = json.loads(request.read())
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["reasoning"], {"effort": "medium"})
            self.assertNotIn("strict", payload["text"]["format"])
            self.assertEqual([m["role"] for m in payload["input"]], ["user"])
            response = self.response()
            response["output"][0]["content"][0]["text"] = json.dumps(value)
            event = {"type": "response.completed", "sequence_number": 0, "response": response}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode())
        # An exported request already has its complete prompt, so only rule IDs
        # are needed for output validation, not a prompt-template task.
        task_ids = {"rules": [{"id": r["id"]} for r in TASK["rules"]]}
        result, files = self.run_clip(handler, {"monitor": "Analyze behavior."}, monitor_task=task_ids, clip_duration=7.2, monitor_output_format="simple", response_options=options)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(json.loads(files["monitor.observations.json"]), value)
        self.assertIn("response.completed", files["monitor.events.jsonl"])

    def test_both_remains_two_calls_and_does_not_include_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"test")
            stdout = io.StringIO()
            with patch.object(experiment,"OpenAI",side_effect=AssertionError("Network client created")), contextlib.redirect_stdout(stdout):
                self.assertEqual(experiment.main([str(video),"--mode","both","--dry-run"]),0)
            self.assertIn("inference calls: 2",stdout.getvalue())
            self.assertIn("describe, hazards",stdout.getvalue())

    def test_missing_key_stops_before_uploads_or_results(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"test")
            output = Path(directory) / "results"
            with patch.dict(os.environ, {"MODEL_API_KEY": ""}):
                with patch.object(experiment, "load_dotenv"):
                    with patch.object(experiment, "OpenAI", side_effect=AssertionError("Network client created")):
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            with self.assertRaises(SystemExit) as error:
                                experiment.main([str(video), "--output-dir", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
