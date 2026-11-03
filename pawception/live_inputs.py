"""Group events by model, choose media once, and retain successful checks."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import time

from pawception.event_inputs import input_settings
from pawception.media_inputs import has_audio, media_window
from pawception.monitor import monitor_format
from pawception.reasoning import EFFORTS
from pawception.muse import safe_error
from pawception.files import write_json


def plan_checks(events, visual_model, audio_model, visual_effort, audio_effort, safety, audio_present):
    groups, unavailable = {}, {}

    def group(model, effort):
        check = groups.setdefault(model, {"model": model, "effort": effort, "events": [], "safety": False})
        levels = (*EFFORTS, "max")
        check["effort"] = max((check["effort"], effort), key=levels.index)
        return check

    for event in events:
        mode = input_settings(event)["resolved_mode"]
        if mode != "visual" and not audio_present:
            unavailable[event["id"]] = "Audio unavailable: this window has no usable audio track."
            continue
        check = group(visual_model, visual_effort) if mode == "visual" else group(audio_model, audio_effort)
        check["events"].append({**event, "resolved_mode": mode})
    if safety.get("enabled"):
        group(visual_model, visual_effort)["safety"] = True
    for check in groups.values():
        modes = {event["resolved_mode"] for event in check["events"]}
        visual = check["safety"] or bool(modes & {"visual", "both"})
        audio = bool(modes & {"audio", "both"})
        check["media_kind"] = "both" if visual and audio else "visual" if visual else "audio"
    return list(groups.values()), unavailable


def analyze_inputs(directory, job, events, model, api_key, started, cancelled=None):
    from pawception.pipeline_runner import analyze_window
    from pawception.live_muse import check_cancelled
    check_cancelled(cancelled)
    safety = job.get("safety", {})
    needs_sound = any(input_settings(event)["resolved_mode"] != "visual" for event in events)
    audio_present = has_audio(directory / job["file"]) if needs_sound else False
    checks, unavailable = plan_checks(events, model, job.get("audio_model", "muse-spark-1.2-contributor"),
        job.get("reasoning_effort", "medium"), job.get("audio_reasoning_effort", "medium"), safety, audio_present)
    folder = directory / "windows" / job["id"]
    write_json(folder / "task.json", {"events": events, "safety": safety, "checks": checks})

    updates = Queue()

    def run(check, index):
        check_cancelled(cancelled)
        window = media_window(directory, job, check["media_kind"], audio_present)
        check_cancelled(cancelled)
        # Keep every request's exact prompt, output, timing, and cleanup receipt.
        window["id"] = f"{job['id']}/checks/{index}"
        request_folder = directory / "windows" / window["id"]
        request_folder.mkdir(parents=True, exist_ok=True)
        task = {"name": "Live monitoring", "visual_evidence": True, 'bounding_boxes':True, "input_kind": check["media_kind"],
                "events": [{"id": event["id"], "description": event["description"],
                            "input_mode": event["resolved_mode"]} for event in check["events"]]}
        if check["safety"]:
            task.update(safety_watch=True, known_hazards=safety.get("known_hazards", []),
                        safety_prompt=safety.get("safety_prompt"), safety_exclusions=safety.get("safety_exclusions", ""))
        request = {"model": check["model"], "max_output_tokens": 8192,
                   "media_kind": "audio" if check["media_kind"] == "audio" else "video",
                   "options": {"stream": True, "temperature": 1, "top_p": 1,
                               "reasoning": {"effort": check["effort"]}, "text": {"format": monitor_format(task, "binary")}}}
        return analyze_window(window, {"provider": "muse", "windows": [window],
            "config": {"task": task}, "request": request}, directory, api_key, None, (started, 0), cancelled=cancelled,
            on_result_ready=lambda record: updates.put(('ready', index, record)))

    output, results, errors, event_times = {}, [], dict(unavailable), {}
    safety_time = None

    def publish():
        # Publish validated check results separately from the request logs. The
        # session thread alone updates episodes, alerts, and public state.
        summaries = []
        for check in checks:
            done = next((r for r in results if r['model'] == check['model']), None)
            summaries.append({key: (done or check).get(key) for key in ('model', 'media_kind', 'error')} |
                {'status':done['status'] if done else 'running',
                 'event_ids':[event['id'] for event in check['events']]})
        record = {'status':'running', 'output':output, 'checks':summaries, 'event_errors':errors,
                  'checks_finished':len(results), 'checks_total':len(checks),
                  'event_finished_after_run_seconds':event_times,
                  'safety_finished_after_run_seconds':safety_time,
                  'finished_after_run_seconds':time.monotonic()-started}
        write_json(folder/'live.progress.json', {'window':job, 'record':record})

    write_json(folder / "result.json", {"status": "analyzing", "checks": checks})
    publish()
    if checks:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="muse-input") as executor:
            futures = [executor.submit(run, check, index) for index, check in enumerate(checks)]
            for index, future in enumerate(futures):
                future.add_done_callback(lambda done, index=index: updates.put(('done', index, done)))
            pending = len(futures)
            while pending:
                kind, index, value = updates.get()
                check = checks[index]
                if kind == 'done':
                    pending -= 1
                    try:
                        record = value.result()
                    except Exception as error:
                        record = {"status": "error", "error": safe_error(error)}
                    # A cleanup/publication failure cannot revoke validated evidence.
                    previous = next((r for r in results if r['model'] == check['model']), None)
                    if previous and previous['status'] == 'completed' and record['status'] != 'completed':
                        record = {**previous, 'cleanup_error':record.get('error')}
                else:
                    record = value
                results[:] = [r for r in results if r['model'] != check['model']]
                results.append({"model": check["model"], "media_kind": check["media_kind"],
                                "event_ids": [event["id"] for event in check["events"]], **record})
                if record["status"] == "completed":
                    output.update(record["output"])
                    finished = record.get('finished_after_run_seconds', time.monotonic()-started)
                    event_times.update({event['id']:finished for event in check['events']})
                    if check['safety']:
                        safety_time = finished
                else:
                    for event in check["events"]:
                        errors[event["id"]] = "Input check failed; no observation was added."
                publish()
                write_json(folder / "result.json", {"status": "analyzing", "checks": results})
    succeeded = sum(record["status"] == "completed" for record in results)
    failed = bool(errors) or any(record["status"] != "completed" for record in results)
    status = "partial" if succeeded and failed else "completed" if succeeded else "error"
    error = next((record.get("error") for record in results if record.get("error")), None)
    if error is None and not succeeded:
        error = {"type": "InputsUnavailable", "detail": "Audio unavailable; no usable inputs for these events."}
    record = {"status": status, "output": output, "checks": results, "event_errors": errors,
              'event_finished_after_run_seconds':event_times, 'safety_finished_after_run_seconds':safety_time,
              'checks_finished':len(results), 'checks_total':len(checks),
              "audio_available": audio_present, "error": error,
              "source_available_seconds": job["end_seconds"], "finished_after_run_seconds": time.monotonic()-started}
    return record
