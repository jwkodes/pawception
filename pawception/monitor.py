"""Task definitions, structured-output schema, and semantic validation for Muse."""

import json
import math
import re


def validate_task(task):
    if not isinstance(task, dict) or any(not isinstance(task.get(k), str) or not task[k].strip() for k in ("name", "subject", "scene_context")):
        raise ValueError("Monitoring task needs name, subject, and scene_context strings")
    rules = task.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= 20:
        raise ValueError("Monitoring task needs between 1 and 20 rules")
    ids = []
    for rule in rules:
        if not isinstance(rule, dict) or any(not isinstance(rule.get(k), str) or not rule[k].strip() for k in ("id", "owner_request", "event_definition")):
            raise ValueError("Each rule needs id, owner_request, and event_definition strings")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rule["id"]):
            raise ValueError("Rule IDs must be short lowercase identifiers")
        ids.append(rule["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Monitoring rule IDs must be unique")
    return task


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_event_task(task):
    if not isinstance(task, dict) or not isinstance(task.get("name"), str) or not task["name"].strip():
        raise ValueError("Event task needs a name")
    events = task.get("events")
    minimum = 0 if task.get("safety_watch") is True else 1
    if not isinstance(events, list) or not minimum <= len(events) <= 20:
        raise ValueError("Event task needs events or enabled Safety watch")
    seen = set()
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event["id"]):
            raise ValueError("Event IDs must be short lowercase identifiers")
        if task.get("safety_watch") and event["id"] == "hazards":
            raise ValueError("hazards is reserved for Safety watch")
        if event["id"] in seen or not isinstance(event.get("description"), str) or not event["description"].strip():
            raise ValueError("Each event needs a unique ID and nonempty description")
        seen.add(event["id"])
    return task


def validate_interval(interval, duration):
    """Clamp sub-microsecond boundary rounding; reject genuinely invalid times."""
    if not isinstance(interval, dict) or set(interval) != {"start", "end"}:
        raise ValueError("Invalid event interval fields")
    start, end = interval["start"], interval["end"]
    if (any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end))
            or not -1e-6 <= start < end <= duration + 1e-6):
        raise ValueError("Event intervals must lie within the clip")
    start, end = max(0, start), min(duration, end)
    if start >= end:
        raise ValueError("Event intervals must lie within the clip")
    # Raw API text stays unchanged; structured observations use exact bounds.
    interval.update(start=start, end=end)


def validate_binary_observations(value, task, duration):
    from pawception.evidence_prompts import FIELDS, validate_prompts
    expected = {event["id"] for event in task["events"]}
    if task.get("safety_watch"):
        expected.add("hazards")
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Expected each configured event exactly once")
    if task.get("safety_watch"):
        from pawception.safety import validate_hazards
        validate_hazards(value["hazards"], duration, task.get("known_hazards", []),
                         require_visual=task.get("visual_evidence", False), require_boxes=task.get('bounding_boxes', False))
    for event in task["events"]:
        detection = value[event["id"]]
        fields = {"detected", "intervals", "evidence"}
        if task.get("visual_evidence"):
            fields.update(FIELDS)
        if task.get('bounding_boxes'):
            from pawception.evidence_boxes import FIELDS as BOX_FIELDS
            fields.update(BOX_FIELDS)
        if not isinstance(detection, dict) or set(detection) != fields:
            raise ValueError("Detection must contain detected, intervals, and evidence")
        if type(detection["detected"]) is not bool or not isinstance(detection["intervals"], list):
            raise ValueError("Detected must be a boolean and intervals an array")
        if detection["detected"] != bool(detection["intervals"]):
            raise ValueError("Yes requires event intervals; no requires an empty interval array")
        if not isinstance(detection["evidence"], str) or not detection["evidence"].strip():
            raise ValueError("Detection needs a brief explanation")
        if task.get("visual_evidence"):
            validate_prompts(detection)
        for interval in detection["intervals"]:
            validate_interval(interval, duration)
        if task.get('bounding_boxes'):
            from pawception.evidence_boxes import validate_boxes
            validate_boxes(detection, duration, audio_only=task.get('input_kind') == 'audio' or event.get('input_mode') == 'audio')
    return value


def monitor_format(task, output_format="temporal"):
    if output_format == "binary":
        from pawception.evidence_prompts import schema_fields
        fields = {
            "detected": {"type": "boolean"},
            "intervals": {"type": "array", "items": obj({"start": {"type": "number"}, "end": {"type": "number"}})},
            "evidence": {"type": "string"},
        }
        if task.get("visual_evidence"):
            fields.update(schema_fields())
        if task.get('bounding_boxes'):
            from pawception.evidence_boxes import schema_fields as box_schema
            fields.update(box_schema())
        detection = obj(fields)
        properties = {event["id"]: detection for event in task["events"]}
        if task.get("safety_watch"):
            from pawception.safety import hazard_schema
            properties["hazards"] = hazard_schema(task.get("visual_evidence", False), task.get('bounding_boxes', False))
        return {"type": "json_schema", "name": "pet_event_detections", "strict": True, "schema": obj(properties)}
    if output_format == "simple":
        assessment = obj({
            "assessment": {"type": "string", "enum": ["observed", "not_observed", "unknown"]},
            "intervals": {"type": "array", "items": obj({"start": {"type": "number"}, "end": {"type": "number"}})},
            "evidence": {"type": "string"},
            "uncertainty": {"type": ["string", "null"]},
        })
        return {"type": "json_schema", "name": "pet_monitor_simple", "strict": True,
                "schema": obj({rule["id"]: assessment for rule in task["rules"]})}
    interval = {"start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}}
    strings = {"type": "array", "items": {"type": "string"}}
    assessment = obj({
        "rule_id": {"type": "string", "enum": [r["id"] for r in task["rules"]]},
        "outcome": {"type": "string", "enum": ["observed", "not_observed", "unknown"]},
        "visibility": {"type": "string", "enum": ["adequate", "limited", "unassessable"]},
        "events": {"type": "array", "items": obj({**interval, "evidence": {"type": "string"}, "uncertainties": strings})},
        "coverage_gaps": {"type": "array", "items": obj({**interval, "reason": {"type": "string"}})},
        "explanation": {"type": "string"},
    })
    return {"type": "json_schema", "name": "pet_monitor_observations", "strict": True,
            "schema": obj({"assessments": {"type": "array", "items": assessment}})}


def render_monitor_prompt(template, task, duration):
    if not any(marker in template for marker in ("{{TASK_JSON}}", "{{DURATION_SECONDS}}", "{{EVENT_DEFINITIONS}}", "{{SCENE_CONTEXT}}")):
        return template
    definitions = "\n".join(f"{index}. {rule['id']}: {rule['event_definition']}" for index, rule in enumerate(task["rules"], 1))
    return (template.replace("{{TASK_JSON}}", json.dumps(task, indent=2))
            .replace("{{DURATION_SECONDS}}", f"{duration:g}")
            .replace("{{EVENT_DEFINITIONS}}", definitions)
            .replace("{{SCENE_CONTEXT}}", task["scene_context"]))


def validate_observations(text, task, duration, output_format="temporal"):
    """A valid JSON shape doesn't establish valid time, coverage, or rule semantics."""
    value = json.loads(text)
    if output_format == "binary":
        return validate_binary_observations(value, task, duration)
    if output_format == "simple":
        expected = {r["id"] for r in task["rules"]}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("Expected each configured rule exactly once")
        for assessment in value.values():
            if not isinstance(assessment, dict) or set(assessment) != {"assessment", "intervals", "evidence", "uncertainty"}:
                raise ValueError("Assessment has missing or unexpected fields")
            outcome, intervals = assessment["assessment"], assessment["intervals"]
            if outcome not in ("observed", "not_observed", "unknown") or not isinstance(intervals, list):
                raise ValueError("Invalid assessment or intervals")
            if (outcome == "observed") != bool(intervals):
                raise ValueError("Only observed assessments may have intervals, and must have at least one")
            if not isinstance(assessment["evidence"], str) or not assessment["evidence"].strip():
                raise ValueError("Assessments need an evidence explanation")
            if assessment["uncertainty"] is not None and not isinstance(assessment["uncertainty"], str):
                raise ValueError("Uncertainty must be a string or null")
            for interval in intervals:
                if not isinstance(interval, dict) or set(interval) != {"start", "end"}:
                    raise ValueError("Invalid interval fields")
                start, end = interval["start"], interval["end"]
                if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or not 0 <= start < end <= duration:
                    raise ValueError("Event intervals must lie within the source clip")
        return value
    if not isinstance(value, dict) or set(value) != {"assessments"} or not isinstance(value["assessments"], list):
        raise ValueError("Expected an assessments array")
    expected = {r["id"] for r in task["rules"]}
    seen = set()
    for assessment in value["assessments"]:
        if not isinstance(assessment, dict) or set(assessment) != {"rule_id", "outcome", "visibility", "events", "coverage_gaps", "explanation"}:
            raise ValueError("Assessment has missing or unexpected fields")
        rule_id = assessment["rule_id"]
        if not isinstance(rule_id, str) or rule_id not in expected or rule_id in seen:
            raise ValueError("Expected each configured rule exactly once")
        seen.add(rule_id)
        outcome, visibility = assessment["outcome"], assessment["visibility"]
        if outcome not in ("observed", "not_observed", "unknown") or visibility not in ("adequate", "limited", "unassessable"):
            raise ValueError("Invalid outcome or visibility")
        if not isinstance(assessment["explanation"], str) or not assessment["explanation"].strip():
            raise ValueError("Assessments need an evidence explanation")
        events, gaps = assessment["events"], assessment["coverage_gaps"]
        if not isinstance(events, list) or not isinstance(gaps, list):
            raise ValueError("Events and coverage gaps must be arrays")
        if (outcome == "observed") != bool(events):
            raise ValueError("Only observed assessments may have events, and must have at least one")
        if outcome == "not_observed" and (visibility != "adequate" or gaps):
            raise ValueError("Negative observations require adequate, uninterrupted coverage")
        if outcome == "unknown" and (visibility == "adequate" or not gaps):
            raise ValueError("Unknown observations must identify unassessable intervals")
        if visibility == "unassessable" and events:
            raise ValueError("A wholly unassessable event cannot be observed")
        for entries, field in ((events, "evidence"), (gaps, "reason")):
            for entry in entries:
                keys = {"start_seconds", "end_seconds", field} | ({"uncertainties"} if field == "evidence" else set())
                if not isinstance(entry, dict) or set(entry) != keys:
                    raise ValueError("Invalid interval fields")
                start, end = entry["start_seconds"], entry["end_seconds"]
                if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or not 0 <= start < end <= duration:
                    raise ValueError("Event and gap intervals must lie within the source clip")
                if not isinstance(entry[field], str) or not entry[field].strip():
                    raise ValueError("Intervals need supporting evidence or a gap reason")
                if field == "evidence" and (not isinstance(entry["uncertainties"], list) or any(not isinstance(u, str) for u in entry["uncertainties"])):
                    raise ValueError("Event uncertainties must be strings")
    if seen != expected:
        raise ValueError("Expected each configured rule exactly once")
    return value
