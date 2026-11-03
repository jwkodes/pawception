"""Yes/no detections, nearby event episodes, and duration-based alerts.

This module has no model or file I/O. Overlapping positive event intervals are
unioned before measuring duration. Bridged gaps never count as detected time.
"""

from __future__ import annotations

import hashlib
import json
import math

from pawception.monitor import validate_event_task, validate_binary_observations

TERMINAL = {"completed", "invalid", "error", "skipped", "interrupted"}
ADAPTER_VERSION = 4
EPSILON = 1e-6


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_config(value):
    required = {"version", "task", "alert_after_seconds"}
    if not isinstance(value, dict) or value.get("version") != 3 or not required <= set(value) or set(value)-required-{"merge_gap_seconds"}:
        raise ValueError("Pipeline config needs version 3, an event task, and alert_after_seconds")
    validate_event_task(value["task"])
    thresholds = value["alert_after_seconds"]
    if not isinstance(thresholds, dict) or set(thresholds) != {event["id"] for event in value["task"]["events"]}:
        raise ValueError("Supply an alert duration for every event")
    if any(isinstance(seconds, bool) or not isinstance(seconds, (int, float))
           or not math.isfinite(seconds) or seconds <= 0 for seconds in thresholds.values()):
        raise ValueError("Alert durations must be finite positive seconds")
    gap = value.get("merge_gap_seconds", 0)
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or not 0 <= gap <= 30:
        raise ValueError("Event gap tolerance must be between 0 and 30 seconds")
    return value


def perception_prompt(task, duration):
    definitions = "\n".join(f"- {event['id']}: {event['description']}" for event in task["events"])
    if task.get("input_kind") == "audio":
        box_instruction = ('evidence_timestamp, pet_bbox, and object_bbox must also be null.\n'
                           if task.get('bounding_boxes') else '')
        return f"""Listen to the attached audio and check these monitoring events:
{definitions}

For each event return detected (true or false), evidence (a short explanation),
and intervals (start/end seconds when the event is audible).
Return true when the audible evidence supports the described event, otherwise
false with an empty intervals array. Use the full recording and context.
Times are relative to this recording: 0 <= start < end <= {duration:.9f} seconds.
Loudness is relative to the recording, not a calibrated room decibel level.
Both pet_segmentation_prompt and object_segmentation_prompt must be null because
this input has no image. {box_instruction}Report events only; the backend decides when to alert.
Return only JSON matching the supplied schema."""
    media = {"audio": "audio recording", "both": "video with its soundtrack"}.get(task.get("input_kind"), "video")
    sequence = "recording" if task.get("input_kind") == "audio" else "video sequence"
    prompt = f"""You are a home and pet-monitoring assistant analyzing {media}.
Determine whether each of these events happens in the attached {media}:
{definitions}

Use the full {sequence} and context to make a yes/no decision for each event.
Return detected=true if the event happens. If there is not enough evidence to
support yes, return detected=false. Identify pets and objects from the footage.

For each event return:
- detected: true or false.
- intervals: matching start/end times in seconds; use an empty array for false.
- evidence: a brief explanation of the decision.

The {media} runs from 0 to {duration:.9f} seconds. Use playback seconds relative to
this attachment, not any date/time printed in the footage. Every interval must
satisfy 0 <= start < end <= {duration:.9f}.

Report detections only. The backend decides whether to trigger an alert.
Return only JSON matching the supplied schema."""
    if task.get("safety_watch"):
        from pawception.safety import safety_prompt
        prompt += safety_prompt(task.get("known_hazards", []), task.get("safety_prompt"), task.get("safety_exclusions", ""))
    if task.get("visual_evidence"):
        from pawception.evidence_prompts import PROMPT
        prompt += PROMPT
    if task.get('bounding_boxes'):
        from pawception.evidence_boxes import PROMPT as BOX_PROMPT
        prompt += BOX_PROMPT
    if task.get("input_kind"):
        prompt += "\nInput supplied: " + task["input_kind"] + ".\n"
        prompt += "For each event, use only its assigned inputs. Audio events require audible evidence; never infer sound from mouth movements. Visual events must ignore sound. For Visual + Audio events evaluate the complete description, preserving AND, OR, and timing relationships. Loudness is relative to the recording, not a calibrated room sound level.\n"
        for event in task["events"]:
            prompt += f"{event['id']} inputs: {event.get('input_mode', 'visual')}\n"
        if task.get("input_kind") == "audio":
            prompt += "This is audio-only. Return null for both segmentation prompts; no image is available.\n"
        if task.get("safety_watch"):
            prompt += "Safety Watch uses visual evidence only; ignore sound when reporting hazards.\n"
    return prompt


def normalize_window(window, record, task):
    """Align valid yes/no responses to source time. API errors are not 'no'."""
    output = record.get("output")
    error = record.get("error") or record.get("status", "No result")
    valid = record.get("status") == "completed"
    if valid:
        try:
            validate_binary_observations(output, task, window["input_duration_seconds"])
        except (ValueError, TypeError, KeyError) as exc:
            valid = False
            error = f"Invalid response: {exc}"
    detections = []
    for event in task["events"]:
        answer = output[event["id"]] if valid else None
        intervals = []
        if answer:
            for interval in answer["intervals"]:
                intervals.append({
                    "start": window["start_seconds"] + interval["start"],
                    "end": min(window["end_seconds"], window["start_seconds"] + interval["end"]),
                })
        detections.append({
            "window_id": window["id"],
            "event_id": event["id"],
            "detected": answer["detected"] if answer else None,
            "intervals": intervals,
            "evidence": answer["evidence"] if answer else "",
            "status": "completed" if valid else "invalid" if record.get("status") == "completed" else record.get("status", "pending"),
            "error": None if valid else error,
        })
    return detections


def merge_detections(detections, gap_seconds=0):
    """Join nearby positives into episodes, keeping their exact detected parts."""
    intervals = []
    for detection in detections:
        if detection["detected"] is True:
            for interval in detection["intervals"]:
                intervals.append({**interval, "event_id": detection["event_id"], "window_ids": [detection["window_id"]]})
    merged = []
    for interval in sorted(intervals, key=lambda item: (item["event_id"], item["start"], item["end"])):
        if merged and merged[-1]["event_id"] == interval["event_id"] and interval["start"] <= merged[-1]["end"] + gap_seconds + EPSILON:
            merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
            merged[-1]["window_ids"] = sorted(set(merged[-1]["window_ids"] + interval["window_ids"]))
            last = merged[-1]["intervals"][-1]
            if interval["start"] <= last["end"] + EPSILON:
                last["end"] = max(last["end"], interval["end"])
            else:
                merged[-1]["intervals"].append({"start": interval["start"], "end": interval["end"]})
        else:
            merged.append({**interval, "intervals": [{"start": interval["start"], "end": interval["end"]}]})
    for episode in merged:
        episode["detected_seconds"] = sum(part["end"]-part["start"] for part in episode["intervals"])
    return merged


def threshold_time(episode, seconds):
    """Locate the threshold in positive footage, excluding gaps and overlap."""
    for part in episode["intervals"]:
        length = part["end"]-part["start"]
        if seconds <= length + EPSILON:
            return min(part["end"], part["start"]+seconds)
        seconds -= length
    raise ValueError("Episode has not reached the alert duration")


def build_pipeline_replay(config, windows, records):
    validate_config(config)
    if not windows:
        raise ValueError("No pipeline windows")
    task = config["task"]
    gap = config.get("merge_gap_seconds", 0)
    alerts = []
    snapshots = []
    processed_detections = []
    available_after = 0

    for window in windows:
        record = records.get(window["id"], {})
        if record.get("status") not in TERMINAL:
            break  # An out-of-order result cannot skip a pending window.
        answers = normalize_window(window, record, task)
        processed_detections.extend(answers)
        available_after = max(available_after, record.get("finished_after_run_seconds", 0))
        intervals = merge_detections(processed_detections, gap)
        for interval in intervals:
            event_id = interval["event_id"]
            threshold = config["alert_after_seconds"][event_id]
            duration = interval["detected_seconds"]
            if duration + EPSILON < threshold:
                continue
            # Extending a previously alerted interval does not alert again.
            # Negative/error windows cannot erase positive overlapping evidence.
            if any(alert["event_id"] == event_id
                   and interval["start"] <= alert["at"] <= interval["end"] + EPSILON for alert in alerts):
                continue
            supporting = [answer for answer in processed_detections
                          if answer["event_id"] == event_id and answer["detected"] is True
                          and any(part["start"] < interval["end"] and part["end"] > interval["start"]
                                  for part in answer["intervals"])]
            at = threshold_time(interval, threshold)
            alerts.append({
                "id": fingerprint([task, event_id, interval["start"], threshold])[:24],
                "event_id": event_id,
                "at": at,
                "threshold_seconds": threshold,
                "detected_duration_seconds": duration,
                "confirmed_through_seconds": window["end_seconds"],
                "available_after_run_seconds": available_after,
                "reason": f"Event detected for at least {threshold:g} seconds in one episode",
                "supporting_window_ids": interval["window_ids"],
                "supporting_detections": supporting,
                "evidence_span": {"start": interval["start"], "end": interval["end"]},
            })
        snapshots.append({
            "time": window["end_seconds"],
            "window_id": window["id"],
            "longest_detected_seconds": {
                event["id"]: max((i["detected_seconds"] for i in intervals if i["event_id"] == event["id"]), default=0)
                for event in task["events"]
            },
            "alerts": list(alerts),
        })

    public_windows = []
    for window in windows:
        record = records.get(window["id"], {})
        answers = normalize_window(window, record, task) if record.get("status") in TERMINAL else []
        status = record.get("status", "pending")
        if any(answer["status"] == "invalid" for answer in answers):
            status = "invalid"
        public_windows.append({**window, "status": status, "record": record, "detections": answers})

    detections = [answer for window in public_windows for answer in window["detections"]]
    return {
        "adapter_version": ADAPTER_VERSION,
        "task": task,
        "alert_after_seconds": config["alert_after_seconds"],
        "merge_gap_seconds": gap,
        "start_seconds": windows[0]["start_seconds"],
        "end_seconds": windows[-1]["end_seconds"],
        "windows": public_windows,
        "detections": detections,
        "events": merge_detections(detections, gap),
        "snapshots": snapshots,
        "alerts": alerts,
        "summary": {
            "windows": len(windows),
            "terminal_windows": sum(w["status"] in TERMINAL for w in public_windows),
            "finalized_windows": len(snapshots),
            "finalized_through": snapshots[-1]["time"] if snapshots else windows[0]["start_seconds"],
            "alerts": len(alerts),
        },
    }
