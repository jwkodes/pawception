"""Deterministic temporal rules over finalized, timestamped video observations.

Times are seconds on one source-video timeline, not wall-clock time. Ingest all
results affecting an interval before advancing the watermark past that interval.
SQLite stores immutable inputs and incident state; evaluate_rule is pure Python.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

EPSILON = 1e-6


def valid_time(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    window_seconds: float
    subject: str
    behavior: str
    threshold: int = 1
    merge_gap_seconds: float = 2
    active_from: float = 0
    stream_id: str = "camera_1"

    def __post_init__(self):
        if not self.id or not self.subject or not self.behavior or not self.stream_id:
            raise ValueError("Rule identifiers, subject, and behavior must be nonempty")
        if self.kind not in {"repeated", "missing"}:
            raise ValueError("Rule kind must be repeated or missing")
        if not valid_time(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive and finite")
        if not isinstance(self.threshold, int) or isinstance(self.threshold, bool) or self.threshold < 1:
            raise ValueError("threshold must be a positive integer")
        if self.kind == "missing" and self.threshold != 1:
            raise ValueError("Missing rules do not use a repetition threshold")
        if not valid_time(self.merge_gap_seconds) or not valid_time(self.active_from):
            raise ValueError("Rule times must be nonnegative and finite")


@dataclass(frozen=True)
class Event:
    start: float
    end: float
    evidence: str

    def __post_init__(self):
        if not valid_time(self.start) or not valid_time(self.end) or self.end < self.start:
            raise ValueError("Invalid event interval")
        if not self.evidence:
            raise ValueError("Observed events must reference supporting evidence")


@dataclass(frozen=True)
class Observation:
    id: str
    rule_id: str
    start: float
    end: float
    outcome: str
    events: tuple[Event, ...] = ()
    stream_id: str = "camera_1"

    def __post_init__(self):
        if not self.id or not self.rule_id or not self.stream_id:
            raise ValueError("Observation identifiers must be nonempty")
        if not valid_time(self.start) or not valid_time(self.end) or self.end <= self.start:
            raise ValueError("Observation windows must have positive finite duration")
        if self.outcome not in {"observed", "not_observed", "unknown"}:
            raise ValueError("Invalid observation outcome")
        if (self.outcome == "observed") != bool(self.events):
            raise ValueError("Only observed windows have events, and must have at least one")
        if any(event.start < self.start or event.end > self.end for event in self.events):
            raise ValueError("Events must lie within the observation window")

    @classmethod
    def from_dict(cls, value: dict) -> Observation:
        return cls(**{**value, "events": tuple(Event(**event) for event in value.get("events", []))})


def merged_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1] + EPSILON:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def coverage_intervals(observations: list[Observation], at: float | None = None) -> list[tuple[float, float]]:
    def intervals(unknown):
        return [
            (o.start, min(o.end, at) if at is not None else o.end)
            for o in observations
            if (o.outcome == "unknown") == unknown and (at is None or o.start < at)
        ]
    reliable = merged_intervals(intervals(False))
    # Conflicting visibility reports are conservatively unknown. Positive event
    # evidence still counts, but uncertainty cannot establish a negative interval.
    for gap_start, gap_end in merged_intervals(intervals(True)):
        remaining = []
        for start, end in reliable:
            if gap_end <= start or gap_start >= end:
                remaining.append((start, end))
            else:
                if start < gap_start:
                    remaining.append((start, gap_start))
                if gap_end < end:
                    remaining.append((gap_end, end))
        reliable = remaining
    return reliable


def covers(coverage: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(left <= start + EPSILON and right >= end - EPSILON for left, right in coverage)


def episodes_for(rule: Rule, observations: list[Observation], coverage: list[tuple[float, float]], at: float) -> list[dict]:
    episodes = []
    events = sorted((Event(event.start, min(event.end, at), event.evidence) for observation in observations for event in observation.events if event.start <= at), key=lambda e: (e.start, e.end, e.evidence))
    for event in events:
        if episodes:
            previous = episodes[-1]
            # A blind interval alone cannot prove a restart. A sufficiently
            # long observable pause anywhere between positives can: the earlier
            # bout ended even if a later part of the gap became unobservable.
            separate = any(
                min(right, event.start) - max(left, previous["end"]) > rule.merge_gap_seconds + EPSILON
                for left, right in coverage
            )
            if not separate:
                previous["end"] = max(previous["end"], event.end)
                previous["evidence"] = sorted(set(previous["evidence"] + [event.evidence]))
                continue
        key = f"{rule.id}:{event.start}"
        episodes.append({"id": hashlib.sha256(key.encode()).hexdigest()[:24], "start": event.start, "end": event.end, "evidence": [event.evidence]})
    return episodes


def evaluate_rule(rule: Rule, observations: list[Observation], at: float, *, known_through: float | None = None) -> dict:
    """Evaluate only fully analyzed windows ending at/before the watermark.

An observable window represents a complete assessment for this rule and pet;
its event list must include all detected episodes in that window. An adapter
must use unknown for missing results, ambiguous pet identity, or poor visibility.
    known_through allows a delayed result to reconstruct an earlier event-time
    transition; events and coverage are still clipped at the evaluation time.
"""
    if not valid_time(at) or at < rule.active_from:
        raise ValueError("Evaluation time precedes rule activation or is invalid")
    known_through = at if known_through is None else known_through
    if not valid_time(known_through) or known_through < at:
        raise ValueError("Knowledge watermark cannot precede evaluation time")
    if any(o.rule_id != rule.id or o.stream_id != rule.stream_id or o.start < rule.active_from for o in observations):
        raise ValueError("Observations must belong to this rule, stream, and activation period")
    observations = [o for o in observations if o.end <= known_through]
    coverage = coverage_intervals(observations, at)
    episodes = episodes_for(rule, observations, coverage, at)
    result = {
        "rule_id": rule.id, "at": at, "kind": rule.kind, "state": "unknown",
        "episodes": episodes, "trigger_key": None,
        "coverage": [{"start": start, "end": end} for start, end in coverage],
        "observation_ids": sorted(o.id for o in observations if o.start <= at),
    }
    if rule.kind == "repeated":
        start = max(rule.active_from, at - rule.window_seconds)
        matching = [e for e in episodes if at - rule.window_seconds < e["start"] <= at]
        result.update(count=len(matching), window_start=start, evidence=sorted({ref for e in matching for ref in e["evidence"]}))
        if len(matching) >= rule.threshold:
            noun = "episode" if len(matching) == 1 else "episodes"
            result.update(state="satisfied", trigger_key=f"repeated:{matching[-1]['id']}", reason=f"{len(matching)} distinct {noun} within {rule.window_seconds:g} seconds")
        elif covers(coverage, start, at):
            result.update(state="clear", reason="Repetition threshold not met")
        else:
            result["reason"] = "Insufficient coverage to establish that the repetition condition is clear"
    else:
        last_seen = max((min(e.end, at) for o in observations for e in o.events if e.start <= at), default=None)
        result.update(last_seen=last_seen, covered_since=None, evidence=sorted({ref for e in episodes for ref in e["evidence"]}))
        current_run = next(((start, end) for start, end in coverage if start <= at and end >= at), None)
        if current_run is None:
            result["reason"] = "No adequate observation coverage through this time"
        else:
            anchor = max(current_run[0], last_seen if last_seen is not None else rule.active_from)
            due_at = anchor + rule.window_seconds
            result.update(covered_since=current_run[0], absence_since=anchor, due_at=due_at)
            if at + EPSILON >= due_at:
                result.update(state="satisfied", trigger_key=f"missing:{anchor}", reason=f"No qualifying event observed across {rule.window_seconds:g} covered seconds")
            else:
                # A qualifying event after an incident can clear it. Coverage
                # recovery alone is not proof that the absent behavior occurred.
                result.update(state="clear" if last_seen is not None and last_seen >= current_run[0] else "pending", reason="Waiting for a full covered interval since the last event or coverage recovery")
    return result


class TemporalEngine:
    """SQLite-backed state; use ':memory:' for an ephemeral demonstration."""

    def __init__(self, database: str | Path = ":memory:"):
        if str(database) != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES rules(id),
                end_time REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observation_rule_time ON observations(rule_id, end_time);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES rules(id),
                status TEXT NOT NULL, opened_at REAL NOT NULL, updated_at REAL NOT NULL,
                resolved_at REAL, evaluation TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_incident ON incidents(rule_id)
                WHERE status IN ('active', 'uncertain');
            CREATE TABLE IF NOT EXISTS notification_outbox (
                incident_id TEXT PRIMARY KEY REFERENCES incidents(id),
                payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
            );
        """)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def watermark(self) -> float | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key='watermark'").fetchone()
        return row[0] if row else None

    def add_rule(self, rule: Rule):
        payload = json.dumps(asdict(rule), sort_keys=True)
        existing = self.connection.execute("SELECT payload FROM rules WHERE id=?", (rule.id,)).fetchone()
        if existing:
            if existing[0] != payload:
                raise ValueError("Rules are immutable; use a new rule ID for a changed specification")
            return
        if self.watermark is not None and rule.active_from < self.watermark:
            raise ValueError("New rules cannot start before the finalized watermark")
        with self.connection:
            self.connection.execute("INSERT INTO rules VALUES (?, ?)", (rule.id, payload))

    def ingest(self, observation: Observation):
        payload = json.dumps(asdict(observation), sort_keys=True)
        existing = self.connection.execute("SELECT payload FROM observations WHERE id=?", (observation.id,)).fetchone()
        if existing:
            if existing[0] != payload:
                raise ValueError("An observation ID cannot be reused with different data")
            return
        row = self.connection.execute("SELECT payload FROM rules WHERE id=?", (observation.rule_id,)).fetchone()
        if not row:
            raise ValueError("Unknown rule")
        rule = Rule(**json.loads(row[0]))
        if observation.stream_id != rule.stream_id or observation.start < rule.active_from:
            raise ValueError("Observation stream or activation period does not match the rule")
        if self.watermark is not None and observation.start < self.watermark:
            raise ValueError("Late observation affects finalized time; buffer overlapping results before advancing")
        with self.connection:
            self.connection.execute("INSERT INTO observations VALUES (?, ?, ?, ?)", (observation.id, observation.rule_id, observation.end, payload))

    def incidents(self) -> list[dict]:
        return [{**dict(row), "evaluation": json.loads(row["evaluation"])} for row in self.connection.execute("SELECT * FROM incidents ORDER BY opened_at, id")]

    def pending_notifications(self) -> list[dict]:
        notices = [json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM notification_outbox WHERE acknowledged=0")]
        return sorted(notices, key=lambda n: (n["detected_at"], n["at"], n["rule_id"]))

    def acknowledge_notification(self, incident_id: str):
        with self.connection:
            cursor = self.connection.execute("UPDATE notification_outbox SET acknowledged=1 WHERE incident_id=?", (incident_id,))
            if cursor.rowcount == 0:
                raise ValueError("Unknown notification")

    def advance(self, at: float) -> dict:
        if not valid_time(at) or (self.watermark is not None and at < self.watermark):
            raise ValueError("Watermark must be finite, nonnegative, and monotonic")
        for row in self.connection.execute("SELECT payload FROM observations WHERE end_time>?", (at,)):
            if json.loads(row[0])["start"] < at:
                raise ValueError("An overlapping window is not yet available at this time; advance after it completes")
        report = {"time": at, "evaluations": [], "notifications": []}
        previous_watermark = self.watermark
        with self.connection:
            for row in self.connection.execute("SELECT payload FROM rules ORDER BY id").fetchall():
                rule = Rule(**json.loads(row[0]))
                if at < rule.active_from:
                    continue
                observations = [Observation.from_dict(json.loads(o[0])) for o in self.connection.execute("SELECT payload FROM observations WHERE rule_id=? AND end_time<=?", (rule.id, at))]
                # Inspect event/deadline boundaries, not just the final instant:
                # a condition can become true and clear within a delayed batch.
                boundaries = {at}
                coverage = coverage_intervals(observations)
                for start, end in coverage:
                    boundaries.update((start, end, start + rule.window_seconds))
                for observation in observations:
                    for event in observation.events:
                        boundaries.update((event.start, event.end, event.end + rule.window_seconds))
                if rule.kind == "repeated":
                    for episode in episodes_for(rule, observations, coverage, at):
                        boundaries.add(episode["start"] + rule.window_seconds)
                # A newly finalized slice can report an event beginning exactly
                # where the previous prefix ended. Re-evaluate that boundary;
                # incident/outbox deduplication makes repeated evaluation safe.
                times = sorted(t for t in boundaries if rule.active_from <= t <= at and (previous_watermark is None or t >= previous_watermark))
                for event_time in times:
                    evaluation = evaluate_rule(rule, observations, event_time, known_through=at)
                    self._apply_evaluation(rule, evaluation, at, report)
                evaluation = evaluate_rule(rule, observations, at)
                report["evaluations"].append(evaluation)
            self.connection.execute("INSERT INTO metadata VALUES ('watermark', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (at,))
        report["notifications"].sort(key=lambda n: (n["at"], n["rule_id"]))
        return report

    def _apply_evaluation(self, rule: Rule, evaluation: dict, detected_at: float, report: dict):
        at = evaluation["at"]
        active = self.connection.execute("SELECT * FROM incidents WHERE rule_id=? AND status IN ('active', 'uncertain')", (rule.id,)).fetchone()
        serialized = json.dumps(evaluation, sort_keys=True)
        if evaluation["state"] == "satisfied":
            if active:
                self.connection.execute("UPDATE incidents SET status='active', updated_at=?, evaluation=? WHERE id=?", (at, serialized, active["id"]))
            else:
                incident_id = hashlib.sha256(f"{rule.id}:{evaluation['trigger_key']}".encode()).hexdigest()[:24]
                self.connection.execute("INSERT INTO incidents VALUES (?, ?, 'active', ?, ?, NULL, ?)", (incident_id, rule.id, at, at, serialized))
                notice = {
                    "incident_id": incident_id, "rule_id": rule.id, "at": at,
                    "detected_at": detected_at, "reason": evaluation["reason"],
                    "evidence": evaluation["evidence"],
                    "observation_ids": evaluation["observation_ids"],
                    "coverage": evaluation["coverage"],
                    "last_seen": evaluation.get("last_seen"),
                    "due_at": evaluation.get("due_at"),
                }
                self.connection.execute("INSERT INTO notification_outbox (incident_id, payload) VALUES (?, ?)", (incident_id, json.dumps(notice, sort_keys=True)))
                report["notifications"].append(notice)
        elif active:
            if evaluation["state"] == "clear":
                self.connection.execute("UPDATE incidents SET status='resolved', updated_at=?, resolved_at=?, evaluation=? WHERE id=?", (at, at, serialized, active["id"]))
            else:
                self.connection.execute("UPDATE incidents SET status='uncertain', updated_at=?, evaluation=? WHERE id=?", (at, serialized, active["id"]))
