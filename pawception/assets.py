"""Download verified file assets and restore the evaluation-media manifest."""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from pawception.files import checksum
from pawception.paths import ROOT


def verify_file(file: Path, entry: dict) -> None:
    if file.stat().st_size != entry["bytes"]:
        raise ValueError("File size differs from the source manifest")
    actual_hash = checksum(file)
    if actual_hash != entry["sha256"]:
        raise ValueError("SHA-256 differs from the source manifest")


def prepare_file(entry: dict, data_dir: Path, verify_only: bool = False) -> str:
    target = (data_dir / entry["file"]).resolve()
    target.relative_to(data_dir.resolve())
    if target.exists():
        verify_file(target, entry)
        return "verified"
    if verify_only:
        raise FileNotFoundError("Missing local file")
    url = entry["download_url"]
    if not url.startswith("https://"):
        raise ValueError("Download URL must use HTTPS")
    target.parent.mkdir(parents=True, exist_ok=True)
    if "original" in entry:
        # Source URLs still download the original. Recreate the working variant
        # using the pinned FFmpeg runtime rather than installing it as 480p.
        from pawception.video import transcode_video
        original = entry["original"]
        prepare_file(original, data_dir)
        with tempfile.TemporaryDirectory(dir=target.parent) as directory:
            temporary = Path(directory) / target.name
            transcode_video(data_dir / original["file"], temporary)
            verify_file(temporary, entry)
            temporary.replace(target)
        return "recreated 480p and verified"
    temporary = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Pawception-Test-Fixtures/0.1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > entry["bytes"]:
                        raise ValueError("Download exceeds the expected file size")
                    stream.write(chunk)
        verify_file(temporary, entry)
        temporary.replace(target)
        return "downloaded and verified"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Verify existing files without downloading")
    args = parser.parse_args(argv)
    manifest = json.loads((ROOT / "data/sources.json").read_text(encoding="utf-8"))
    failures = 0
    for entry in manifest["videos"]:
        try:
            status = prepare_file(entry, ROOT / "data", args.verify_only)
            print(f"{entry['file']}: {status}")
        except (OSError, ValueError, urllib.error.URLError) as error:
            failures += 1
            print(f"{entry['file']}: FAILED ({error})")
    if failures:
        print("Do not change the manifest hashes to bypass a mismatch. Check the source file or license first.")
    return 1 if failures else 0
