"""Meta video/audio transport and the standalone understanding experiment runner."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI

from pawception.paths import ROOT
from pawception.files import write_json
from pawception.monitor import monitor_format, render_monitor_prompt, validate_observations, validate_task

BASE_URL = "https://api.meta.ai/v1"
PROMPT_FILES = {
    "describe": ROOT / "prompts/video_description.txt",
    "hazards": ROOT / "prompts/video_hazards.txt",
    "monitor": ROOT / "prompts/video_monitor.txt",
}


def safe_error(error: Exception) -> dict:
    """Record useful error categories without logging credentials or HTTP bodies."""
    result = {"type": type(error).__name__}
    if isinstance(error, APIStatusError):
        result["status_code"] = error.status_code
        result["request_id"] = error.request_id
    elif isinstance(error, APIConnectionError):
        result["detail"] = "Could not reach Meta Model API. Check network access."
    return result


def analyze_video(
    client: OpenAI,
    video: Path,
    prompts: dict[str, str],
    output: Path,
    model: str,
    max_output_tokens: int,
    keep_uploads: bool = False,
    monitor_task: dict | None = None,
    clip_duration: float | None = None,
    monitor_output_format: str = "temporal",
    response_options: dict | None = None,
    shared_upload=None,
    media_kind="video",
    cancelled=None,
    on_result_ready=None,
) -> dict:
    request_started = time.perf_counter()
    if media_kind not in {"video", "audio"}:
        raise ValueError("Choose video or audio input")
    filename, mime = ("clip.wav", "audio/wav") if media_kind == "audio" else ("clip.mp4", "video/mp4")
    if shared_upload is not None and keep_uploads:
        raise ValueError("Shared uploads manage their own cleanup")
    allowed_options = {"stream", "temperature", "top_p", "reasoning", "text"}
    if response_options is not None and set(response_options) - allowed_options:
        raise ValueError("Unsupported response options")
    if media_kind == "audio":
        if shared_upload is not None or keep_uploads:
            raise ValueError("Audio is sent inline and has no retained upload")
        from pawception.audio_inference import analyze_audio
        return analyze_audio(client, video, prompts, output, model, max_output_tokens,
                             monitor_task, clip_duration, monitor_output_format, response_options, cancelled)
    result = {
        "video": str(video.resolve()),
        "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "bytes": video.stat().st_size,
        "upload_filename": filename,
        "media_kind": media_kind,
        "status": "uploading",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "analyses": {},
    }
    if monitor_task is not None:
        result["clip_duration_seconds"] = clip_duration
    file_id = None
    write_json(output / "result.json", result)
    try:
        if (cancelled is not None and cancelled.is_set()) or (shared_upload is not None and shared_upload.cancelled.is_set()):
            from concurrent.futures import CancelledError
            raise CancelledError("Monitoring stopped before upload")
        started = time.perf_counter()
        # Descriptive local filenames must not leak the expected action to the model.
        with video.open("rb") as stream:
            uploaded = client.files.create(
                file=(filename, stream, mime), purpose="user_data"
            )
        file_id = uploaded.id
        if shared_upload is not None:
            shared_upload.register(file_id)
        result.update(file_id=file_id, upload_seconds=time.perf_counter() - started, status="analyzing")
        for mode, prompt in prompts.items():
            options = {}
            if mode == "monitor":
                prompt = render_monitor_prompt(prompt, monitor_task, clip_duration)
                options["text"] = {"format": monitor_format(monitor_task, monitor_output_format)}
            options.update(response_options or {})
            record = {"prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "requested_model": model, "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
            if "text" in options:
                record["response_format"] = options["text"]["format"]
            if response_options is not None:
                record["request_options"] = response_options
            (output / f"{mode}.prompt.txt").write_text(prompt + "\n", encoding="utf-8")
            result["analyses"][mode] = record
            write_json(output / "result.json", result)
            started = time.perf_counter()
            try:
                if (cancelled is not None and cancelled.is_set()) or (shared_upload is not None and shared_upload.cancelled.is_set()):
                    from concurrent.futures import CancelledError
                    raise CancelledError("Monitoring stopped before inference")
                # Each analysis starts a fresh conversation, including in --mode both.
                response = client.responses.create(
                    model=model,
                    input=[{
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_audio" if media_kind == "audio" else "input_file", "file_id": file_id},
                        ],
                    }],
                    max_output_tokens=max_output_tokens,
                    store=False,
                    **options,
                )
                if options.get("stream"):
                    final_response = None
                    # Save each event as it arrives; do not reconstruct final JSON
                    # from deltas or silently repeat a partially streamed call.
                    with response as events, (output / f"{mode}.events.jsonl").open("w") as log:
                        for event in events:
                            log.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
                            log.flush()
                            if event.type in {"response.completed", "response.incomplete", "response.failed"}:
                                final_response = event.response
                    if final_response is None:
                        raise ValueError("Stream ended without a final response")
                    response = final_response
                raw = response.model_dump(mode="json")
                write_json(output / f"{mode}.response.json", raw)
                text = response.output_text
                (output / f"{mode}.txt").write_text(text + "\n")
                completed = response.status == "completed" and bool(text.strip())
                record.update(
                    status="completed" if completed else "incomplete",
                    response_status=response.status,
                    response_id=response.id,
                    returned_model=response.model,
                    usage=raw.get("usage"),
                    incomplete_details=raw.get("incomplete_details"),
                )
                if mode == "monitor" and completed:
                    try:
                        observations = validate_observations(text, monitor_task, clip_duration, monitor_output_format)
                        record.update(validation_status="valid", structured_output=observations)
                        write_json(output / "monitor.observations.json", observations)
                    except (ValueError, TypeError) as error:
                        record.update(status="invalid", validation_status="invalid", validation_error=str(error))
            except Exception as error:
                record.update(status="error", error=safe_error(error))
            finally:
                record["inference_seconds"] = time.perf_counter() - started
                result["analyses"][mode] = record
                write_json(output / "result.json", result)
    except Exception as error:
        result["upload_error"] = safe_error(error)
    else:
        result["result_ready_seconds"] = time.perf_counter() - request_started
        result["result_ready_at"] = datetime.now(timezone.utc).isoformat()
        # Notify before deleting the upload. The finally block still owns cleanup,
        # including if the consumer fails while publishing the observation.
        if on_result_ready is not None:
            on_result_ready(result)
    finally:
        cleanup_started = time.perf_counter()
        if shared_upload is not None:
            success = bool(result["analyses"]) and not result.get("upload_error") and all(
                record["status"] == "completed" for record in result["analyses"].values())
            shared_upload.finish_spark(success)
            result["upload_cleanup"] = "shared_pending" if file_id else "not_uploaded"
            result["upload_receipt"] = "upload.json"
        elif file_id:
            if keep_uploads:
                result["upload_cleanup"] = "retained_by_request"
            else:
                try:
                    result["status"] = "cleaning_up"
                    write_json(output / "result.json", result)
                    receipt = client.files.delete(file_id)
                    result["upload_cleanup"] = "deleted" if receipt.deleted else "not_confirmed"
                except Exception as error:
                    result["upload_cleanup"] = "failed"
                    result["cleanup_error"] = safe_error(error)
        result["status"] = "completed" if not result.get("upload_error") and all(r["status"] == "completed" for r in result["analyses"].values()) and result.get("upload_cleanup") not in {"failed", "not_confirmed"} else "has_errors"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["cleanup_seconds"] = time.perf_counter() - cleanup_started
        write_json(output / "result.json", result)
    return result


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="*", type=Path, help="MP4 paths; defaults to data/videos/*.mp4")
    parser.add_argument("--mode", choices=["describe", "hazards", "both", "monitor"], default="describe")
    parser.add_argument("--monitor-task", type=Path, help="Task JSON with subject, scene context, and atomic event rules; required in monitor mode")
    parser.add_argument("--monitor-format", choices=["temporal", "simple"], default="temporal", help="Simple uses the shorter prompt and assessment/intervals/evidence/uncertainty schema")
    parser.add_argument("--model", help="Overrides META_MODEL from .env")
    parser.add_argument("--limit", type=positive_int, help="Process only the first N sorted videos")
    parser.add_argument("--max-output-tokens", type=positive_int, default=4096)
    parser.add_argument("--timeout", type=positive_int, default=180, help="HTTP timeout in seconds")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/experiments")
    parser.add_argument("--keep-uploads", action="store_true", help="Retain uploaded files on Meta after the run")
    parser.add_argument("--dry-run", action="store_true", help="List the plan without uploads or model calls")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    model = args.model or os.environ.get("META_MODEL", "muse-spark-1.3-contributor")
    videos = sorted(set(args.videos or (ROOT / "data/videos").glob("*.mp4")))
    if args.limit:
        videos = videos[:args.limit]
    if not videos:
        parser.error("No videos found. Supply MP4 paths or add files to data/videos.")
    for video in videos:
        if not video.is_file() or video.suffix.lower() != ".mp4":
            parser.error(f"Not an MP4 file: {video}")
        if not 0 < video.stat().st_size <= 1_073_741_824:
            parser.error(f"File must be nonempty and at most 1 GiB: {video}")
    modes = ["describe", "hazards"] if args.mode == "both" else [args.mode]
    prompts = {mode: PROMPT_FILES[mode].read_text().strip() for mode in modes}
    if args.monitor_format != "temporal" and args.mode != "monitor":
        parser.error("--monitor-format is used only with --mode monitor")
    if args.mode == "monitor" and args.monitor_format == "simple":
        prompts["monitor"] = (ROOT / "prompts/video_monitor_simple.txt").read_text().strip()
    task = None
    durations = {}
    if args.mode == "monitor":
        if args.monitor_task is None:
            parser.error("--mode monitor requires --monitor-task")
        try:
            task = validate_task(json.loads(args.monitor_task.read_text()))
            metadata = json.loads((ROOT / "data/video_metadata.json").read_text())
            durations = {m["file"]: m["duration_seconds"] for m in metadata}
            for video in videos:
                relative = str(video.resolve().relative_to(ROOT / "data"))
                if relative not in durations:
                    raise ValueError("Monitor mode requires a known duration in data/video_metadata.json")
        except (OSError, ValueError) as error:
            parser.error(str(error))
    elif args.monitor_task is not None:
        parser.error("--monitor-task is used only with --mode monitor")
    print(f"Meta endpoint: {BASE_URL}")
    print(f"Model: {model}; videos: {len(videos)}; inference calls: {len(videos) * len(modes)}")
    if args.dry_run:
        for video in videos:
            print(f"  {video.name}: {video.stat().st_size / 1_000_000:.1f} MB; {', '.join(modes)}")
        if task:
            print(f"Task: {task['name']}; rules: {', '.join(r['id'] for r in task['rules'])}")
            for video in videos:
                duration = durations[str(video.resolve().relative_to(ROOT / "data"))]
                print(render_monitor_prompt(prompts["monitor"], task, duration))
        print("Dry run complete. No files uploaded and no API calls made.")
        return 0
    api_key = os.environ.get("MODEL_API_KEY", "").strip()
    if not api_key or api_key.lower() in {"your-api-key-here", "your_meta_api_key_here"}:
        parser.error("Add MODEL_API_KEY to the project .env file before running. Use --dry-run to preview.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output_dir = args.output_dir.resolve() / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    run = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": BASE_URL,
        "model": model,
        "max_output_tokens": args.max_output_tokens,
        "python_version": platform.python_version(),
        "sdk_version": importlib.metadata.version("openai"),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "python_version_pin": (ROOT / ".python-version").read_text().strip(),
        "timeout_seconds": args.timeout,
        "prompt_sha256": {mode: hashlib.sha256(prompt.encode("utf-8")).hexdigest() for mode, prompt in prompts.items()},
        "modes": modes,
        "status": "running",
        "planned_videos": [str(video.resolve()) for video in videos],
        "max_retries": 0,
        "store_responses": False,
        "keep_uploads": args.keep_uploads,
        "reasoning_effort": "API default (not set)",
        "video_sampling": "API managed (no sampling parameters set)",
        "prompts": prompts,
        "results": [],
    }
    if task:
        run.update(monitor_task=task, monitor_task_path=str(args.monitor_task.resolve()),
                   monitor_task_sha256=hashlib.sha256(args.monitor_task.read_bytes()).hexdigest(),
                   response_format=monitor_format(task, args.monitor_format),
                   prompt_scope="Monitor prompt template; exact rendered prompts are saved per analysis")
    write_json(output_dir / "run.json", run)
    failed = False
    # Disable automatic retries so a failed experiment doesn't silently repeat paid calls.
    with OpenAI(api_key=api_key, base_url=BASE_URL, timeout=args.timeout, max_retries=0) as client:
        for index, video in enumerate(videos, 1):
            clip_output = output_dir / f"{index:02d}_{video.stem}"
            clip_output.mkdir()
            run["current_video"] = str(video.resolve())
            write_json(output_dir / "run.json", run)
            print(f"[{index}/{len(videos)}] {video.name}", flush=True)
            duration = durations.get(str(video.resolve().relative_to(ROOT / "data"))) if task else None
            result = analyze_video(client, video, prompts, clip_output, model, args.max_output_tokens, args.keep_uploads, task, duration, args.monitor_format)
            run["results"].append(result)
            if "upload_error" in result:
                failed = True
                print(f"  Upload failed: {json.dumps(result['upload_error'])}")
            for mode, record in result["analyses"].items():
                print(f"  {mode}: {record['status']} ({record['inference_seconds']:.1f}s)")
                failed |= record["status"] != "completed"
            if result.get("upload_cleanup") in {"failed", "not_confirmed"}:
                failed = True
                print(f"  Upload cleanup not confirmed; file ID saved in result.json: {result['file_id']}")
            write_json(output_dir / "run.json", run)
            errors = [result.get("upload_error", {})] + [r.get("error", {}) for r in result["analyses"].values()]
            if any(error.get("status_code") in {401, 402, 403, 429} for error in errors):
                print("Stopping remaining videos: check API access, credits, or rate limits.")
                break
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    run.pop("current_video", None)
    run["status"] = "has_errors" if failed else "completed"
    write_json(output_dir / "run.json", run)
    print(f"Results: {output_dir}")
    return 1 if failed else 0
