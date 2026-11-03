"""Prepare browser-readable mock replays using the real temporal engine."""

import copy
from dataclasses import asdict

from pawception.temporal import Observation, Rule, TemporalEngine

DEFAULT_SETTINGS = {
    "threshold": 3,
    "repeated_window_seconds": 30,
    "merge_gap_seconds": 1,
    "missing_window_seconds": 10,
    "include_gap": True,
}


def build_replay(fixture: dict, overrides: dict | None = None) -> dict:
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULT_SETTINGS):
        raise ValueError("Unknown replay settings")
    settings = {**DEFAULT_SETTINGS, **overrides}
    if type(settings["include_gap"]) is not bool:
        raise ValueError("include_gap must be true or false")
    if type(settings["threshold"]) is not int or not 1 <= settings["threshold"] <= 20:
        raise ValueError("Episode threshold must be an integer from 1 to 20")
    for name in ("repeated_window_seconds", "missing_window_seconds"):
        value = settings[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 600:
            raise ValueError("Time windows must be from 1 to 600 seconds")
    gap = settings["merge_gap_seconds"]
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not 0 <= gap <= 30:
        raise ValueError("Merge allowance must be from 0 to 30 seconds")
    source = copy.deepcopy(fixture)
    rules = []
    for value in source["rules"]:
        if value["kind"] == "repeated":
            value.update(threshold=settings["threshold"], window_seconds=settings["repeated_window_seconds"], merge_gap_seconds=gap)
        else:
            value["window_seconds"] = settings["missing_window_seconds"]
        rules.append(Rule(**value))
    if not settings["include_gap"]:
        for value in source["observations"]:
            if value["outcome"] == "unknown":
                value["outcome"] = "not_observed"
    snapshots = []
    notices = []
    with TemporalEngine() as engine:
        for rule in rules:
            engine.add_rule(rule)
        for value in source["observations"]:
            engine.ingest(Observation.from_dict(value))
        for at in sorted({0, *source["evaluation_times"]}):
            report = engine.advance(at)
            notices.extend(report["notifications"])
            snapshots.append({
                **report,
                "alerts": list(notices),
                "incidents": engine.incidents(),
                "available_observations": [o["id"] for o in source["observations"] if o["end"] <= at],
            })
    return {
        "source": source["source"],
        "settings": settings,
        "rules": [asdict(rule) for rule in rules],
        "observations": source["observations"],
        "duration": max(o["end"] for o in source["observations"]),
        "snapshots": snapshots,
        "summary": {"alerts": len(notices), "observations": len(source["observations"])},
    }
