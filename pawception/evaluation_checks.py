"""Recompute the final saved evaluation without inference or modifying reports."""

import json

from pawception.submission_eval import build_report
from pawception.paths import ROOT


def main():
    folder=ROOT/'data/evaluations/submission-20261102T082730_792914Z'
    read=lambda name:json.loads((folder/name).read_text())
    expected=read('report.json')
    actual=build_report(read('ground_truth.snapshot.json'),read('run.json'))
    for key in ('summary','custom','safety_watch','revisions'):
        if actual.get(key)!=expected.get(key):
            raise SystemExit(f'Saved evaluation differs in {key}. Inspect the scorer or saved inputs.')
    summary=actual['summary']
    print(f"Verified {summary['scored_cases']} clip/event decisions across {summary['independent_clips']} clips.")
    print(f"Mean positive temporal IoU: {summary['mean_positive_temporal_iou']:.12f}")
    print('Frozen results and revision notes match. No API calls; reports are unchanged.')
