"""Read-only access to saved pipeline runs and their explicitly listed videos."""

from pathlib import Path
import re

from pawception.experiments import read_json, RUN_ID


def run_directory(root, run_id):
    if not RUN_ID.fullmatch(run_id):
        return None
    directory = root / "data/pipeline_runs" / run_id
    if directory.is_symlink() or not directory.is_dir():
        return None
    return directory


def pipeline_detail(root, run_id):
    directory = run_directory(root, run_id)
    if directory is None:
        return None
    run = read_json(directory / "run.json")
    replay = read_json(directory / "replay.json")
    if not isinstance(run, dict):
        return None
    fields = ("id", "status", "provider", "source", "source_context", "mode", "request", "config",
              "started_at", "finished_at", "window_seconds", "overlap_seconds", "workers", "timeline_note")
    return {"run": {key: run[key] for key in fields if key in run}, "replay": replay,
            "media_url": f"/pipeline-media/{run_id}/source.mp4" if pipeline_media(root, run_id, "source.mp4") else None}


def pipeline_index(root):
    runs = []
    for path in sorted((root / "data/pipeline_runs").glob("*"), reverse=True):
        directory = run_directory(root, path.name)
        if directory is None:
            continue
        run = read_json(directory / "run.json")
        replay = read_json(directory / "replay.json") or {}
        if isinstance(run, dict) and run.get("version") == 3:
            runs.append({"id": path.name, "status": run.get("status"), "provider": run.get("provider"),
                         "started_at": run.get("started_at"), "summary": replay.get("summary", {})})
    return {"runs": runs}


def pipeline_media(root, run_id, name):
    directory = run_directory(root, run_id)
    if directory is None:
        return None
    run = read_json(directory / "run.json") or {}
    if name == "source.mp4":
        path = Path(run.get("source", {}).get("file", ""))
        allowed = root / "data"
    elif re.fullmatch(r"\d{4}\.mp4", name):
        window_id = name.removesuffix(".mp4")
        window = next((w for w in run.get("windows", []) if w["id"] == window_id), None)
        if window is None or window.get("file") != f"windows/{window_id}/clip.mp4":
            return None
        path = directory / window["file"]
        allowed = directory / "windows" / window_id
    else:
        return None
    if path.suffix != ".mp4" or path.is_symlink() or not path.is_file():
        return None
    if not path.resolve().is_relative_to(allowed.resolve()):
        return None
    return path


def pipeline_window(root, run_id, window_id):
    directory = run_directory(root, run_id)
    if directory is None or not re.fullmatch(r"\d{4}", window_id):
        return None
    run = read_json(directory / "run.json") or {}
    if not any(w["id"] == window_id for w in run.get("windows", [])):
        return None
    folder = directory / "windows" / window_id
    if folder.is_symlink():
        return None
    return {"result": read_json(folder / "result.json"),
            "response": read_json(folder / "monitor.response.json"),
            "media_url": f"/pipeline-media/{run_id}/{window_id}.mp4"}
