"""Read saved experiment artifacts for the local developer UI. Never invokes Muse."""

import json
import hashlib
import re
from pathlib import Path

RUN_ID = re.compile(r"\d{8}T\d{6}_\d{6}Z")
PRICING = {
    "as_of": "2026-10-27",
    "source": "https://dev.meta.ai/docs/pricing-rate-limits",
    "currency": "USD",
    "per_million_tokens": {
        "muse-spark-1.3-contributor": {"input": 0.10, "cached_input": 0.002, "output": 0.20},
        "muse-spark-1.3": {"input": 1.25, "cached_input": 0.15, "output": 4.25},
    },
}
RUN_FIELDS = ("started_at", "finished_at", "status", "endpoint", "model", "max_output_tokens", "python_version", "sdk_version", "uv_lock_sha256", "python_version_pin", "timeout_seconds", "prompt_sha256", "modes", "prompts", "max_retries", "store_responses", "reasoning_effort", "video_sampling", "keep_uploads", "planned_videos", "current_video", "configuration_provenance", "monitor_task", "monitor_task_path", "monitor_task_sha256", "response_format", "prompt_scope")
CLIP_FIELDS = ("video", "sha256", "bytes", "upload_filename", "file_id", "upload_seconds", "upload_cleanup", "upload_error", "cleanup_error", "status", "started_at", "finished_at", "clip_duration_seconds", "evaluation_trial")
ANALYSIS_FIELDS = ("prompt", "requested_model", "returned_model", "status", "started_at", "response_status", "response_id", "usage", "incomplete_details", "inference_seconds", "error", "response_format", "validation_status", "validation_error", "structured_output")


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 8_000_000:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def media_assets(root):
    """Serve only manifest-listed working clips and their archived originals."""
    assets = {}
    data = root / "data"
    for source in (read_json(data / "sources.json") or {}).get("videos", []):
        for archived, version in ((False, source), (True, source.get("original"))):
            if not isinstance(version, dict) or not isinstance(version.get("file"), str):
                continue
            path = data / version["file"]
            parent = data / ("archive/high_res" if archived else "videos")
            if path.parent != parent or path.is_symlink() or not path.is_file():
                continue
            if path.resolve().parent != parent.resolve() or not path.resolve().is_relative_to(data.resolve()):
                continue
            key = ("archive/" if archived else "") + path.name
            assets[key] = path
    return assets


def estimated_cost(usage, model):
    """Token-only estimate; unknown usage or rates must not be displayed as zero."""
    rates = PRICING["per_million_tokens"].get(model)
    if not isinstance(usage, dict) or not rates:
        return None
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    if any(type(value) is not int or value < 0 for value in (incoming, outgoing)):
        return None
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    if type(cached) is not int or not 0 <= cached <= incoming:
        return None
    # Reasoning tokens are already part of output_tokens, not an extra charge.
    return ((incoming - cached) * rates["input"] + cached * rates["cached_input"] + outgoing * rates["output"]) / 1_000_000


def experiment_detail(root: Path, run_id: str, *, include_outputs=True):
    if not RUN_ID.fullmatch(run_id):
        return None
    directory = root / "data/experiments" / run_id
    if directory.is_symlink() or not directory.is_dir():
        return None
    run = read_json(directory / "run.json")
    if not isinstance(run, dict):
        return None
    # Optional, explicit context for legacy runs; original request/output logs stay intact.
    context = read_json(directory / "execution_context.json")
    if isinstance(context, dict):
        run = {**pick(context, RUN_FIELDS), **run}
    metadata = read_json(root / "data/video_metadata.json") or []
    sources = (read_json(root / "data/sources.json") or {}).get("videos", [])
    clips = []
    known_costs = []
    completed = 0
    count = 0
    for folder in sorted(directory.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        result = read_json(folder / "result.json")
        if not isinstance(result, dict):
            continue
        filename = Path(result.get("video", "")).name
        source = next((s for s in sources if Path(s["file"]).name == filename), None)
        meta = next((m for m in metadata if Path(m["file"]).name == filename), {})
        clip = {**pick(result, CLIP_FIELDS), "id": folder.name, "filename": filename, "metadata": meta, "source": source, "analyses": {}}
        # Select the exact version used in the saved run, rather than showing a
        # new 480p replacement beside an old high-resolution inference result.
        media_key = filename
        if source and source.get("original", {}).get("sha256") == result.get("sha256") and result.get("sha256"):
            meta = source["original"].get("metadata", {})
            clip["metadata"] = meta
            media_key = f"archive/{filename}"
        elif source and source.get("sha256") and source["sha256"] != result.get("sha256"):
            media_key = None
        clip["media_url"] = f"/media/{media_key}" if media_key in media_assets(root) else None
        previews = meta.get("decoded_preview_frames", [])
        poster = Path(previews[min(1, len(previews) - 1)]).name if previews else None
        clip["poster_url"] = f"/previews/{poster}" if poster and (root / "data/previews" / poster).is_file() else None
        for mode, record in result.get("analyses", {}).items():
            if mode not in {"describe", "hazards", "monitor"} or not isinstance(record, dict):
                continue
            value = pick(record, ANALYSIS_FIELDS)
            if isinstance(record.get("prompt"), str):
                value["prompt_sha256"] = hashlib.sha256(record["prompt"].encode("utf-8")).hexdigest()
            cost = estimated_cost(record.get("usage"), record.get("returned_model") or record.get("requested_model") or run.get("model"))
            value["estimated_cost_usd"] = cost
            if cost is not None:
                known_costs.append(cost)
            count += 1
            completed += record.get("status") == "completed"
            if include_outputs:
                try:
                    text_path = folder / f"{mode}.txt"
                    value["output_text"] = text_path.read_text(encoding="utf-8") if not text_path.is_symlink() and text_path.stat().st_size <= 8_000_000 else ""
                except OSError:
                    value["output_text"] = ""
                value["response"] = read_json(folder / f"{mode}.response.json")
            clip["analyses"][mode] = value
        clips.append(clip)
    expected = len(run.get("planned_videos", [])) * len(run.get("modes", [])) or None
    state = run.get("status") or ("running_or_interrupted" if not run.get("finished_at") else "unknown")
    detail = {
        "id": run_id, **pick(run, RUN_FIELDS), "status": state, "clips": clips, "pricing": PRICING,
        "summary": {"clips": len(clips), "analyses": count, "completed": completed, "planned_analyses": expected, "estimated_cost_usd": sum(known_costs) if known_costs else None, "usage_records": len(known_costs)},
    }
    labels = root / "data/ground_truth/pet_behaviors.v1.json"
    if include_outputs and labels.is_file() and any("monitor" in clip["analyses"] for clip in clips):
        from pawception.evaluation import evaluate_runs, load_dataset
        try:
            detail["evaluation"] = evaluate_runs(load_dataset(labels), [directory])
        except (OSError, ValueError, KeyError, TypeError):
            detail["evaluation"] = {"error": "Ground-truth labels or saved observations could not be evaluated"}
    return detail


def experiment_index(root: Path):
    directory = root / "data/experiments"
    runs = []
    if directory.is_dir():
        for folder in sorted(directory.iterdir(), reverse=True):
            detail = experiment_detail(root, folder.name, include_outputs=False)
            if detail:
                runs.append({key: detail.get(key) for key in ("id", "started_at", "finished_at", "model", "status", "summary")})
    return {"runs": runs, "read_only": True}
