"""Hazard observations and immediate, deduplicated Safety watch alerts."""

from copy import deepcopy
import re

from pawception.pipeline import EPSILON, fingerprint, merge_detections

KINDS = ["electrical", "entanglement", "ingestion", "fall", "heat", "fire", "smoke", "flooding", "aggression", "injury", "other"]

DEFAULT_HAZARD_PROMPT = """Examine the entire scene for visible danger to pets, people, or the home, including hazards the owner has not explicitly named.
No pet or person needs to be visible: report supported household dangers even in an empty room. Examples include uncontrolled fire, smoke spreading through a room, electrical sparks, or leaking water spreading across a floor. Also watch for dangerous pet or person interactions such as chewing an electrical cable, becoming tangled in a cord, ingesting a dangerous object, or approaching an active heat source.
Report concrete dangerous interactions or circumstances with visible evidence, not hypothetical risks from an object's mere presence. Distinguish smoke from ordinary steam, and uncontrolled fire from a normally contained flame such as a candle or fireplace. Do not flag ordinary cooking, contained flames, or water in a sink merely because they are present; report visible evidence of a dangerous situation.
Ordinary furniture chewing, normal play, and calm behavior are not automatically hazards. Do not diagnose medical issues or assume an unidentified substance or plant is toxic.
Report the same incident once: when smoke accompanies a visible fire, report the fire and describe the smoke in its evidence instead of duplicating it as a smoke hazard.
Explain what is visible and why it may be dangerous; do not claim harm or ingestion without evidence. Use kind=other for supported hazards outside the listed categories."""


def safety_settings(prompt=None, exclusions=""):
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip() or len(prompt)>6000):
        raise ValueError("Hazard prompt must contain 1–6000 characters")
    if not isinstance(exclusions, str) or len(exclusions)>2000:
        raise ValueError("Safety exclusions must contain at most 2000 characters")
    return {"safety_prompt": prompt.strip() if prompt is not None else None,
            "safety_exclusions": exclusions.strip()}


def hazard_schema(visual_evidence=False, bounding_boxes=False):
    from pawception.monitor import obj
    fields = {
        "id": {"type": ["string", "null"]},
        "kind": {"type": "string", "enum": KINDS},
        "subject": {"type": "string"}, "target": {"type": "string"},
        "title": {"type": "string"}, "evidence": {"type": "string"},
        "intervals": {"type": "array", "items": obj({"start": {"type": "number"}, "end": {"type": "number"}})},
    }
    if visual_evidence:
        from pawception.evidence_prompts import schema_fields
        fields.update(schema_fields())
    if bounding_boxes:
        from pawception.evidence_boxes import schema_fields
        fields.update(schema_fields())
    return {"type": "array", "items": obj(fields)}


def safety_prompt(known, prompt=None, exclusions=""):
    import json
    settings = safety_settings(prompt, exclusions)
    instructions = settings["safety_prompt"] or DEFAULT_HAZARD_PROMPT
    if settings["safety_exclusions"]:
        instructions += "\n\nOwner exclusions (take precedence over the hazard guidance above; apply only to Safety watch, not custom events):\n" + settings["safety_exclusions"]
    return "\nSafety watch is enabled for pets, people, and the home. A pet does not need to be visible for a household hazard.\n" + instructions + """

Return hazards as a list in the hazards field; use [] when none is supported.

Each hazard needs kind, subject (the affected pet, person, or household area,
<=120 characters), target (the object
or source of risk, <=160 characters), a specific title (<=120 characters),
evidence (<=1000 characters), and clip-relative intervals. Distinguish
different pets, people, household areas and objects with concise, stable visual descriptions. Report at
most 20 hazards. Each description must stay under 1000 characters.
For a hazard matching one below, reuse its id, kind, subject and target EXACTLY,
even when the title or explanation changes. Otherwise set id=null. Do not merge
different subjects, locations, objects or kinds of danger. A continuing hazard is the same
identity across windows; a changed title does not create a new hazard.
Known hazards (identity references, not proof they occur in this clip):
""" + json.dumps(known, ensure_ascii=False)


def validate_hazards(value, duration, known=(), *, require_visual=False, require_boxes=False):
    from pawception.monitor import validate_interval
    from pawception.evidence_prompts import FIELDS, validate_prompts
    from pawception.evidence_boxes import FIELDS as BOX_FIELDS, validate_boxes
    if not isinstance(value, list) or len(value)>20:
        raise ValueError("Expected a list of at most 20 hazards")
    known = {item["id"]: item for item in known}
    fields = {"id", "kind", "subject", "target", "title", "evidence", "intervals"}
    for hazard in value:
        allowed = [fields | prompts | boxes
                   for prompts in ([set(FIELDS)] if require_visual else [set(),set(FIELDS)])
                   for boxes in ([set(BOX_FIELDS)] if require_boxes else [set(),set(BOX_FIELDS)])]
        if not isinstance(hazard, dict) or set(hazard) not in allowed or hazard["kind"] not in KINDS:
            raise ValueError("Invalid hazard fields or kind")
        if set(FIELDS) <= set(hazard):
            validate_prompts(hazard)
        for field, maximum in (("subject", 120), ("target", 160), ("title", 120), ("evidence", 1000)):
            if not isinstance(hazard[field], str) or not hazard[field].strip() or len(hazard[field])>maximum:
                raise ValueError(f"Invalid hazard {field}")
        reference = hazard["id"]
        if reference is not None:
            if not isinstance(reference, str) or reference not in known:
                raise ValueError("Hazard refers to an unknown identity")
            if any(hazard[field] != known[reference][field] for field in ("kind", "subject", "target")):
                raise ValueError("A known hazard must preserve its identity")
        if not isinstance(hazard["intervals"], list) or not hazard["intervals"]:
            raise ValueError("A hazard needs observed intervals")
        for interval in hazard["intervals"]:
            validate_interval(interval, duration)
        if set(BOX_FIELDS) <= set(hazard):
            validate_boxes(hazard, duration)
    return value


def identity(hazard):
    # Titles and evidence deliberately do not participate in identity.
    words = lambda value: " ".join(word for word in re.findall(r"\w+", value.casefold()) if word not in {"a", "an", "the"})
    return "safety_" + fingerprint([hazard["kind"], words(hazard["subject"]), words(hazard["target"])])[:12]


class SafetyWatch:
    def __init__(self, enabled=False, prompt=None, exclusions=""):
        if type(enabled) is not bool:
            raise ValueError("Safety watch must be on or off")
        self.enabled, self.generation, self.active_from = enabled, 0, 0
        self.settings = safety_settings(prompt, exclusions)
        self.hazards, self.detections, self.alerts = {}, [], []

    def set_enabled(self, enabled, at):
        if type(enabled) is not bool:
            raise ValueError("Safety watch must be on or off")
        if self.enabled != enabled:
            self.enabled = enabled
            self.generation += 1
            self.active_from = at

    def context(self):
        known = list(self.hazards.values())[-20:]
        return {"enabled": self.enabled, "generation": self.generation, "active_from": self.active_from, **self.settings,
                "known_hazards": [{key: hazard[key] for key in ("id", "kind", "subject", "target")} for hazard in known]}

    def configure(self, prompt, exclusions):
        # Jobs snapshot their instructions at dispatch. Let older jobs finish so
        # changing guidance does not create an artificial gap in an episode.
        self.settings = safety_settings(prompt, exclusions)

    def observe(self, window, record, gap):
        context = window.get("safety", {})
        if (record.get("status") not in {"completed", "partial"} or not self.enabled or not context.get("enabled")
                or context.get("generation") != self.generation):
            return
        if "hazards" not in (record.get("output") or {}):
            return
        hazards = record["output"]["hazards"]
        validate_hazards(hazards, window["input_duration_seconds"], context.get("known_hazards", []))
        for hazard in hazards:
            intervals = [{"start": max(window["start_seconds"]+part["start"], self.active_from),
                          "end": min(window["start_seconds"]+part["end"], window["end_seconds"])}
                         for part in hazard["intervals"]]
            intervals = [part for part in intervals if part["end"]>part["start"]]
            if not intervals:
                continue
            key = hazard["id"] or identity(hazard)
            if key not in self.hazards:
                self.hazards[key] = {"id": key, "source": "safety", "kind": hazard["kind"],
                    "subject": hazard["subject"], "target": hazard["target"], "palette_index": 20+len(self.hazards),
                    "active_from": min(part["start"] for part in intervals), "threshold_seconds": 0}
            self.hazards[key].update(name=hazard["title"], description=hazard["evidence"])
            self.detections.append({"window_id": window["id"], "event_id": key, "detected": True,
                                    "intervals": intervals, "evidence": hazard["evidence"], "status": "completed", "error": None})
        for episode in merge_detections(self.detections, gap):
            matched = [alert for alert in self.alerts if alert["event_id"] == episode["event_id"]
                       and episode["start"]-EPSILON <= alert["at"] <= episode["end"]+EPSILON]
            if matched:
                alert = matched[0]
            else:
                alert = {"id": fingerprint([episode["event_id"], episode["start"], self.generation])[:24],
                         "event_id": episode["event_id"], "source": "safety", "at": episode["start"],
                         "threshold_seconds": 0, "reason": "Potential danger detected",
                         "available_after_run_seconds": record.get("finished_after_run_seconds", 0)}
                self.alerts.append(alert)
            supporting = [item for item in self.detections if item["event_id"] == episode["event_id"]
                          and any(part["end"]>episode["start"] and part["start"]<episode["end"] for part in item["intervals"])]
            alert.update(event_name=self.hazards[episode["event_id"]]["name"],
                         detected_duration_seconds=episode["detected_seconds"], confirmed_through_seconds=window["end_seconds"],
                         evidence_span={"start": episode["start"], "end": episode["end"]},
                         supporting_window_ids=episode["window_ids"], supporting_detections=supporting,
                         evidence=" ".join(dict.fromkeys(item["evidence"] for item in supporting)))

    def snapshot(self, gap):
        return deepcopy({"safety_watch": self.enabled, "hazards": list(self.hazards.values()),
                         "detections": [{**episode, "source": "safety"} for episode in merge_detections(self.detections, gap)],
                         "alerts": self.alerts})


def mock_hazards(window):
    context = window.get("safety", {})
    if not context.get("enabled"):
        return []
    result = []
    for kind, target, title, start, end in (
            ("electrical", "power cable beside the sofa", "Dog chewing an electrical cable", 3, 38),
            ("entanglement", "cord loop near the chair", "Dog tangled in a cord", 15, 24)):
        left = max(start, window["start_seconds"], context.get("active_from", 0))
        right = min(end, window["end_seconds"])
        if right<=left:
            continue
        hazard = {"id": None, "kind": kind, "subject": "dog", "target": target, "title": title,
                  "evidence": "Scripted mock hazard for interface testing. No footage was analyzed.",
                  "intervals": [{"start": left-window["start_seconds"], "end": right-window["start_seconds"]}]}
        hazard["id"] = next((item["id"] for item in context.get("known_hazards", [])
                             if identity(item) == identity(hazard)), None)
        result.append(hazard)
    return result
