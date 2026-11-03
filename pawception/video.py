"""Video probing, transcoding, and verified 480p working-copy preparation."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import imageio_ffmpeg

from pawception.paths import ROOT
from pawception.files import checksum, write_json

PROFILE = {"short_edge": 480, "video_codec": "libx264", "crf": 23, "preset": "medium", "pixel_format": "yuv420p", "audio": "copy", "fps": "preserve", "faststart": True}


def ffmpeg(*args):
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *map(str, args)], check=True, capture_output=True, text=True)


def probe_video(path):
    """Use packet presentation timestamps, rather than rounded container duration."""
    result = ffmpeg("-i", path, "-map", "0:v:0", "-c:v", "copy", "-f", "framehash", "-")
    time_base = None
    dimensions = None
    packets = []
    for line in result.stdout.splitlines():
        if line.startswith("#tb 0:"):
            time_base = Fraction(line.split(":", 1)[1].strip())
        elif line.startswith("#dimensions 0:"):
            dimensions = tuple(map(int, line.split(":", 1)[1].strip().split("x")))
        elif line and not line.startswith("#"):
            fields = [part.strip() for part in line.split(",")]
            packets.append((int(fields[2]), int(fields[3])))
    if not time_base or not dimensions or not packets:
        raise ValueError("Cannot inspect video timestamps and dimensions")
    origin = min(pts for pts, _ in packets)
    timeline = sorted(((pts - origin) * time_base, duration * time_base) for pts, duration in packets)
    duration = max(pts + length for pts, length in timeline)
    metadata = {"width": dimensions[0], "height": dimensions[1], "duration_seconds": float(duration), "fps": float(len(packets) / duration), "frame_count": len(packets), "playable": True}
    return metadata, timeline, time_base


def transcode_video(source, target):
    original, timeline, time_base = probe_video(source)
    scale = "-2:480" if original["width"] >= original["height"] else "480:-2"
    ffmpeg("-n", "-i", source, "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale={scale}:flags=lanczos", "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-video_track_timescale", time_base.denominator, "-c:a", "copy", "-map_metadata", "-1", "-movflags", "+faststart", target)
    encoded, encoded_timeline, _ = probe_video(target)
    if min(encoded["width"], encoded["height"]) != 480 or timeline != encoded_timeline:
        raise ValueError("Transcode changed video timing or did not produce 480p")
    # Decode every output frame before installing the working copy.
    ffmpeg("-i", target, "-map", "0:v:0", "-f", "null", "-")
    return original, encoded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    data = ROOT / "data"
    archive = data / "archive/high_res"
    if archive.exists():
        manifest = json.loads((archive / "manifest.json").read_text())
        for record in manifest["videos"]:
            for version in (record["original"], record["working"]):
                path = data / version["file"]
                if path.stat().st_size != version["bytes"] or checksum(path) != version["sha256"]:
                    raise ValueError(f"Video differs from its manifest: {path}")
        print("Verified all 480p working copies and archived originals. No files changed.")
        return 0
    if args.verify_only:
        parser.error("No archive exists yet")
    sources = json.loads((data / "sources.json").read_text())
    metadata = json.loads((data / "video_metadata.json").read_text())
    by_name = {Path(entry["file"]).name: entry for entry in sources["videos"]}
    old_meta = {Path(entry["file"]).name: entry for entry in metadata}
    videos = sorted((data / "videos").glob("*.mp4"))
    if not videos:
        parser.error("No local videos")
    records = []
    # Finish and verify every transcode before moving any original.
    with tempfile.TemporaryDirectory(prefix=".480p-", dir=data) as staging_dir:
        staging = Path(staging_dir)
        for video in videos:
            digest = checksum(video)
            source = by_name.get(video.name)
            if source is None:
                source = next((entry for entry in sources["videos"] if entry["sha256"] == digest), None)
                if source is None:
                    raise ValueError(f"No provenance for {video.name}")
                source = copy.deepcopy(source)
                source["alias_of"] = source["file"]
                source["file"] = f"videos/{video.name}"
            if digest != source["sha256"] or video.stat().st_size != source["bytes"]:
                raise ValueError(f"Source checksum mismatch: {video.name}")
            original, working = transcode_video(video, staging / video.name)
            original.update(file=f"archive/high_res/{video.name}", bytes=video.stat().st_size, sha256=digest)
            working.update(file=f"videos/{video.name}", bytes=(staging / video.name).stat().st_size, sha256=checksum(staging / video.name))
            records.append({"original": original, "working": working, "source": source})
            print(f"{video.name}: {original['width']}x{original['height']} -> {working['width']}x{working['height']}; {original['bytes']:,} -> {working['bytes']:,} bytes; {working['frame_count']} frames verified", flush=True)
        archive.mkdir(parents=True)
        write_json(archive / "sources.json", sources)
        write_json(archive / "video_metadata.json", metadata)
        manifest = {"profile": PROFILE, "ffmpeg_version": imageio_ffmpeg.get_ffmpeg_version(), "imageio_ffmpeg_version": imageio_ffmpeg.__version__, "videos": records}
        write_json(archive / "manifest.json", manifest)
        current_sources = []
        current_metadata = []
        for record in records:
            original, working = record["original"], record["working"]
            name = Path(working["file"]).name
            (data / working["file"]).rename(data / original["file"])
            (staging / name).replace(data / working["file"])
            source = copy.deepcopy(record["source"])
            source.update(bytes=working["bytes"], sha256=working["sha256"], processing=PROFILE,
                          original={"file": original["file"], "bytes": original["bytes"], "sha256": original["sha256"], "download_url": source["download_url"], "metadata": {**old_meta.get(name, {}), **original}})
            current_sources.append(source)
            current_metadata.append({**working, "decoded_preview_frames": old_meta.get(name, {}).get("decoded_preview_frames", [])})
        sources["videos"] = current_sources
        sources["notes"].append("Active videos are 480p derivatives. Original source downloads and metadata are preserved under archive/high_res; original download URLs refer to those sources.")
        write_json(data / "sources.json", sources)
        write_json(data / "video_metadata.json", current_metadata)
    total_original = sum(r["original"]["bytes"] for r in records)
    total_working = sum(r["working"]["bytes"] for r in records)
    print(f"Archived originals: {total_original:,} bytes; working clips: {total_working:,} bytes ({100 * (1 - total_working / total_original):.1f}% smaller)")
    return 0
