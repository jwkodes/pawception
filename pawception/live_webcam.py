"""Append browser recording chunks; analyze fixed windows from captured bytes.

Recording continues while inference is busy. Only the inference queue drops old
work. Recording chunks must arrive in order: dropping one would corrupt WebM.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import math
from pathlib import Path
from threading import Lock
import time

from pawception.live_mock import MockSession, number
from pawception.event_inputs import needs_audio
from pawception.live_muse import MuseSession, analyze_prepared_window
from pawception.files import checksum, write_json
from pawception.video import ffmpeg, probe_video

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_RECORDING_BYTES = 1024 * 1024 * 1024
MAX_CAPTURE_SECONDS = 7200
CAPTURE_MARGIN = 0.25  # dataavailable can fire slightly after the last encoded frame.


def encode_clip(source, target, start, end, *, include_audio=False):
    """Read an already received prefix, never future camera footage."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".partial.mp4")
    ffmpeg("-y", "-ss", start, "-i", source, "-t", end-start,
           "-map", "0:v:0", *(["-map", "0:a:0?", "-af", "aresample=async=1:first_pts=0", "-c:a", "aac"] if include_audio else ["-an"]), "-vf", "scale=w='min(854,iw)':h='min(480,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setpts=PTS-STARTPTS",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-fps_mode", "passthrough",
           "-map_metadata", "-1", "-movflags", "+faststart", temporary)
    metadata, timeline, _ = probe_video(temporary)
    frame_tolerance = min(0.1,1/metadata["fps"]+0.02)
    gaps = [float(right[0]-left[0]) for left, right in zip(timeline, timeline[1:])]
    if metadata["duration_seconds"] < end-start-frame_tolerance or max(gaps, default=0)>1:
        temporary.unlink(missing_ok=True)
        raise ValueError("Camera recording does not cover the requested interval")
    temporary.replace(target)
    return metadata


def analyze_webcam_window(source, directory, job, events, model, api_key, started, provider, cancelled=None):
    from pawception.live_muse import check_cancelled
    check_cancelled(cancelled)
    target = directory / "windows" / job["id"] / "clip.mp4"
    if needs_audio(events):
        metadata = encode_clip(source, target, job["start_seconds"], job["end_seconds"], include_audio=True)
    else:
        metadata = encode_clip(source, target, job["start_seconds"], job["end_seconds"])
    # Frame/muxer rounding must not create millisecond gaps between windows.
    # encode_clip rejects missing frames/time before using the planned bounds.
    job.update(file=str(target.relative_to(directory)), sha256=checksum(target),
               encoded_duration_seconds=metadata["duration_seconds"],
               input_duration_seconds=job["end_seconds"]-job["start_seconds"])
    if provider == "muse":
        return analyze_prepared_window(directory, job, events, model, api_key, started, cancelled)
    from pawception.pipeline_runner import mock_response
    fixture = {"events": {event["id"]: [{"start": event["active_from"]+offset,
                                          "end": event["active_from"]+offset+length}
                                         for offset, length in ((5, 23), (45, 24))] for event in events}}
    record = {"status": "completed", "output": mock_response(job, {"events": events}, fixture),
              "finished_after_run_seconds": max(time.monotonic()-started, job.get("finishes_at", 0))}
    if job.get("safety", {}).get("enabled"):
        from pawception.safety import mock_hazards
        record["output"]["hazards"] = mock_hazards(job)
    write_json(target.parent / "observation.json", record)
    return job, record


class WebcamSession(MuseSession):
    def __init__(self, settings, *, now, root, api_key=None, executor=None):
        self.plans, self.next_plan, self.future = [], 0, None
        self.next_capture_start = 0
        self.received_seconds, self.received_bytes, self.sequence = 0, 0, 0
        self.last_chunk_at = now
        self.capture_audio = settings.get("capture_audio", False)
        if type(self.capture_audio) is not bool:
            raise ValueError("Microphone capture must be on or off")
        self.mime = None
        self.api_key, self.directory = api_key, None
        self.media_lock = Lock()
        MockSession.__init__(self, {**settings, "duration": MAX_CAPTURE_SECONDS,
                                   "source_name": "Webcam"}, now=now)
        self.provider = settings.get("provider", "mock")
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="pawception-camera")
        self.directory = Path(root) / "data/live_sessions" / self.id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.source = self.directory / "capture.webm"
        self.init_alert_images(settings)
        self.persist()

    def receive(self, data, *, sequence, end, mime, now):
        if self.status != "live":
            raise ValueError("This camera session is no longer capturing")
        if type(sequence) is not int or sequence != self.sequence:
            raise ValueError("Camera recording chunks must arrive in order")
        number(end, "Capture timestamp", self.received_seconds+0.001, MAX_CAPTURE_SECONDS)
        if end > now-self.started+2:
            raise ValueError("Camera timestamps cannot precede capture")
        if mime not in {"video/webm", "video/mp4"} or self.mime not in {None, mime}:
            raise ValueError("Use one supported recording format throughout the session")
        if not data or len(data)>MAX_CHUNK_BYTES or self.received_bytes+len(data)>MAX_RECORDING_BYTES:
            raise ValueError("Camera recording limit reached; stop and start a new session")
        # Format is fixed by the first chunk; subsequent chunks may lack headers.
        if self.sequence == 0:
            if mime == "video/webm" and not data.startswith(b"\x1aE\xdf\xa3"):
                raise ValueError("Expected a WebM camera recording")
            if mime == "video/mp4" and data[4:8] != b"ftyp":
                raise ValueError("Expected an MP4 camera recording")
            self.source = self.directory / ("capture.webm" if mime == "video/webm" else "capture.mp4")
        with self.source.open("ab") as stream:
            stream.write(data)
        self.mime = mime
        self.received_seconds, self.last_chunk_at = end, now
        self.received_bytes += len(data)
        self.sequence += 1
        while self.next_capture_start+self.window <= max(0,end-CAPTURE_MARGIN):
            start = self.next_capture_start
            self.plans.append({"id": f"{len(self.plans):04d}", "index": len(self.plans),
                               "start_seconds": start, "end_seconds": start+self.window,
                               "input_duration_seconds": self.window})
            self.next_capture_start += self.window-self.overlap
        self.advance(now)

    def _dispatch(self, at):
        job = self._take_job(at)
        if job is None:
            return
        job.update(status="running", started_at=at)
        if self.provider == "mock":
            job["finishes_at"] = at+self.delay
        self.records[job["id"]] = {"status": "running"}
        self.active = job
        self.future = self.executor.submit(analyze_webcam_window, self.source, self.directory,
                                          deepcopy(job), deepcopy(self.events), self.model,
                                          self.api_key, self.started, self.provider, self.cancelled)

    def advance(self, now):
        if self.status not in {"live", "finishing"}:
            return
        if now-self.last_chunk_at > 15 or now-self.started >= MAX_CAPTURE_SECONDS:
            self.stop(now)
            return
        super().advance(now)

    def result_ready(self, elapsed):
        return super().result_ready(elapsed) and (self.provider != "mock" or elapsed>=self.active["started_at"]+self.delay)

    def snapshot(self, now):
        state = super().snapshot(now)
        captured = max(0,self.received_seconds-CAPTURE_MARGIN)
        state.update(source_type="webcam", captured_seconds=captured,
                     duration=max(60, math.ceil(self.received_seconds/60)*60),
                     collecting_start=self.next_capture_start if self.status == "live" else None,
                     lag_seconds=max(0, captured-state["analyzed_through"]),
                     recording_bytes=self.received_bytes, capture_audio=self.capture_audio)
        if self.provider == "mock":
            state["mock_delay_seconds"] = self.delay
        return state

    def persist(self):
        if self.directory is not None:
            write_json(self.directory / "session.json", {
                "id": self.id, "provider": self.provider, "source_type": "webcam", "status": self.status,
                "source": str(self.source), "received_seconds": self.received_seconds,
                "model": self.model, "window_seconds": self.window, "overlap_seconds": self.overlap,
                "video_reasoning_effort": self.video_reasoning_effort,
                "audio_model": self.audio_model, "audio_reasoning_effort": self.audio_reasoning_effort,
                "capture_audio": self.capture_audio,
                "merge_gap_seconds": self.merge_gap,
                "sam_snapshots": getattr(self, "sam_snapshots", False),
                "safety": self.safety.context(), "safety_results": self.safety.snapshot(self.merge_gap),
                "events": self.events, "windows": self.windows, "records": self.records,
                "viewed_alert_ids": sorted(self.viewed_alerts)})

    def review_media(self, *, start, end, frame=False):
        captured = max(0,self.received_seconds-CAPTURE_MARGIN)
        number(start, "Review start", 0, max(0, captured-0.05))
        number(end, "Review end", start+0.05, min(captured, start+120))
        key = sha256(f"{start:.3f}:{end:.3f}:{frame}".encode()).hexdigest()[:20]
        target = self.directory / "review" / (key + (".jpg" if frame else ".mp4"))
        # HTTP review work never holds the monitor's scheduling lock.
        with self.media_lock:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                if frame:
                    ffmpeg("-y", "-ss", start, "-i", self.source, "-frames:v", 1,
                           "-vf", "scale=288:-2", target)
                else:
                    encode_clip(self.source, target, start, end, include_audio=self.capture_audio)
        return target
