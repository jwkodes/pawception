"""Shared live-session state and deterministic mock scheduling.

The clock drives fixed windows and one worker. Only two pending windows are kept;
overflow discards the oldest pending job, never the active job. Muse mode is
explicitly selected and delegates real inference to pawception.live_muse.
"""

from __future__ import annotations

import math
import time
from copy import deepcopy
from threading import Lock, Thread
from uuid import uuid4

from pawception.pipeline import build_pipeline_replay
from pawception.safety import SafetyWatch, mock_hazards
from pawception.reasoning import validate_effort
from pawception.event_inputs import MODELS, input_settings

MAX_PENDING = 2


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1–{maximum} characters")
    return value.strip()


class MockSession:
    def __init__(self, settings, *, now=0):
        self.id = uuid4().hex
        self.started = now
        self.duration = number(settings.get("duration"), "Video duration", 0.1, 7200)
        self.source_name = text(settings.get("source_name"), "Source name", 200)
        self.window = number(settings.get("window_seconds"), "Window length", 1, 120)
        self.overlap = number(settings.get("overlap_seconds"), "Overlap", 0, 119)
        self.merge_gap = number(settings.get("merge_gap_seconds", 1), "Event gap tolerance", 0, 30)
        self.safety = SafetyWatch(settings.get("safety_watch", False), settings.get("safety_prompt"), settings.get("safety_exclusions", ""))
        if self.overlap >= self.window:
            raise ValueError("Overlap must be smaller than the window length")
        if self.window - self.overlap < 1:
            raise ValueError("Windows must advance by at least one second")
        self.delay = number(settings.get("mock_delay_seconds", 5), "Mock processing time", 0.1, 300)
        self.model = settings.get("model")
        if self.model not in MODELS:
            raise ValueError("Select a supported model")
        self.video_reasoning_effort = validate_effort(settings.get("video_reasoning_effort", "medium"), self.model)
        self.audio_model = settings.get("audio_model", "muse-spark-1.2-contributor")
        if self.audio_model not in MODELS:
            raise ValueError("Select a supported audio model")
        self.audio_reasoning_effort = validate_effort(settings.get("audio_reasoning_effort", "medium"), self.audio_model)
        self.events = []
        self.windows = []
        self.records = {}
        self.event_replays = {}
        self.viewed_alerts = set()
        self.queue = []
        self.active = None
        self.next_start = 0
        self.time = 0
        self.status = "live"
        self.provider = "mock"
        events = settings.get("events")
        if not isinstance(events, list) or not 0 <= len(events) <= 20 or not events and not self.safety.enabled:
            raise ValueError("Add an event or enable Safety watch")
        for event in events:
            self.add_event(event, now=now)

    def add_event(self, value, *, now):
        self.advance(now)
        if self.status != "live":
            raise ValueError("Events can only be added while the source is live")
        if not isinstance(value, dict) or len(self.events) >= 20:
            raise ValueError("A session supports up to 20 events")
        used_colors = {event["palette_index"] for event in self.events}
        palette_index = value.get("palette_index")
        if type(palette_index) is not int or not 0 <= palette_index < 20 or palette_index in used_colors:
            palette_index = next(index for index in range(20) if index not in used_colors)
        event = {
            "id": f"event_{len(self.events) + 1}",
            **input_settings(value),
            "name": text(value.get("name"), "Event name", 80),
            "description": text(value.get("description"), "Event description", 1000),
            "threshold_seconds": number(value.get("threshold_seconds"), "Alert duration", 0.1, 600),
            "active_from": min(self.time, self.duration),
            "palette_index": palette_index,
        }
        self.events.append(event)
        return event

    def set_safety(self, enabled, *, now):
        if self.status != "live":
            raise ValueError("Safety watch can only be changed while live")
        self.safety.set_enabled(enabled, min(self.duration, max(self.time, now-self.started)))
        self.advance(now)
        self.persist()

    def persist(self):
        pass  # Real/recorded camera sessions override this hook.

    def configure_safety(self, value, *, now):
        if self.status != "live":
            raise ValueError("Safety watch can only be changed while live")
        self.safety.configure(value.get("safety_prompt"), value.get("safety_exclusions", ""))
        self.advance(now)
        self.persist()

    def _take_job(self, at):
        if self.active is not None or not self.queue:
            return None
        if not self.events and not self.safety.enabled:
            for job in self.queue:
                job.update(status="skipped", skip_reason="No monitoring enabled")
            self.queue.clear()
            return None
        job = self.queue.pop(0)
        job["event_ids"] = [event["id"] for event in self.events]
        job["safety"] = self.safety.context()
        job["reasoning_effort"] = self.video_reasoning_effort
        job["audio_model"] = self.audio_model
        job["audio_reasoning_effort"] = self.audio_reasoning_effort
        return job

    def _dispatch(self, at):
        job = self._take_job(at)
        if job is None:
            return
        job.update(status="running", started_at=at, finishes_at=at + self.delay)
        self.records[job["id"]] = {"status": "running"}
        self.active = job

    def _complete(self):
        job = self.active
        output = {}
        for event in self.events:
            if event["id"] not in job["event_ids"]:
                continue
            intervals = []
            # Scripted activity relative to activation, independent of video
            # contents and event wording. This is deliberately not perception.
            for offset, length in ((5, 23), (45, 24)):
                start = max(job["start_seconds"], event["active_from"] + offset)
                end = min(job["end_seconds"], event["active_from"] + offset + length)
                if end > start:
                    intervals.append({"start": start - job["start_seconds"], "end": end - job["start_seconds"]})
            output[event["id"]] = {
                "detected": bool(intervals), "intervals": intervals,
                "evidence": "Scripted mock activity for interface testing. No footage was analyzed.",
            }
        if job.get("safety", {}).get("enabled"):
            output["hazards"] = mock_hazards(job)
        job["status"] = "completed"
        self.records[job["id"]] = {
            "status": "completed", "output": output,
            "finished_after_run_seconds": job["finishes_at"],
        }
        self.event_replays.clear()
        self.safety.observe(job, self.records[job["id"]], self.merge_gap)
        self.active = None

    def advance(self, now):
        if self.status in {"stopped", "completed"}:
            return
        elapsed = max(self.time, now - self.started)
        # Simulate arrivals/completions chronologically even after a tab has
        # been hidden. A slow poll must not change queue eviction or alert times.
        while True:
            arrival = min(self.next_start + self.window, self.duration) if self.next_start is not None else math.inf
            finish = self.active["finishes_at"] if self.active else math.inf
            at = min(arrival, finish)
            if at > elapsed or at == math.inf:
                break
            if finish <= arrival:
                self._complete()
                self._dispatch(at)
            else:
                job = {
                    "id": f"{len(self.windows):04d}", "index": len(self.windows),
                    "start_seconds": self.next_start, "end_seconds": arrival,
                    "input_duration_seconds": arrival - self.next_start, "status": "pending",
                }
                self.windows.append(job)
                self.queue.append(job)
                self.next_start = None if arrival >= self.duration else self.next_start + self.window - self.overlap
                if len(self.queue) > MAX_PENDING:
                    skipped = self.queue.pop(0)
                    skipped["status"] = "skipped"
                self._dispatch(at)
        self.time = elapsed
        if elapsed >= self.duration:
            self.status = "finishing" if self.active or self.queue else "completed"

    def stop(self, now):
        self.advance(now)
        for job in self.queue + ([self.active] if self.active else []):
            job["status"] = "cancelled"
        self.queue.clear()
        self.active = None
        if self.status != "completed":
            self.status = "stopped"

    def view_alert(self, alert_id, now):
        snapshot = self.snapshot(now)
        if not isinstance(alert_id, str) or not any(alert["id"] == alert_id for alert in snapshot["alerts"]):
            raise ValueError("Alert not found in this session")
        self.viewed_alerts.add(alert_id)

    def snapshot(self, now):
        self.advance(now)
        alerts, events, detected = [], [], []
        for event in self.events:
            windows = [job for job in self.windows if job["status"] in {"completed", "partial", "running", "cancelled"}
                       and event["id"] in job.get("event_ids", [])
                       and event["id"] in (self.records.get(job["id"], {}).get("output") or {})]
            if event["id"] not in self.event_replays and windows:
                config = {"version": 3, "task": {"name": event["name"], "events": [
                    {"id": event["id"], "description": event["description"]}]},
                    "alert_after_seconds": {event["id"]: event["threshold_seconds"]},
                    "merge_gap_seconds": self.merge_gap}
                records = {job["id"]: {**self.records[job["id"]], "status": "completed",
                    'finished_after_run_seconds':self.records[job['id']].get('event_finished_after_run_seconds', {}).get(
                        event['id'], self.records[job['id']].get('finished_after_run_seconds', 0)), "output": {
                    event["id"]: {key: self.records[job["id"]]["output"][event["id"]][key]
                                  for key in ("detected", "intervals", "evidence")}
                }} for job in windows}
                self.event_replays[event["id"]] = build_pipeline_replay(config, windows, records)
            replay = self.event_replays.get(event["id"])
            if replay:
                for alert in replay["alerts"]:
                    alerts.append({**alert, "event_name": event["name"],
                                   "evidence": " ".join(dict.fromkeys(item["evidence"] for item in alert["supporting_detections"]))})
                detected.extend(replay["events"])
            attempts = [job for job in self.windows if event["id"] in job.get("event_ids", [])
                        and job["status"] in {"completed", "partial", "error", "invalid", "running"}]
            issue = self.records[attempts[-1]["id"]].get("event_errors", {}).get(event["id"]) if attempts else None
            events.append({**event, "input_issue": issue, "longest_detected_seconds": max(
                (item["detected_seconds"] for item in replay["events"]), default=0) if replay else 0,
                "analyzed_windows": len(windows)})
        safety = self.safety.snapshot(self.merge_gap)
        alerts.extend(safety["alerts"])
        detected.extend(safety["detections"])
        captured = min(self.time, self.duration)
        completed = [job for job in self.windows if job["status"] in {"completed", "partial"}]
        latest = max((job["end_seconds"] for job in completed), default=0)
        return deepcopy({
            "id": self.id, "provider": self.provider, "status": self.status, "source_name": self.source_name, "duration": self.duration,
            "revision": self.time,
            "captured_seconds": captured, "analyzed_through": latest, "lag_seconds": max(0, captured - latest),
            "window_seconds": self.window, "overlap_seconds": self.overlap, "model": self.model,
            "video_reasoning_effort": self.video_reasoning_effort,
            "audio_model": self.audio_model, "audio_reasoning_effort": self.audio_reasoning_effort,
            "merge_gap_seconds": self.merge_gap,
            "safety_watch": self.safety.enabled, **self.safety.settings, "hazards": safety["hazards"],
            "mock_delay_seconds": self.delay, "max_pending": MAX_PENDING,
            "collecting_start": self.next_start, "queue_size": len(self.queue),
            "active_window": self.active, "windows": self.windows, "events": events,
            "detections": detected, "alerts": sorted(alerts, key=lambda alert: (alert["at"], alert["event_id"])),
            "skipped_windows": [job for job in self.windows if job["status"] == "skipped"],
            "partial_windows": [{"id": job["id"], "event_errors": self.records.get(job["id"], {}).get("event_errors", {})}
                                for job in self.windows if job["status"] == "partial"],
            "viewed_alert_ids": sorted(self.viewed_alerts),
            "failed_windows": [{"id": job["id"], "error": self.records.get(job["id"], {}).get("error"),
                                "event_errors": self.records.get(job["id"], {}).get("event_errors", {})}
                               for job in self.windows if job["status"] in {"error", "invalid"}],
        })


class LiveMonitor:
    """One in-memory session for the local prototype, protected across requests."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = Lock()
        self.session = None

    def create(self, settings, *, root=None, source=None):
        with self.lock:
            now = self.clock()
            if self.session and self.session.snapshot(now)["status"] in {"live", "finishing"}:
                raise ValueError("Stop the current session before starting another")
            provider = settings.get("provider", "mock")
            if provider not in {"mock", "muse"}:
                raise ValueError("Choose Mock or Muse mode")
            webcam = settings.get("source_type") == "webcam"
            api_key = None
            if provider == "muse":
                if root is None or source is None and not webcam:
                    raise ValueError("Load a video before starting Muse mode")
                import os
                from dotenv import load_dotenv
                from pawception.live_muse import MuseSession
                load_dotenv(root / ".env", override=False)
                api_key = os.environ.get("MODEL_API_KEY", "").strip()
                if not api_key:
                    raise ValueError("Add MODEL_API_KEY to the local .env file before starting Muse mode")
            if webcam:
                if root is None:
                    raise ValueError("A local recording directory is required")
                from pawception.live_webcam import WebcamSession
                session = WebcamSession(settings, now=now, root=root, api_key=api_key)
            elif provider == "muse":
                session = MuseSession(settings, now=now, root=root, source=source, api_key=api_key)
            else:
                session = MockSession(settings, now=now)
            now = self.clock()
            session.started = now
            if self.session is not None and getattr(self.session, "alert_images", None):
                self.session.alert_images.close()
            self.session = session
            if provider == "muse" or webcam:
                Thread(target=self.drive, args=(session,), daemon=True).start()
            return session.snapshot(now)

    def drive(self, session):
        # Real capture/processing continues independently of browser polling.
        while True:
            with self.lock:
                if self.session is not session:
                    return
                session.snapshot(self.clock())
                if session.status not in {"live", "finishing"}:
                    return
            time.sleep(0.1)

    def current(self):
        with self.lock:
            return self.session.snapshot(self.clock()) if self.session else None

    def camera_chunk(self, session_id, data, **metadata):
        with self.lock:
            if not self.session or self.session.id != session_id or not hasattr(self.session, "receive"):
                raise ValueError("Camera session unavailable")
            self.session.receive(data, now=self.clock(), **metadata)
            return self.session.snapshot(self.clock())

    def camera_media(self, session_id, **interval):
        with self.lock:
            session = self.session
            if not session or session.id != session_id or not hasattr(session, "review_media"):
                raise ValueError("Camera recording unavailable")
        return session.review_media(**interval)

    def update(self, session_id, action=None, value=None):
        with self.lock:
            if not self.session or self.session.id != session_id:
                raise ValueError("Session unavailable. Return to Setup to start a new session.")
            now = self.clock()
            if action == "event":
                self.session.add_event(value, now=now)
            elif action == "stop":
                self.session.stop(now)
            elif action == "viewed":
                self.session.view_alert(value.get("alert_id"), now)
            elif action == "safety":
                if set(value) == {"enabled"}:
                    self.session.set_safety(value.get("enabled"), now=now)
                elif set(value) == {"safety_prompt", "safety_exclusions"}:
                    self.session.configure_safety(value, now=now)
                else:
                    raise ValueError("Provide a Safety watch toggle or a prompt and exclusions")
            return self.session.snapshot(now)
