import json
from pathlib import Path
import tempfile
import unittest

from pawception.pipeline_store import pipeline_detail, pipeline_index, pipeline_media, pipeline_window


class PipelineStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_id = "20261028T030000_123456Z"
        self.folder = self.root / "data/pipeline_runs" / self.run_id
        (self.folder / "windows/0000").mkdir(parents=True)
        self.source = self.root / "data/source.mp4"
        self.source.write_bytes(b"test video")
        (self.folder / "windows/0000/clip.mp4").write_bytes(b"window")
        self.run = {"id": self.run_id, "version": 3, "provider": "mock", "status": "completed",
                    "source": {"file": str(self.source)}, "api_key": "must-not-be-served",
                    "windows": [{"id": "0000", "file": "windows/0000/clip.mp4"}]}
        self.save()
        (self.folder / "replay.json").write_text('{"summary": {"alerts": 1}}')

    def save(self):
        (self.folder / "run.json").write_text(json.dumps(self.run))

    def test_only_explicit_run_media_are_served(self):
        self.assertEqual(pipeline_media(self.root, self.run_id, "source.mp4"), self.source)
        self.assertEqual(pipeline_media(self.root, self.run_id, "0000.mp4"), self.folder / "windows/0000/clip.mp4")
        self.assertIsNone(pipeline_media(self.root, self.run_id, "0001.mp4"))
        self.assertIsNone(pipeline_media(self.root, self.run_id, "../../.env"))
        self.assertIsNone(pipeline_media(self.root, "../../.env", "source.mp4"))

    def test_media_outside_data_and_symlinks_are_rejected(self):
        outside = self.root / "private.mp4"
        outside.write_bytes(b"private")
        self.run["source"]["file"] = str(outside)
        self.save()
        self.assertIsNone(pipeline_media(self.root, self.run_id, "source.mp4"))
        self.source.unlink()
        self.source.symlink_to(outside)
        self.run["source"]["file"] = str(self.source)
        self.save()
        self.assertIsNone(pipeline_media(self.root, self.run_id, "source.mp4"))

    def test_run_and_window_readers_do_not_leak_extra_top_level_fields(self):
        detail = pipeline_detail(self.root, self.run_id)
        self.assertNotIn("must-not-be-served", json.dumps(detail))
        self.assertEqual(pipeline_index(self.root)["runs"][0]["summary"]["alerts"], 1)
        self.assertIsNotNone(pipeline_window(self.root, self.run_id, "0000"))
        self.assertIsNone(pipeline_window(self.root, self.run_id, "../.env"))
        self.assertIsNone(pipeline_window(self.root, self.run_id, "0001"))

    def test_legacy_runs_are_retained_but_not_listed_as_duration_runs(self):
        for version in (1, 2):
            self.run["version"] = version
            self.save()
            self.assertEqual(pipeline_index(self.root)["runs"], [])
            self.assertIsNotNone(pipeline_detail(self.root, self.run_id))


if __name__ == "__main__":
    unittest.main()
