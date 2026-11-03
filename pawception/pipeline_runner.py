"""Prepare windows, execute inference, and run or resume a video pipeline.

Keep orchestration here and event/confirmation logic in pawception.pipeline.
One standalone run represents one continuous source excerpt. Live monitoring
and saved-recording analysis also reuse the preparation and inference helpers.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI

from pawception.monitor import monitor_format
from pawception.pipeline import build_pipeline_replay, fingerprint, perception_prompt, validate_config, TERMINAL
from pawception.video_windows import frame_windows
from pawception.files import checksum, write_json
from pawception.paths import ROOT
from pawception.video import ffmpeg, probe_video
from pawception.muse import analyze_video, BASE_URL

MODEL = "muse-spark-1.3-contributor"


def prepare_windows(video, windows, directory, *, include_audio=False, source_probe=None):
    """Encode independently playable inputs while preserving source-frame PTS."""
    source, source_timeline, time_base = source_probe or probe_video(video)
    if source["width"] >= source["height"]:
        scale = f"-2:{min(480, source['height'])}"
    else:
        scale = f"{min(480, source['width'])}:-2"
    for window in windows:
        folder = directory / "windows" / window["id"]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "clip.mp4"
        count = window["source_end_frame_exclusive"] - window["source_start_frame"]
        if not target.exists():
            temporary = folder / "clip.partial.mp4"
            temporary.unlink(missing_ok=True)
            ffmpeg("-n", "-ss", f"{window['start_seconds']:.12f}", "-i", video,
                   "-map", "0:v:0",
                   *(["-map", "0:a:0?", "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-t", window["input_duration_seconds"]] if include_audio else ["-an"]),
                   *([] if include_audio else ["-frames:v", count]),
                   "-vf", f"trim=end_frame={count},scale={scale}:flags=lanczos,setpts=PTS-STARTPTS",
                   "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
                   "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
                   "-video_track_timescale", time_base.denominator, "-map_metadata", "-1",
                   "-movflags", "+faststart", temporary)
            temporary.replace(target)
        metadata, timeline, _ = probe_video(target)
        expected = source_timeline[window["source_start_frame"]:window["source_end_frame_exclusive"]]
        origin = expected[0][0]
        if [pts for pts, _ in timeline] != [pts - origin for pts, _ in expected]:
            raise ValueError(f"Window {window['id']} changed source frame timestamps")
        digest = checksum(target)
        if window.get("sha256") and window["sha256"] != digest:
            raise ValueError(f"Cached window {window['id']} changed")
        ffmpeg("-i", target, "-map", "0:v:0", "-f", "null", "-")
        window.update(file=str(target.relative_to(directory)), sha256=digest,
                      input_duration_seconds=metadata["duration_seconds"], frame_count=metadata["frame_count"])


def mock_response(window, task, fixture):
    """Synthetic yes/no detections, not labels for the accompanying footage."""
    output = {}
    for event in task["events"]:
        intervals = []
        for interval in fixture.get("events", {}).get(event["id"], []):
            start = max(window["start_seconds"], interval["start"]) - window["start_seconds"]
            end = min(window["input_duration_seconds"], interval["end"] - window["start_seconds"])
            if end > start:
                intervals.append({"start": start, "end": end})
        output[event["id"]] = {
            "detected": bool(intervals),
            "intervals": intervals,
            "evidence": "Synthetic response for testing; not a conclusion about this footage.",
        }
    return output


def analyze_window(window, run, directory, api_key, fixture, run_clock, shared_upload=None, cancelled=None, on_result_ready=None):
    window_started = time.monotonic()
    folder = directory / "windows" / window["id"]
    prompt = perception_prompt(run["config"]["task"], window["input_duration_seconds"])
    record = {"status": "completed", "cache_key": fingerprint([window["sha256"], prompt, run["request"]]),
              "prompt": prompt, "source_available_seconds": window["end_seconds"] - run["windows"][0]["start_seconds"]}

    def update_result(result):
        analysis = result.get("analyses", {}).get("monitor", {})
        record.update(
            status="completed" if analysis.get("status") == "completed" else "invalid" if analysis.get("status") == "invalid" else "error",
            output=analysis.get("structured_output"), error=analysis.get("error") or result.get("upload_error") or analysis.get("validation_error"),
            inference_seconds=analysis.get("inference_seconds"), usage=analysis.get("usage"),
            upload_seconds=result.get("upload_seconds"), upload_cleanup=result.get("upload_cleanup"),
            cleanup_seconds=result.get("cleanup_seconds"), result_ready_seconds=result.get("result_ready_seconds"),
            cleanup_error=result.get("cleanup_error"), file_id=result.get("file_id"),
        )

    def ready(result):
        update_result(result)
        if record['status'] != 'completed':
            return
        started, previous_elapsed = run_clock
        record.update(finished_after_run_seconds=previous_elapsed + time.monotonic() - started,
                      processing_seconds=time.monotonic() - window_started,
                      finished_at=datetime.now(timezone.utc).isoformat())
        write_json(folder / "observation.json", record)
        if on_result_ready is not None:
            from copy import deepcopy
            on_result_ready(deepcopy(record))
    if run["provider"] == "mock":
        record["output"] = mock_response(window, run["config"]["task"], fixture)
    else:
        with OpenAI(api_key=api_key, base_url=BASE_URL, timeout=180, max_retries=0) as client:
            result = analyze_video(client, directory / window["file"], {"monitor": prompt}, folder,
                                   run["request"]["model"], run["request"]["max_output_tokens"],
                                   monitor_task=run["config"]["task"], clip_duration=window["input_duration_seconds"],
                                   monitor_output_format="binary", response_options=run["request"]["options"],
                                   shared_upload=shared_upload, media_kind=run["request"].get("media_kind", "video"), cancelled=cancelled,
                                   on_result_ready=ready)
        update_result(result)
    started, previous_elapsed = run_clock
    record.setdefault("finished_after_run_seconds", previous_elapsed + time.monotonic() - started)
    record.setdefault("processing_seconds", time.monotonic() - window_started)
    record.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
    record["cleanup_finished_after_run_seconds"] = previous_elapsed + time.monotonic() - started
    # Save before notifying the scheduler, so a crash between completion and
    # updating run.json can recover the result without another paid call.
    write_json(folder / "observation.json", record)
    return record


def save_replay(run, directory):
    replay = build_pipeline_replay(run["config"], run["windows"], run["records"])
    write_json(directory / "replay.json", replay)
    write_json(directory / "run.json", run)
    return replay


def process_run(run, directory, *, api_key=None, fixture=None, workers=2, paced=False, retry_failed=False):
    """Only the main thread updates run state; at most workers calls are queued."""
    started = time.monotonic()
    run["workers"] = workers
    previous_elapsed = max((r.get("finished_after_run_seconds", 0) for r in run["records"].values()), default=0)
    pending = []
    for window in run["windows"]:
        record = run["records"].get(window["id"], {})
        if record.get("status") == "running":
            receipt = directory / "windows" / window["id"] / "observation.json"
            if receipt.is_file():
                record = json.loads(receipt.read_text())
                run["records"][window["id"]] = record
        if record.get("status") == "completed":
            prompt = perception_prompt(run["config"]["task"], window["input_duration_seconds"])
            expected = fingerprint([window["sha256"], prompt, run["request"]])
            if record.get("cache_key") != expected:
                raise ValueError("Cached response does not match its window and request")
        if record.get("status") == "running":
            # A crashed call may already have been billed. Never retry it silently.
            record["status"] = "interrupted"
            record["error"] = "Previous call was interrupted; use --retry-failed to request another call"
        if record.get("status") in TERMINAL and not (retry_failed and record["status"] != "completed"):
            continue
        pending.append(window)
    run["status"] = "running"
    save_replay(run, directory)
    captured_through = max(
        (w["end_seconds"] - run["windows"][0]["start_seconds"]
         for w in run["windows"] if run["records"].get(w["id"], {}).get("status") in TERMINAL),
        default=0,
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = {}
        stopped = False
        while pending or active:
            while pending and len(active) < workers and not stopped:
                window = pending[0]
                ready = window["end_seconds"] - run["windows"][0]["start_seconds"]
                if paced and captured_through + time.monotonic() - started < ready:
                    break
                pending.pop(0)
                run["records"][window["id"]] = {"status": "running"}
                write_json(directory / "run.json", run)
                active[pool.submit(analyze_window, window, run, directory, api_key, fixture,
                                   (started, previous_elapsed))] = window
            if not active:
                if stopped:
                    break
                time.sleep(0.1)
                continue
            done, _ = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                window = active.pop(future)
                try:
                    record = future.result()
                except Exception as error:
                    record = {"status": "error", "error": f"Window processing failed: {type(error).__name__}"}
                record.setdefault("finished_after_run_seconds", previous_elapsed + time.monotonic() - started)
                record.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
                run["records"][window["id"]] = record
                write_json(directory / "windows" / window["id"] / "observation.json", record)
                error = record.get("error")
                if isinstance(error, dict) and error.get("status_code") in {401, 402, 403, 429}:
                    stopped = True
                replay = save_replay(run, directory)
                print(f"Window {window['id']}: {record['status']} · processed through {replay['summary']['finalized_through']:.2f}s", flush=True)
            if stopped and not active:
                break
    replay = build_pipeline_replay(run["config"], run["windows"], run["records"])
    run["status"] = "completed" if all(w["status"] == "completed" for w in replay["windows"]) else "has_errors"
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(directory / "run.json", run)
    write_json(directory / "replay.json", replay)
    return replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", type=Path)
    parser.add_argument("--config", type=Path, help="Required for a new run; resume keeps the saved event task")
    parser.add_argument("--provider", choices=("mock", "muse"), help="Default: mock; resume keeps the saved provider")
    parser.add_argument("--model", help=f"Default: {MODEL}")
    parser.add_argument("--window-seconds", type=float, help="Default: 3")
    parser.add_argument("--overlap-seconds", type=float, help="Default: 0")
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--paced", action="store_true", help="Release each window only after its source end time")
    parser.add_argument("--mock-fixture", type=Path, help="Required for a new mock run; resume keeps saved observations")
    parser.add_argument("--resume", type=Path, help="Resume this saved run; successful windows are never called again")
    parser.add_argument("--replay-only", action="store_true", help="Rebuild saved observations without any model calls")
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly authorize new calls for failed/interrupted windows")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.resume:
        directory = args.resume.resolve()
        run = json.loads((directory / "run.json").read_text())
        config = validate_config(json.loads(args.config.read_text()) if args.config else run["config"])
        if config["task"] != run["config"]["task"]:
            parser.error("Changing the event task requires a new run; cached responses describe the saved task")
        upgrading = run.get("version") != 3
        if upgrading and (run.get("version") != 2 or not args.replay_only):
            parser.error("Rebuild binary version-2 runs with --config and --replay-only to change the alert rule")
        if args.provider is not None and args.provider != run["provider"]:
            parser.error("Resume keeps the saved provider; start a new run to change it")
        if args.model is not None and args.model != run["request"]["model"]:
            parser.error("Resume keeps the saved model; start a new run to change it")
        if any(value is not None for value in (args.start, args.end, args.window_seconds, args.overlap_seconds)):
            parser.error("Resume keeps saved source boundaries and window settings")
        video = Path(run["source"]["file"])
        if checksum(video) != run["source"]["sha256"]:
            parser.error("Source video changed since this run")
        if upgrading and not args.dry_run:
            # Keep the original two-window policy and replay for comparison.
            for name in ("run", "replay"):
                original = directory / f"{name}.json"
                backup = directory / f"{name}.before-duration-alerts.json"
                if original.exists() and not backup.exists():
                    backup.write_bytes(original.read_bytes())
        run.update(config=config, version=3)
    else:
        if not args.video or args.replay_only:
            parser.error("Supply a video, or use --resume with --replay-only")
        if args.config is None:
            parser.error("--config is required when starting a new run")
        if (args.provider or "mock") == "mock" and args.mock_fixture is None:
            parser.error("--mock-fixture is required when starting a new mock run")
        video = args.video.resolve()
        config = validate_config(json.loads(args.config.read_text()))
        metadata, timeline, _ = probe_video(video)
        window_seconds = 3 if args.window_seconds is None else args.window_seconds
        overlap_seconds = 0 if args.overlap_seconds is None else args.overlap_seconds
        windows = frame_windows(timeline, start=0 if args.start is None else args.start, end=args.end,
                                window_seconds=window_seconds, overlap_seconds=overlap_seconds)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        directory = ROOT / "data/pipeline_runs" / run_id
        run = {
            "id": run_id, "version": 3, "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "preparing", "provider": args.provider or "mock", "config": config,
            "source": {"file": str(video), "sha256": checksum(video), "bytes": video.stat().st_size, **metadata},
            "window_seconds": window_seconds, "overlap_seconds": overlap_seconds,
            "mode": "paced" if args.paced else "batch", "windows": windows, "records": {},
            "request": {"model": args.model or MODEL, "max_output_tokens": 8192,
                        "options": {"stream": True, "temperature": 1, "top_p": 1,
                                    "reasoning": {"effort": "medium"},
                                    "text": {"format": monitor_format(config["task"], "binary")}}},
            "uv_lock_sha256": checksum(ROOT / "uv.lock"),
            "timeline_note": "One continuous excerpt using source playback seconds. Processing time is recorded separately.",
        }
        if run["provider"] == "mock":
            run["mock_fixture"] = json.loads(args.mock_fixture.read_text())
        for path in (ROOT / "data/candidates").glob("*.json"):
            source = json.loads(path.read_text())
            working = source.get("working", {})
            if working.get("sha256") == run["source"]["sha256"]:
                run["source_context"] = source
                break
    print(f"{run['provider']} · {len(run['windows'])} windows · {run['window_seconds']:g}s / {run['overlap_seconds']:g}s overlap", flush=True)
    print(f"Run: {directory}", flush=True)
    if args.dry_run:
        print("Dry run: no files changed, credentials read, or model calls.")
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "run.json", run)
    if args.replay_only:
        replay = save_replay(run, directory)
    else:
        prepare_windows(video, run["windows"], directory)
        api_key = None
        needs_calls = any(
            run["records"].get(w["id"], {}).get("status") not in TERMINAL | {"running"}
            or args.retry_failed and run["records"].get(w["id"], {}).get("status") != "completed"
            for w in run["windows"]
        )
        if run["provider"] == "muse" and needs_calls:
            load_dotenv(ROOT / ".env", override=False)
            api_key = os.environ.get("MODEL_API_KEY", "").strip()
            if not api_key:
                parser.error("Add MODEL_API_KEY to .env")
        fixture = run.get("mock_fixture")
        replay = process_run(run, directory, api_key=api_key, fixture=fixture, workers=args.workers,
                             paced=run["mode"] == "paced", retry_failed=args.retry_failed)
    print(f"{replay['summary']['alerts']} alerts · {replay['summary']['finalized_windows']} processed windows")
    return 0 if run["status"] == "completed" or args.replay_only else 1
