"""Evaluate the confirmed labeling set using the actual live monitoring code."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

from dotenv import load_dotenv

from pawception.evaluation import digest, load_dataset
from pawception.live_muse import analyze_live_window
from pawception.submission_eval import build_report, markdown_report
from pawception.video_windows import frame_windows
from pawception.video import probe_video
from pawception.muse import safe_error
from pawception.files import write_json

from pawception.paths import ROOT
DATASET = ROOT/'data/ground_truth/submission_eval.v1.json'
CONFIG = {'visual_model':'muse-spark-1.3-contributor','audio_model':'muse-spark-1.2-contributor',
          'video_reasoning_effort':'medium','audio_reasoning_effort':'medium',
          'window_seconds':60,'overlap_seconds':0,'merge_gap_seconds':1,'repeats':1}


def events_from(dataset):
    # No annotations, clip titles, expected event names or timing are consulted.
    return [{'id':event,'description':description,'input_mode':dataset['behavior_modes'][event],
             'active_from':0} for event,description in dataset['behaviors'].items()
            if dataset['behavior_types'][event]=='custom']


def stop_for_access_error(record):
    return any(isinstance(error,dict) and error.get('status_code') in {401,402,403,429}
               for error in [record.get('error'),*(check.get('error') for check in record.get('checks',[]))])


def save_report(dataset, run, output):
    write_json(output/'run.json',run)
    report=build_report(dataset,run)
    report['report_provenance']={'scorer_sha256':digest(ROOT/'pawception/submission_eval.py'),
                                 'matching_sha256':digest(ROOT/'pawception/evaluation.py'),
                                 'episode_merging_sha256':digest(ROOT/'pawception/pipeline.py'),
                                 'scored_at':datetime.now(timezone.utc).isoformat()}
    write_json(output/'report.json',report)
    (output/'report.md').write_text(markdown_report(report,dataset))
    return report


def run_suite(dataset, output, api_key):
    events=events_from(dataset)
    run={'id':output.name,'status':'running','started_at':datetime.now(timezone.utc).isoformat(),
         'dataset_sha256':digest(output/'ground_truth.snapshot.json'),'config':CONFIG,
         'git_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
         'code_hashes':{str(path):digest(ROOT/path) for path in map(Path,[
             'pawception/live_muse.py','pawception/live_inputs.py','pawception/audio_inference.py',
             'pawception/monitor.py','pawception/safety.py','pawception/pipeline.py','pawception/submission_eval.py',
             'pawception/pipeline_runner.py','pawception/evaluation_runner.py','uv.lock'])},'clips':[]}
    write_json(output/'events.json',events)
    save_report(dataset,run,output)
    stopped=False
    for clip in dataset['clips']:
        if not clip.get('enabled',True):continue
        source=ROOT/'data'/clip['media'][0]['file']
        metadata,timeline,_=probe_video(source)
        if abs(metadata['duration_seconds']-clip['duration_seconds'])>1e-6:
            raise ValueError('Source duration differs from frozen labels')
        windows=frame_windows(timeline,window_seconds=CONFIG['window_seconds'],overlap_seconds=CONFIG['overlap_seconds'])
        saved={'clip_id':clip['id'],'status':'running','windows':[]}
        run['clips'].append(saved)
        directory=output/'clips'/clip['id'];directory.mkdir(parents=True)
        clip_started=time.monotonic()
        print(f"[{len(run['clips'])}/{sum(c.get('enabled',True) for c in dataset['clips'])}] {clip['id']}: {len(windows)} window(s)",flush=True)
        for window in windows:
            job={**window,'reasoning_effort':CONFIG['video_reasoning_effort'],
                 'audio_model':CONFIG['audio_model'],'audio_reasoning_effort':CONFIG['audio_reasoning_effort'],
                 'safety':{'enabled':True,'known_hazards':[],'safety_prompt':dataset['safety_prompt'],'safety_exclusions':''}}
            started=time.monotonic()
            (directory/'windows'/window['id']).mkdir(parents=True,exist_ok=True)
            try:
                prepared,record=analyze_live_window(source,directory,deepcopy(job),deepcopy(events),
                                                   CONFIG['visual_model'],api_key,started)
            except Exception as error:
                prepared,record=job,{'status':'error','error':safe_error(error)}
            saved['windows'].append({'window':prepared,'record':record})
            write_json(directory/'windows'/window['id']/'evaluation.observation.json',record)
            save_report(dataset,run,output)
            checks=', '.join(f"{c['media_kind']}={c['status']}" for c in record.get('checks',[]))
            print(f"  {window['id']}: {record['status']} · {checks} · {time.monotonic()-started:.1f}s including cleanup",flush=True)
            if stop_for_access_error(record):stopped=True;break
        saved['total_seconds']=round(time.monotonic()-clip_started,3)
        saved['status']='completed' if all(item['record']['status'] in {'completed','partial'} and
            all(check['status']=='completed' for check in item['record'].get('checks',[])) for item in saved['windows']) else 'has_errors'
        save_report(dataset,run,output)
        if stopped:print('Stopped new calls after an access, credit or rate-limit error.',flush=True);break
    run.update(status='stopped' if stopped else 'has_errors' if any(c['status']=='has_errors' for c in run['clips']) else 'completed',
               finished_at=datetime.now(timezone.utc).isoformat())
    return save_report(dataset,run,output)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,default=DATASET)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--score-only',type=Path,help='Rebuild a report from saved observations, with no API calls')
    args=parser.parse_args()
    if args.score_only:
        dataset=json.loads((args.score_only/'ground_truth.snapshot.json').read_text())
        report=save_report(dataset,json.loads((args.score_only/'run.json').read_text()),args.score_only)
        print(json.dumps(report['summary'],indent=2));return
    dataset=load_dataset(args.dataset,root=ROOT,verify_media=True)
    included=[c for c in dataset['clips'] if c.get('enabled',True)]
    if not included or any(c.get('annotation_status')!='confirmed' for c in included):
        raise ValueError('Confirm every included clip before running evaluation')
    print(f"Plan: {len(included)} videos; {len(included)} visual checks + {sum(c['has_audio'] for c in included)} audio checks; medium effort",flush=True)
    if args.dry_run:return
    load_dotenv(ROOT/'.env')
    api_key=os.environ.get('MODEL_API_KEY','').strip()
    if not api_key:raise ValueError('Add MODEL_API_KEY to the local .env')
    output=ROOT/'data/evaluations'/('submission-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'))
    output.mkdir(parents=True)
    write_json(output/'ground_truth.snapshot.json',dataset)
    print('Results:',output,flush=True)
    report=run_suite(dataset,output,api_key)
    print(json.dumps(report['summary'],indent=2),flush=True)
