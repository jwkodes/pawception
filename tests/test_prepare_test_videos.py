import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pawception import assets as fixtures


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.content = b"test video contents"
        self.entry = {
            "file": "videos/clip.mp4",
            "download_url": "https://example.com/clip.mp4",
            "bytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }

    def test_download_is_verified_before_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(fixtures.urllib.request, "urlopen", return_value=io.BytesIO(self.content)):
                self.assertEqual(fixtures.prepare_file(self.entry, root), "downloaded and verified")
            self.assertEqual((root / self.entry["file"]).read_bytes(), self.content)

    def test_existing_file_is_verified_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / self.entry["file"]
            target.parent.mkdir()
            target.write_bytes(self.content)
            with patch.object(fixtures.urllib.request, "urlopen", side_effect=AssertionError("Network called")):
                self.assertEqual(fixtures.prepare_file(self.entry, root), "verified")

    def test_checksum_mismatch_leaves_no_video_or_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = b"x" * len(self.content)
            with patch.object(fixtures.urllib.request, "urlopen", return_value=io.BytesIO(wrong)):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    fixtures.prepare_file(self.entry, root)
            self.assertFalse((root / self.entry["file"]).exists())
            self.assertEqual(list((root / "videos").iterdir()), [])

    def test_verify_only_never_downloads_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(fixtures.urllib.request, "urlopen", side_effect=AssertionError("Network called")):
                with self.assertRaises(FileNotFoundError):
                    fixtures.prepare_file(self.entry, Path(directory), verify_only=True)

    def test_missing_working_copy_is_recreated_from_verified_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {**self.entry, "file": "archive/high_res/clip.mp4"}
            source = root / original["file"]
            source.parent.mkdir(parents=True)
            source.write_bytes(self.content)
            derived = b"small encoded video"
            entry = {**self.entry, "original": original, "bytes": len(derived), "sha256": hashlib.sha256(derived).hexdigest()}
            def transcode(path, output):
                self.assertEqual(path, source)
                self.assertEqual(path.read_bytes(), self.content)
                output.write_bytes(derived)
            with patch("pawception.video.transcode_video", side_effect=transcode), patch.object(fixtures.urllib.request, "urlopen", side_effect=AssertionError("Network called")):
                self.assertEqual(fixtures.prepare_file(entry, root), "recreated 480p and verified")
            self.assertEqual((root / entry["file"]).read_bytes(), derived)
            self.assertEqual(source.read_bytes(), self.content)


if __name__ == "__main__":
    unittest.main()
