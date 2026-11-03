"""Replay synthetic Muse-style observations without a key, video upload, or API."""

import argparse
import json
from pathlib import Path

from pawception.temporal import Observation, Rule, TemporalEngine

from pawception.paths import ROOT


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "data/fixtures/temporal_demo.json")
    parser.add_argument("--database", default=":memory:", help="SQLite path to persist/reopen state; default is in memory")
    parser.add_argument("--report", type=Path, help="Save replay evaluations and incidents as JSON")
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    print(fixture["source"])
    reports = []
    with TemporalEngine(args.database) as engine:
        for value in fixture["rules"]:
            engine.add_rule(Rule(**value))
        # Prefilling the mock source is safe: evaluations exclude windows ending
        # after their watermark. In production the caller buffers model results.
        for value in fixture["observations"]:
            engine.ingest(Observation.from_dict(value))
        watermark = engine.watermark
        for at in fixture["evaluation_times"]:
            if watermark is not None and at <= watermark:
                continue
            report = engine.advance(at)
            reports.append(report)
            for evaluation in report["evaluations"]:
                count = f"; episodes={evaluation['count']}" if "count" in evaluation else ""
                print(f"t={at:>3}s {evaluation['rule_id']}: {evaluation['state']}{count}")
            for notice in report["notifications"]:
                print(f"  ALERT {notice['rule_id']} at t={notice['at']:g}s (detected through t={notice['detected_at']:g}s): {notice['reason']}")
        incidents = engine.incidents()
    print(f"New alerts: {sum(len(r['notifications']) for r in reports)}; stored incidents: {len(incidents)}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"source": fixture["source"], "timeline": reports, "incidents": incidents}, indent=2) + "\n", encoding="utf-8")
    return 0
