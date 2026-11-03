"""A bounded, independent SAM worker; alert delivery never waits for images."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

from pawception.safety import identity
from pawception.sam_evidence import create_evidence


def evidence_request(alert, windows, records):
    for window in reversed(windows):
        if window["id"] not in alert["supporting_window_ids"]:
            continue
        output = records.get(window["id"], {}).get("output") or {}
        if alert.get("source") == "safety":
            answer = next((h for h in output.get("hazards", [])
                           if (h["id"] or identity(h)) == alert["event_id"]), None)
        else:
            answer = output.get(alert["event_id"])
        if not answer:
            continue
        for part in answer["intervals"]:
            start = max(part["start"], alert["evidence_span"]["start"]-window["start_seconds"])
            end = min(part["end"], alert["evidence_span"]["end"]-window["start_seconds"])
            if end > start:
                timestamp = answer.get('evidence_timestamp')
                frame = timestamp if timestamp is not None and start <= timestamp < end else start + min(1, (end-start)/2)
                return {"window_id": window["id"], "clip": window["file"], "frame_seconds": frame,
                        "window_start_seconds": window["start_seconds"],
                        "source_seconds": window["start_seconds"] + frame,
                        "prompts": {"pet": answer.get("pet_segmentation_prompt"),
                                    "object": answer.get("object_segmentation_prompt")}}
    return None


class AlertImages:
    def __init__(self, directory, api_key, *, executor=None):
        self.directory, self.api_key = directory, api_key
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="pawception-sam")
        self.jobs = {}
        self.closed = False
        self.cancelled = Event()

    def update(self, state, windows, records):
        for alert in state["alerts"]:
            key = alert["id"]
            if key not in self.jobs and not self.closed:
                request = evidence_request(alert, windows, records)
                if request is None:
                    continue
                folder = self.directory / "evidence" / key
                pending = sum(bool(job["future"] and not job["future"].done()) for job in self.jobs.values())
                # A full queue falls back to the ordinary alert; it cannot grow forever.
                if pending >= 3:
                    self.jobs[key] = {"future": None, "request": request}
                else:
                    future = self.executor.submit(create_evidence, self.directory / request["clip"],
                                                  request["frame_seconds"], request["prompts"], folder, self.api_key,
                                                  self.cancelled)
                    self.jobs[key] = {"future": future, "request": request}
            job = self.jobs.get(key)
            if not job:
                continue
            future = job["future"]
            path = self.directory / "evidence" / key / "result.json"
            result = {"status": "queued" if future else "unavailable"}
            if path.exists():
                result = json.loads(path.read_text())
            if future and future.done():
                if future.cancelled() or future.exception():
                    result.update(status="unavailable")
                else:
                    result = future.result()
            base = f"/alert-images/{state['id']}/{key}/"
            visual = {"status": result["status"], "source_seconds": job["request"]["window_start_seconds"]
                      + result.get("frame_seconds", job["request"]["frame_seconds"])}
            for field in ("original", "zoom", "highlighted"):
                if result.get(field):
                    visual[field + "_url"] = base + result[field]
            visual["snapshot_url"] = visual.get("zoom_url") or visual.get("original_url")
            alert["visual_evidence"] = visual
        state["evidence_pending"] = any(job["future"] and not job["future"].done() for job in self.jobs.values())
        if state["status"] in {"stopped", "error"} or state["status"] == "completed" and not state["evidence_pending"]:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.cancelled.set()
            self.executor.shutdown(wait=False, cancel_futures=True)
