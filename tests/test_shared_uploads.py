import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai import OpenAI
import httpx2 as httpx

from pawception.sam_evidence import segment_video
from pawception.shared_uploads import SharedUploads
from pawception.muse import analyze_video, BASE_URL


class SharedUploadTests(unittest.TestCase):
    def test_stop_does_not_delete_while_sam_is_still_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            pool, delete = self.pool(Path(temp))
            upload = pool.for_window("0")
            upload.register("file-shared")
            upload.finish_spark(True)
            upload.acquire("sam:a")
            pool.close()
            delete.assert_not_called()
            self.assertIsNone(upload.acquire("sam:late"))
            upload.release("sam:a")
            self.assertTrue(pool.wait(2))
            delete.assert_called_once()

    def pool(self, root, delete=None):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.files.delete = delete or Mock(return_value=SimpleNamespace(deleted=True))
        return SharedUploads(root, "test-secret", client_factory=lambda: client), client.files.delete

    def test_upload_is_deleted_once_only_after_all_sam_consumers_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            pool, delete = self.pool(Path(temp))
            upload = pool.for_window("0")
            upload.register("file-shared")
            upload.finish_spark(True)
            self.assertEqual(upload.acquire("sam:a"), "file-shared")
            self.assertEqual(upload.acquire("sam:b"), "file-shared")
            pool.finish_planning([{"id": "0", "status": "completed"}])
            upload.release("sam:a")
            delete.assert_not_called()
            upload.release("sam:b")
            self.assertTrue(pool.wait(2))
            upload.release("sam:b")
            pool.close()
            delete.assert_called_once_with("file-shared")
            self.assertEqual(upload.snapshot()["status"], "deleted")

    def test_no_alert_and_spark_failure_both_release_uploads(self):
        for success in [True, False]:
            with self.subTest(success=success), tempfile.TemporaryDirectory() as temp:
                pool, delete = self.pool(Path(temp))
                upload = pool.for_window("0")
                upload.register("file-shared")
                upload.finish_spark(success)
                pool.finish_planning([{"id": "0", "status": "completed"}])
                self.assertTrue(pool.wait(2))
                delete.assert_called_once()

    def test_stop_waits_for_inflight_spark_and_registration_after_stop_is_cleaned(self):
        for registered in [True, False]:
            with self.subTest(registered=registered), tempfile.TemporaryDirectory() as temp:
                pool, delete = self.pool(Path(temp))
                upload = pool.for_window("0")
                if registered:
                    upload.register("file-shared")
                pool.close()
                delete.assert_not_called()
                if not registered:
                    upload.register("file-shared")
                upload.finish_spark(True)
                self.assertTrue(pool.wait(2))
                self.assertIsNone(upload.acquire("sam:late"))
                delete.assert_called_once()

    def test_cleanup_failure_is_recorded_without_leaking_credentials_or_repeating_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            pool, delete = self.pool(Path(temp), Mock(side_effect=RuntimeError("test-secret")))
            upload = pool.for_window("0")
            upload.register("file-shared")
            upload.finish_spark(False)
            self.assertTrue(pool.wait(2))
            result = json.loads((Path(temp)/"windows/0/upload.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("test-secret", json.dumps(result))
            upload.release("handoff")
            delete.assert_called_once()

    def test_spark_http_failure_cleans_shared_upload_without_a_sam_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=root/"clip.mp4"
            source.write_bytes(b"clip")
            output=root/"results"
            output.mkdir()
            calls=[]
            def handler(request):
                calls.append(request.method)
                if request.method=="DELETE":
                    return httpx.Response(200,json={"id":"file-shared","object":"file","deleted":True})
                if request.url.path=="/v1/files":
                    return httpx.Response(200,json={"id":"file-shared","object":"file"})
                return httpx.Response(403,json={"error":{"message":"no access","type":"permission_error"}})
            def client():
                return OpenAI(api_key="secret",base_url=BASE_URL,max_retries=0,
                              http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            pool=SharedUploads(root,"secret",client_factory=client)
            upload=pool.for_window("0")
            with client() as api:
                result=analyze_video(api,source,{"describe":"Describe"},output,"muse-spark-1.3-contributor",1024,shared_upload=upload)
            self.assertTrue(pool.wait(2))
            self.assertEqual(result["analyses"]["describe"]["status"],"error")
            self.assertIsNone(upload.acquire("sam:late"))
            self.assertEqual(calls,["POST","POST","DELETE"])

    def test_spark_and_sam_reference_same_id_with_only_one_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root/"clip.mp4"
            video.write_bytes(b"video")
            from PIL import Image
            image = root/"frame.jpg"
            Image.new("RGB", (10,10)).save(image)
            calls = []
            def handler(request):
                calls.append((request.method,request.url.path))
                if request.method == "DELETE":
                    return httpx.Response(200,json={"id":"file-shared","object":"file","deleted":True})
                if request.url.path == "/v1/files":
                    return httpx.Response(200,json={"id":"file-shared","object":"file"})
                body = json.loads(request.read())
                media = body["input"][0]["content"][1]
                self.assertEqual(media["file_id"],"file-shared")
                self.assertNotIn("image_url",media)
                model = body["model"]
                text = "A cat moves." if model.startswith("muse") else ""
                response = {"id":"resp-test","object":"response","created_at":0,"model":model,"status":"completed",
                            "output":[{"id":"msg-test","type":"message","role":"assistant","status":"completed",
                                       "content":[{"type":"output_text","text":text,"annotations":[]}]}]}
                if body.get("stream"):
                    event = json.dumps({"type":"response.completed","response":response})
                    return httpx.Response(200,headers={"content-type":"text/event-stream"},content=f"data: {event}\n\ndata: [DONE]\n\n")
                return httpx.Response(200,json=response)
            def client():
                return OpenAI(api_key="test-secret",base_url=BASE_URL,max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            pool = SharedUploads(root,"test-secret",client_factory=client)
            upload = pool.for_window("0")
            output=root/"spark"
            output.mkdir()
            with client() as api:
                result=analyze_video(api,video,{"describe":"Describe."},output,"muse-spark-1.3-contributor",1024,shared_upload=upload)
            self.assertEqual(result["upload_cleanup"],"shared_pending")
            self.assertEqual(calls,[('POST','/v1/files'),('POST','/v1/responses')])
            file_id=upload.acquire("sam:alert")
            pool.finish_planning([{"id":"0","status":"completed"}])
            try:
                with client() as api:
                    result=segment_video(api,image,file_id,0,"cat",root/"sam")
                self.assertEqual(result["segments"],[])
                self.assertNotIn(('DELETE','/v1/files/file-shared'),calls)
            finally:
                upload.release("sam:alert")
            self.assertTrue(pool.wait(2))
            self.assertEqual(calls.count(('POST','/v1/files')),1)
            self.assertEqual(calls[-1],('DELETE','/v1/files/file-shared'))


if __name__ == "__main__":
    unittest.main()
