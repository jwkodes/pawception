"""Playback-paced Muse sessions: fixed windows, one worker, two pending slots."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from subprocess import SubprocessError
from threading import Event

from pawception.live_mock import MockSession, MAX_PENDING
from pawception.video_windows import frame_windows
from pawception.event_inputs import needs_audio
from pawception.files import write_json
from pawception.video import probe_video


def analyze_live_window(source, directory, job, events, model, api_key, started, cancelled=None):
    # Reuse neutral filenames, validated JSON, saved artifacts and upload cleanup.
    from pawception.pipeline_runner import prepare_windows
    check_cancelled(cancelled)
    if needs_audio(events):
        prepare_windows(source, [job], directory, include_audio=True)
    else:
        prepare_windows(source, [job], directory)
    return analyze_prepared_window(directory, job, events, model, api_key, started, cancelled)


def check_cancelled(cancelled):
    if cancelled is not None and cancelled.is_set():
        from concurrent.futures import CancelledError
        raise CancelledError("Monitoring stopped")


def clip_to_activation(record, events, job):
    # A queued window can precede a newly added event's activation.
    for event in events:
        answer = (record.get('output') or {}).get(event['id'])
        if answer is None:
            continue
        active_from = max(0, event['active_from'] - job['start_seconds'])
        answer['intervals'] = [{'start':max(part['start'], active_from), 'end':part['end']}
                               for part in answer['intervals'] if part['end'] > active_from]
        answer['detected'] = bool(answer['intervals'])
        timestamp = answer.get('evidence_timestamp')
        if not answer['detected'] or timestamp is not None and not any(
                part['start'] <= timestamp < part['end'] for part in answer['intervals']):
            fields = ('evidence_timestamp','pet_bbox','object_bbox')
            if not answer['detected']:
                fields += ('pet_segmentation_prompt','object_segmentation_prompt')
            for field in fields:
                if field in answer:
                    answer[field] = None


def analyze_prepared_window(directory, job, events, model, api_key, started, cancelled=None):
    from pawception.monitor import monitor_format
    from pawception.pipeline_runner import analyze_window
    check_cancelled(cancelled)
    if needs_audio(events):
        from pawception.live_inputs import analyze_inputs
        record = analyze_inputs(directory, job, events, model, api_key, started, cancelled)
    else:
        task = {"name": "Live monitoring", "visual_evidence": True, 'bounding_boxes':True, "events": [
            {"id": event["id"], "description": event["description"]} for event in events]}
        safety = job.get("safety", {})
        if safety.get("enabled"):
            task.update(safety_watch=True, known_hazards=safety.get("known_hazards", []),
                        safety_prompt=safety.get("safety_prompt"), safety_exclusions=safety.get("safety_exclusions", ""))
        write_json(directory / "windows" / job["id"] / "task.json", {"events": events, "safety": safety})
        run = {"provider": "muse", "config": {"task": task}, "windows": [job], "request": {
            "model": model, "max_output_tokens": 8192,
            "options": {"stream": True, "temperature": 1, "top_p": 1,
                        "reasoning": {"effort": job.get("reasoning_effort", "medium")}, "text": {"format": monitor_format(task, "binary")}}}}
        def publish(record):
            write_json(directory / 'windows' / job['id'] / 'live.progress.json', {
                'window':job, 'record':{**record, 'status':'running', 'checks_finished':1, 'checks_total':1,
                    'checks':[{'model':model, 'media_kind':'visual', 'status':'completed',
                               'event_ids':[event['id'] for event in events]}],
                    'event_finished_after_run_seconds':{event['id']:record['finished_after_run_seconds'] for event in events},
                    'safety_finished_after_run_seconds':record['finished_after_run_seconds'] if safety.get('enabled') else None}})

        record = analyze_window(job, run, directory, api_key, None, (started, 0), cancelled=cancelled,
                                on_result_ready=publish)
    record["source_available_seconds"] = job["end_seconds"]
    if record.get("status") in {"completed", "partial"}:
        clip_to_activation(record, events, job)
    write_json(directory / "windows" / job["id"] / "live.observation.json", record)
    return job, record


class MuseSession(MockSession):
    def __init__(self, settings, *, now, root, source, api_key, executor=None):
        self.source = Path(source)
        try:
            metadata, timeline, _ = probe_video(self.source)
        except (OSError, RuntimeError, ValueError, SubprocessError):
            raise ValueError("The local backend could not decode this video. Choose a supported video file.") from None
        from pawception.live_mock import number
        number(metadata["duration_seconds"], "Video duration", 0.1, 7200)
        self.plans = frame_windows(timeline, window_seconds=settings.get("window_seconds", 60),
                                   overlap_seconds=settings.get("overlap_seconds", 0))
        self.next_plan = 0
        self.future = None
        self.api_key = api_key
        self.directory = None
        super().__init__({**settings, "duration": metadata["duration_seconds"], "mock_delay_seconds": 5}, now=now)
        self.provider = "muse"
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="pawception-muse")
        self.directory = Path(root) / "data/live_sessions" / self.id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.init_alert_images(settings)
        self.persist()

    def init_alert_images(self, settings):
        self.cancelled = Event()
        self.safety_observed_windows = set()
        # Ignore old browser settings: live monitoring no longer starts SAM.
        self.sam_snapshots = False
        self.alert_images = None

    def persist(self):
        if self.directory is None:
            return
        write_json(self.directory / "session.json", {
            "id": self.id, "provider": "muse", "status": self.status, "source": str(self.source),
            "model": self.model, "window_seconds": self.window, "overlap_seconds": self.overlap,
            "video_reasoning_effort": self.video_reasoning_effort,
            "audio_model": self.audio_model, "audio_reasoning_effort": self.audio_reasoning_effort,
            "merge_gap_seconds": self.merge_gap,
            "sam_snapshots": getattr(self, "sam_snapshots", False),
            "safety": self.safety.context(), "safety_results": self.safety.snapshot(self.merge_gap),
            "events": self.events, "windows": self.windows, "records": self.records,
            "viewed_alert_ids": sorted(self.viewed_alerts),
        })

    def snapshot(self, now):
        state = super().snapshot(now)
        state.pop("mock_delay_seconds", None)
        state["sam_snapshots"] = getattr(self, "sam_snapshots", False)
        if getattr(self, "alert_images", None):
            self.alert_images.update(state, self.windows, self.records)
        if state["active_window"] and self.directory:
            from pawception.experiments import read_json
            progress = read_json(self.directory / "windows" / state["active_window"]["id"] / "result.json") or {}
            state["active_window"]["phase"] = progress.get("status", "preparing")
        return state

    def _dispatch(self, at):
        job = self._take_job(at)
        if job is None:
            return
        job.update(status="running", started_at=at)
        self.records[job["id"]] = {"status": "running"}
        self.active = job
        self.future = self.executor.submit(analyze_live_window, self.source, self.directory,
                                          deepcopy(job), deepcopy(self.events), self.model, self.api_key, self.started, self.cancelled)

    def apply_record(self, prepared, record):
        clip_to_activation(record, self.events, prepared)
        self.active.update(prepared, status=record['status'], event_errors=record.get('event_errors', {}),
                           checks=[{key:check.get(key) for key in ('model','media_kind','status','event_ids','error')}
                                   for check in record.get('checks', [])],
                           checks_finished=record.get('checks_finished', 0), checks_total=record.get('checks_total', 0),
                           safety_checked='hazards' in (record.get('output') or {}))
        self.records[self.active['id']] = record
        self.event_replays.clear()
        if self.active['safety_checked'] and self.active['id'] not in self.safety_observed_windows:
            safety_record = {**record, 'status':'completed', 'finished_after_run_seconds':
                record.get('safety_finished_after_run_seconds') or record.get('finished_after_run_seconds', 0)}
            self.safety.observe(self.active, safety_record, self.merge_gap)
            self.safety_observed_windows.add(self.active['id'])

    def read_progress(self):
        if self.active is None or self.directory is None:
            return False
        from pawception.experiments import read_json
        progress = read_json(self.directory/'windows'/self.active['id']/'live.progress.json')
        if not progress or progress['record']['checks_finished'] <= self.active.get('checks_finished', -1):
            return False
        self.apply_record(progress['window'], progress['record'])
        return True

    def advance(self, now):
        if self.status in {"stopped", "completed", "error"}:
            return
        elapsed = max(self.time, now - self.started)
        changed = self.read_progress()
        result, finish = None, float("inf")
        if self.result_ready(elapsed):
            try:
                result = self.future.result()
            except Exception as error:
                previous = self.records.get(self.active['id'], {})
                result = (self.active, {**previous, "status": "partial" if previous.get('output') else "error",
                                        "error": {"type": type(error).__name__},
                                        "finished_after_run_seconds": elapsed})
            finish = result[1].get("finished_after_run_seconds", elapsed)
        while True:
            plan = self.plans[self.next_plan] if self.next_plan < len(self.plans) else None
            arrival = plan["end_seconds"] if plan else float("inf")
            if min(arrival, finish) > elapsed:
                break
            if finish <= arrival:
                prepared, record = result
                self.apply_record(prepared, record)
                self.active = self.future = None
                finish = float("inf")
                error = record.get("error")
                if isinstance(error, dict) and error.get("status_code") in {401, 402, 403, 429}:
                    self.status = "error"
                    for job in self.queue:
                        job["status"] = "cancelled"
                    self.queue.clear()
                    self.next_start = None
                    changed = True
                    break
                self._dispatch(elapsed)
            else:
                job = {**plan, "status": "pending"}
                self.windows.append(job)
                self.queue.append(job)
                self.next_plan += 1
                if len(self.queue) > MAX_PENDING:
                    self.queue.pop(0)["status"] = "skipped"
                self._dispatch(elapsed)
            changed = True
        self.time = elapsed
        if self.status != "error":
            self.next_start = self.plans[self.next_plan]["start_seconds"] if self.next_plan < len(self.plans) else None
            if elapsed >= self.duration:
                self.status = "finishing" if self.active or self.queue else "completed"
        if self.status in {"completed", "error"} and hasattr(self, "executor"):
            self.executor.shutdown(wait=False)
        if changed:
            self.persist()

    def result_ready(self, elapsed):
        return self.future is not None and self.future.done()

    def stop(self, now):
        self.cancelled.set()
        self.read_progress()
        if getattr(self, "alert_images", None):
            self.alert_images.close()
        self.time = max(self.time, now - self.started)
        for job in self.queue:
            job["status"] = "cancelled"
        if self.active:
            record = self.records[self.active['id']]
            # Already published detections survive Stop; unfinished checks add
            # no negative evidence and cannot erase an earlier notification.
            self.active['status'] = 'partial' if record.get('output') else 'cancelled'
            if record.get('output'):
                record['status'] = 'partial'
                for check in self.active.get('checks', []):
                    if check['status'] == 'running':
                        check['status'] = 'cancelled'
                        for event_id in check['event_ids']:
                            record.setdefault('event_errors', {})[event_id] = 'Monitoring stopped before this input check finished.'
                self.active['event_errors'] = record.get('event_errors', {})
        self.queue.clear()
        self.active = None
        self.next_start = None
        self.status = "stopped"
        # An in-flight call may still finish and clean up its upload. Never retry
        # it or submit additional calls after Stop.
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.persist()

    def view_alert(self, alert_id, now):
        super().view_alert(alert_id, now)
        self.persist()
