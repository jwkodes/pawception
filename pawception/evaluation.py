"""Deterministic behavior and temporal scoring against versioned annotations.

This module never calls a model. Labels, predictions, and annotation review
status remain separate; unknown predictions are abstentions, not negatives.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

LABELS = {"observed", "not_observed", "unassessable", "unlabeled"}
OUTCOMES = {"observed", "not_observed", "unknown"}
RUN_ID = re.compile(r"\d{8}T\d{6}_\d{6}Z")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def interval_error(interval, duration):
    if not isinstance(interval, dict) or set(interval) != {"start", "end"}:
        return "Interval must contain start and end"
    start, end = interval["start"], interval["end"]
    if not finite(start) or not finite(end) or not 0 <= start < end <= duration:
        return "Interval requires 0 <= start < end <= clip duration"
    return None


def validate_dataset(dataset):
    if not isinstance(dataset, dict) or dataset.get("schema_version") != 1:
        raise ValueError("Expected ground-truth schema_version 1")
    for key in ("dataset_id", "version"):
        if not isinstance(dataset.get(key), str) or not dataset[key].strip():
            raise ValueError(f"Dataset needs {key}")
    definitions = dataset.get("behaviors")
    if not isinstance(definitions, dict) or not definitions:
        raise ValueError("Dataset needs behavior definitions")
    for key, definition in definitions.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or not isinstance(definition, str) or not definition.strip():
            raise ValueError("Invalid behavior definition")
    if not isinstance(dataset.get("clips"), list) or not dataset["clips"]:
        raise ValueError("Dataset needs clips")
    ids, hashes = set(), {}
    for clip in dataset["clips"]:
        if "enabled" in clip and type(clip["enabled"]) is not bool:
            raise ValueError("Clip enabled flag must be boolean")
        if not isinstance(clip.get("id"), str) or not clip["id"] or clip["id"] in ids:
            raise ValueError("Clip IDs must be unique")
        ids.add(clip["id"])
        duration = clip.get("duration_seconds")
        if not finite(duration) or duration <= 0:
            raise ValueError("Clip duration must be positive")
        media = clip.get("media")
        if not isinstance(media, list) or not media:
            raise ValueError("Clip needs hashed media variants")
        variants = set()
        for version in media:
            path = PurePosixPath(version.get("file", ""))
            sha = version.get("sha256", "")
            if path.is_absolute() or ".." in path.parts or str(path) in ("", "."):
                raise ValueError("Media paths must be relative to data/")
            if not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise ValueError("Media needs a SHA-256")
            if sha in hashes and hashes[sha] != clip["id"]:
                raise ValueError("Duplicate footage cannot be counted as a different clip")
            hashes[sha] = clip["id"]
            if not isinstance(version.get("variant"), str) or version["variant"] in variants:
                raise ValueError("Media variant names must be unique within each clip")
            variants.add(version["variant"])
        annotations = clip.get("annotations")
        if not isinstance(annotations, dict) or set(annotations) != set(definitions):
            raise ValueError("Explicitly label or leave unlabeled every configured behavior")
        for behavior, annotation in annotations.items():
            label = annotation.get("label")
            if label not in LABELS or annotation.get("review_status") not in ("confirmed", "proposed"):
                raise ValueError("Invalid label or review status")
            if not isinstance(annotation.get("reviewed_by"), str) or not annotation["reviewed_by"].strip():
                raise ValueError("Labels need annotator provenance")
            if not isinstance(annotation.get("basis"), str) or not annotation["basis"].strip():
                raise ValueError("Labels need an annotation basis")
            timing = annotation.get("timing_status")
            intervals = annotation.get("intervals")
            if label == "observed":
                if timing == "unlabeled":
                    if intervals is not None:
                        raise ValueError("Unlabeled timing must use null intervals")
                elif timing in ("proposed", "confirmed"):
                    if not isinstance(intervals, list) or not intervals or any(interval_error(i, duration) for i in intervals):
                        raise ValueError("Invalid ground-truth event intervals")
                    ordered = sorted(intervals, key=lambda i: i["start"])
                    if any(a["end"] > b["start"] for a, b in zip(ordered, ordered[1:])):
                        raise ValueError("Ground-truth episodes must not overlap")
                else:
                    raise ValueError("Observed labels need an explicit timing status")
            elif timing != "not_applicable" or intervals != []:
                raise ValueError("Non-positive labels must not invent event intervals")
    scoring = dataset.get("scoring", {})
    for key in ("interval_iou_threshold", "minimum_accuracy"):
        value = scoring.get(key, 0.5 if key == "interval_iou_threshold" else 1.0)
        if not finite(value) or not 0 <= value <= 1:
            raise ValueError(f"Invalid scoring threshold: {key}")
    tolerance = scoring.get("boundary_tolerance_seconds", 0.5)
    if not finite(tolerance) or tolerance < 0:
        raise ValueError("Boundary tolerance must be non-negative")
    return dataset


def load_dataset(path, *, root=None, verify_media=False):
    dataset = validate_dataset(json.loads(path.read_text()))
    if verify_media:
        if root is None:
            raise ValueError("Media verification needs a project root")
        data = (root / "data").resolve()
        for clip in dataset["clips"]:
            for version in clip["media"]:
                file = root / "data" / version["file"]
                if file.is_symlink() or not file.resolve().is_relative_to(data) or not file.is_file() or digest(file) != version["sha256"]:
                    raise ValueError(f"Missing or mismatched media: {version['file']}")
    return dataset


def union(intervals):
    result = []
    for interval in sorted(intervals, key=lambda i: (i["start"], i["end"])):
        start, end = interval["start"], interval["end"]
        if result and start <= result[-1]["end"]:
            result[-1]["end"] = max(result[-1]["end"], end)
        else:
            result.append({"start": start, "end": end})
    return result


def temporal_iou(expected, predicted):
    expected, predicted = union(expected), union(predicted)
    intersection = sum(max(0, min(a["end"], b["end"]) - max(a["start"], b["start"])) for a in expected for b in predicted)
    total = sum(i["end"] - i["start"] for i in union(expected + predicted))
    return intersection / total if total else None


def match_events(expected, predicted, threshold):
    """Maximum-cardinality one-to-one matching; a prediction cannot count twice."""
    return match_event_groups([[part] for part in expected], [[part] for part in predicted], threshold)


def match_event_groups(expected, predicted, threshold):
    """Match episodes using their actual detected pieces, without filling gaps."""
    edges = {i: sorted((j for j, p in enumerate(predicted) if temporal_iou(e, p) >= threshold), key=lambda j: -temporal_iou(e, predicted[j])) for i, e in enumerate(expected)}
    assignments = {}
    def augment(i, seen):
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in assignments or augment(assignments[j], seen):
                assignments[j] = i
                return True
        return False
    for i in range(len(expected)):
        augment(i, set())
    pairs = [(i, j) for j, i in assignments.items()]
    return {"true_positives": len(pairs), "false_positives": len(predicted) - len(pairs), "false_negatives": len(expected) - len(pairs),
            "matched_ious": [temporal_iou(expected[i], predicted[j]) for i, j in pairs],
            "onset_errors_seconds": [abs(min(p['start'] for p in expected[i])-min(p['start'] for p in predicted[j])) for i, j in pairs],
            "offset_errors_seconds": [abs(max(p['end'] for p in expected[i])-max(p['end'] for p in predicted[j])) for i, j in pairs]}


def requested_behaviors(run, analysis):
    fmt = analysis.get("response_format") or run.get("response_format") or {}
    properties = fmt.get("schema", {}).get("properties", {})
    if "assessments" in properties:
        return properties["assessments"].get("items", {}).get("properties", {}).get("rule_id", {}).get("enum", [])
    return list(properties)


def parse_predictions(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value
    value = json.loads(text, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Prediction root must be an object")
    if set(value) == {"assessments"}:
        if not isinstance(value["assessments"], list):
            raise ValueError("Expected assessments array")
        converted = {}
        for item in value["assessments"]:
            key = item["rule_id"]
            if key in converted:
                raise ValueError("Duplicate rule assessment")
            converted[key] = {"assessment": item["outcome"], "intervals": [{"start": e["start_seconds"], "end": e["end_seconds"]} for e in item["events"]]}
        return converted
    return value


def score_case(annotation, prediction, duration, *, include_proposed=False, iou_threshold=0.5, unavailable=False, parse_error=None):
    expected = annotation["label"]
    result = {"expected": expected, "review_status": annotation["review_status"], "timing_status": annotation["timing_status"],
              "expected_intervals": annotation["intervals"], "scored": False, "prediction": None, "interval_errors": [], "timing": None, "provisional": annotation["review_status"] == "proposed"}
    if unavailable:
        result["prediction"] = "unavailable"
    elif parse_error:
        result.update(prediction="invalid", prediction_error=parse_error)
    elif prediction is None:
        result["prediction"] = "missing"
    elif not isinstance(prediction, dict) or not isinstance(prediction.get("assessment"), str) or prediction["assessment"] not in OUTCOMES:
        result.update(prediction="invalid", prediction_error="Invalid assessment")
    else:
        result["prediction"] = prediction["assessment"]
    valid_intervals = []
    if result["prediction"] in OUTCOMES:
        intervals = prediction.get("intervals")
        if not isinstance(intervals, list):
            result["interval_errors"].append("Missing intervals array")
        else:
            if (result["prediction"] == "observed") != bool(intervals):
                result["interval_errors"].append("Only observed assessments may contain intervals, and need at least one")
            for interval in intervals:
                error = interval_error(interval, duration)
                if error:
                    result["interval_errors"].append(error)
                else:
                    valid_intervals.append(interval)
        result["predicted_intervals"] = intervals
    if expected in ("unassessable", "unlabeled"):
        result["skip_reason"] = expected + "_ground_truth"
    elif annotation["review_status"] != "confirmed" and not include_proposed:
        result["skip_reason"] = "proposed_ground_truth"
    elif unavailable:
        result["skip_reason"] = "infrastructure_failure"
    else:
        result["scored"] = True
        result["correct"] = result["prediction"] == expected
        can_time = expected == "not_observed" or annotation["timing_status"] == "confirmed" or (include_proposed and annotation["timing_status"] == "proposed")
        if can_time:
            truth = annotation["intervals"] or []
            metrics = match_events(truth, valid_intervals, iou_threshold)
            result["timing"] = {**metrics, "iou": temporal_iou(truth, valid_intervals), "provisional": annotation["timing_status"] == "proposed", "valid": not result["interval_errors"]}
        elif expected == "observed":
            result["timing_skip_reason"] = "Event boundaries are not confirmed"
    return result


def ratio(a, b):
    return a / b if b else None


def summarize(cases):
    scored = [c for c in cases if c["scored"]]
    positive = [c for c in scored if c["expected"] == "observed"]
    negative = [c for c in scored if c["expected"] == "not_observed"]
    tp = sum(c["prediction"] == "observed" for c in positive)
    fp = sum(c["prediction"] == "observed" for c in negative)
    correct = sum(c["correct"] for c in scored)
    answered = sum(c["prediction"] in ("observed", "not_observed") for c in scored)
    timed = [c["timing"] for c in cases if c["timing"] is not None]
    ious = [c["iou"] for c in timed if c["iou"] is not None]
    event_tp = sum(c["true_positives"] for c in timed)
    event_fp = sum(c["false_positives"] for c in timed)
    event_fn = sum(c["false_negatives"] for c in timed)
    return {"scored_cases": len(scored), "positive_cases": len(positive), "negative_cases": len(negative),
            "true_positives": tp, "false_positives": fp, "positive_misses_including_abstentions": len(positive) - tp,
            "true_negatives": sum(c["prediction"] == "not_observed" for c in negative),
            "accuracy": ratio(correct, len(scored)), "precision": ratio(tp, tp + fp), "positive_recall": ratio(tp, len(positive)),
            "decision_coverage": ratio(answered, len(scored)), "selective_accuracy": ratio(correct, answered),
            "abstentions": sum(c["prediction"] == "unknown" for c in scored),
            "missing_predictions": sum(c["prediction"] == "missing" for c in cases),
            "unrequested_confirmed_cases": sum(c["prediction"] == "not_requested" for c in cases),
            "invalid_predictions": sum(c["prediction"] == "invalid" for c in cases),
            "invalid_interval_cases": sum(bool(c["interval_errors"]) for c in cases),
            "infrastructure_failure_cases": sum(c["prediction"] == "unavailable" for c in cases),
            "skipped_cases": dict(Counter(c.get("skip_reason") for c in cases if not c["scored"])),
            "independent_clips": len({c["clip_id"] for c in scored if c.get("clip_id")}),
            "timing": {"scored_cases": len(timed), "mean_iou": ratio(sum(ious), len(ious)), "event_true_positives": event_tp, "event_false_positives": event_fp, "event_false_negatives": event_fn,
                       "event_precision": ratio(event_tp, event_tp + event_fp), "event_recall": ratio(event_tp, event_tp + event_fn),
                       "mean_onset_error_seconds": mean([v for c in timed for v in c["onset_errors_seconds"]]),
                       "mean_offset_error_seconds": mean([v for c in timed for v in c["offset_errors_seconds"]])}}


def mean(values):
    return sum(values) / len(values) if values else None


def evaluate_runs(dataset, run_directories, *, include_proposed=False):
    validate_dataset(dataset)
    by_hash = {m["sha256"]: (clip, m) for clip in dataset["clips"] for m in clip["media"]}
    by_name = {Path(m["file"]).name: clip for clip in dataset["clips"] for m in clip["media"]}
    cases, calls, skipped_runs, unmatched, excluded = [], [], [], [], []
    threshold = dataset.get("scoring", {}).get("interval_iou_threshold", 0.5)
    for directory in run_directories:
        run = json.loads((directory / "run.json").read_text())
        if "monitor" not in run.get("modes", []):
            skipped_runs.append({"run_id": directory.name, "reason": "Unstructured describe/hazard prose is not automatically graded"})
            continue
        found_paths = Counter()
        for result_file in sorted(directory.glob("*/result.json")):
            result = json.loads(result_file.read_text())
            found_paths[result.get("video")] += 1
            match = by_hash.get(result.get("sha256"))
            if match is None:
                unmatched.append({"run_id": directory.name, "file": result.get("video"), "sha256": result.get("sha256")})
                continue
            clip, media = match
            if not clip.get("enabled", True):
                excluded.append({"run_id": directory.name, "clip_id": clip["id"], "reason": clip.get("exclusion_reason", "Excluded from annotation set")})
                continue
            analysis = result.get("analyses", {}).get("monitor", {})
            requested = requested_behaviors(run, analysis)
            if not requested:
                skipped_runs.append({"run_id": directory.name, "reason": "Requested behavior schema is missing"})
                continue
            config = {"model": analysis.get("requested_model") or run["model"], "prompt_template": run.get("prompts", {}).get("monitor"),
                      "response_format": analysis.get("response_format") or run.get("response_format"), "max_output_tokens": run.get("max_output_tokens"),
                      "request_options": analysis.get("request_options", {})}
            config_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
            call_id = f"{directory.name}/{result_file.parent.name}/monitor"
            unavailable = not analysis or analysis.get("status") in ("error", "running", "incomplete") or analysis.get("response_status") not in (None, "completed")
            predictions, parse_error = {}, None
            if not unavailable:
                try:
                    output = result_file.parent / "monitor.txt"
                    text = output.read_text() if output.exists() else json.dumps(analysis.get("structured_output"))
                    predictions = parse_predictions(text)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    parse_error = f"Cannot parse structured response ({type(error).__name__})"
            calls.append({"call_id": call_id, "run_id": directory.name, "clip_id": clip["id"], "variant": media["variant"], "config_id": config_id, "model": config["model"],
                          "config": config, "video_sha256": result["sha256"], "upload_seconds": result.get("upload_seconds"), "inference_seconds": analysis.get("inference_seconds"), "usage": analysis.get("usage"), "unavailable": unavailable})
            confirmed = [b for b, a in clip["annotations"].items() if a["review_status"] == "confirmed" and a["label"] in ("observed", "not_observed")]
            for behavior in dict.fromkeys(requested + confirmed):
                if behavior not in clip["annotations"]:
                    continue
                value = score_case(clip["annotations"][behavior], predictions.get(behavior), clip["duration_seconds"], include_proposed=include_proposed, iou_threshold=threshold, unavailable=unavailable, parse_error=parse_error)
                if behavior not in requested:
                    value.update(scored=False, skip_reason="confirmed_behavior_not_requested", prediction="not_requested", timing=None)
                    value.pop("correct", None)
                cases.append({"call_id": call_id, "run_id": directory.name, "clip_id": clip["id"], "behavior": behavior, "variant": media["variant"], "config_id": config_id, "model": config["model"], **value})
        # A failed or interrupted run cannot improve its score by omitting a file.
        missing_paths = Counter(run.get("planned_videos", [])) - found_paths
        for path in missing_paths:
            clip = by_name.get(Path(path).name)
            if clip is None:
                unmatched.append({"run_id": directory.name, "file": path, "reason": "Planned media has no annotated identity"})
                continue
            if not clip.get("enabled", True):
                excluded.append({"run_id": directory.name, "clip_id": clip["id"], "reason": clip.get("exclusion_reason", "Excluded from annotation set")})
                continue
            for missing_index in range(missing_paths[path]):
                for behavior in requested_behaviors(run, {}):
                    if behavior in clip["annotations"]:
                        value = score_case(clip["annotations"][behavior], None, clip["duration_seconds"], include_proposed=include_proposed, unavailable=True)
                        cases.append({"call_id": f"{directory.name}/missing/{clip['id']}/{missing_index}", "run_id": directory.name, "clip_id": clip["id"], "behavior": behavior, "variant": "unavailable", "config_id": "unavailable", "model": run["model"], **value})
    groups = []
    grouped = defaultdict(list)
    for case in cases:
        grouped[(case["model"], case["config_id"], case["variant"])].append(case)
    for (model, config_id, variant), values in sorted(grouped.items()):
        metrics = summarize(values)
        groups.append({"model": model, "config_id": config_id, "variant": variant, "metrics": metrics,
                       "by_behavior": {b: summarize([c for c in values if c["behavior"] == b]) for b in sorted({c["behavior"] for c in values})}})
    consistency = []
    repeated = defaultdict(list)
    for case in cases:
        if case["prediction"] in OUTCOMES:
            repeated[(case["model"], case["config_id"], case["variant"], case["clip_id"], case["behavior"])].append(case["prediction"])
    for (model, config, variant, clip, behavior), outcomes in sorted(repeated.items()):
        if len(outcomes) > 1:
            counts = Counter(outcomes)
            consistency.append({"model": model, "config_id": config, "variant": variant, "clip_id": clip, "behavior": behavior, "repeats": len(outcomes), "counts": dict(counts), "agreement": max(counts.values()) / len(outcomes), "changed": len(counts) > 1})
    summary = summarize(cases)
    minimum = dataset.get("scoring", {}).get("minimum_accuracy", 1.0)
    failures = []
    if summary["accuracy"] is None:
        failures.append("No confirmed, assessable ground-truth cases were scored")
    if any(g["metrics"]["accuracy"] is not None and g["metrics"]["accuracy"] < minimum for g in groups):
        failures.append(f"At least one configuration is below minimum accuracy {minimum:g}")
    for key in ("invalid_predictions", "missing_predictions", "unrequested_confirmed_cases", "invalid_interval_cases", "infrastructure_failure_cases"):
        if summary[key]:
            failures.append(f"{summary[key]} {key}")
    tolerance = dataset.get("scoring", {}).get("boundary_tolerance_seconds", 0.5)
    for case in cases:
        timing = case["timing"]
        if timing is not None:
            timing["passed"] = not (case["interval_errors"] or timing["false_positives"] or timing["false_negatives"] or
                                    any(v > tolerance + 1e-9 for v in timing["onset_errors_seconds"] + timing["offset_errors_seconds"]))
    bad_timing = [c for c in cases if c["timing"] is not None and not c["timing"]["passed"]]
    summary["timing"]["failed_cases"] = len(bad_timing)
    if bad_timing:
        failures.append(f"{len(bad_timing)} timing cases failed event matching or {tolerance:g}s boundary tolerance")
    if unmatched:
        failures.append("Some submitted media hashes could not be matched to ground truth")
    return {"schema_version": 1, "dataset_id": dataset["dataset_id"], "dataset_version": dataset["version"],
            "dataset_sha256": hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest(), "include_proposed": include_proposed,
            "gate": {"status": "fail" if failures else "pass", "reasons": failures, "minimum_accuracy": minimum},
            "summary": summary, "groups": groups, "repeat_consistency": consistency, "cases": cases, "calls": calls, "skipped_runs": skipped_runs, "unmatched_media": unmatched, "excluded_clips": excluded,
            "metric_notes": ["Unknown is an abstention; it lowers accuracy and positive recall on assessable truth without becoming a negative observation.",
                             "Precision excludes abstentions; decision coverage and positive recall must be read alongside it.",
                             "Invalid time intervals do not erase an otherwise readable behavior assessment, but fail the regression gate.",
                             "Unassessable/unlabeled truth is excluded; proposed truth and timing are excluded unless explicitly enabled.",
                             "Temporal IoU uses interval unions; event counts use maximum-cardinality one-to-one matching at the configured IoU threshold.",
                             "Independent clip counts exclude repeat trials and alternate resolutions as new footage."]}
