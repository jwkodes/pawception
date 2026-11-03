from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pawception.server import ReplayHandler, monitor_source


class Request:
    def __init__(self, path, body, content_type="application/json"):
        self.path = path
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)

    def respond(self, status, content):
        self.status, self.content = status, content


class MonitorAPITests(unittest.TestCase):
    def test_safety_editor_forwards_both_fields_to_requested_session(self):
        manager = Mock()
        manager.update.return_value = {"safety_prompt": "Look for staples.", "safety_exclusions": "Ignore cushion chewing."}
        session_id = "a"*32
        value = manager.update.return_value
        request = Request(f"/api/monitor/{session_id}/safety", json.dumps(value).encode())
        with patch("pawception.server.MONITOR", manager):
            ReplayHandler.do_POST(request)
        self.assertEqual(request.status, 200)
        manager.update.assert_called_once_with(session_id, "safety", value)

    def test_live_safety_toggle_updates_only_the_requested_session(self):
        manager = Mock()
        manager.update.return_value = {"safety_watch": False}
        session_id = "a"*32
        request = Request(f"/api/monitor/{session_id}/safety", b'{"enabled":false}')
        with patch("pawception.server.MONITOR", manager):
            ReplayHandler.do_POST(request)
        self.assertEqual(request.status, 200)
        manager.update.assert_called_once_with(session_id, "safety", {"enabled": False})

    def test_muse_start_resolves_only_an_allowed_source_before_creating_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "data/demo/generated/online/01_cat_in_can.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"test video")
            manager = Mock()
            manager.create.return_value = {"provider": "muse"}
            body = {"provider": "muse", "source_id": "sample"}
            request = Request("/api/monitor", json.dumps(body).encode())
            with patch("pawception.server.ROOT", root), patch("pawception.server.MONITOR", manager):
                ReplayHandler.do_POST(request)
                self.assertIsNone(monitor_source("../../.env"))
            self.assertEqual(request.status, 200)
            manager.create.assert_called_once_with(body, root=root, source=source)

    def test_selected_file_copy_uses_a_generated_id_and_has_no_arbitrary_path_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = Request("/api/monitor/source", b"local-video-bytes", "application/octet-stream")
            with patch("pawception.server.ROOT", root):
                ReplayHandler.do_POST(request)
                self.assertEqual(request.status, 200)
                source = monitor_source(request.content["source_id"])
                self.assertEqual(source.read_bytes(), b"local-video-bytes")
                source.unlink()
                source.symlink_to(root / "private.mp4")
                self.assertIsNone(monitor_source(request.content["source_id"]))

    def test_oversized_source_and_invalid_json_are_rejected(self):
        source = Request("/api/monitor/source", b"", "application/octet-stream")
        source.headers.replace_header("Content-Length", str(201 * 1024 * 1024))
        ReplayHandler.do_POST(source)
        self.assertEqual(source.status, 400)
        request = Request("/api/monitor", b"[]")
        ReplayHandler.do_POST(request)
        self.assertEqual(request.status, 400)

    def test_viewed_endpoint_updates_only_the_selected_alert(self):
        manager = Mock()
        manager.update.return_value = {"viewed_alert_ids": ["selected"]}
        session_id = "a" * 32
        request = Request(f"/api/monitor/{session_id}/viewed", b'{"alert_id":"selected"}')
        with patch("pawception.server.MONITOR", manager):
            ReplayHandler.do_POST(request)
        self.assertEqual(request.status, 200)
        manager.update.assert_called_once_with(session_id, "viewed", {"alert_id": "selected"})
