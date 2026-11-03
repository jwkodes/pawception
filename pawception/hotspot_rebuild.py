"""Rebuild saved hotspot footprints from local cached inputs, without API calls."""

import argparse
import json
from pathlib import Path
import re
from threading import Event

from PIL import Image

from pawception.hotspot_changes import analyse_changes, background, read_frames
from pawception.files import write_json
from pawception.sam_preview_rebuild import rebuild as rebuild_sam

from pawception.paths import ROOT


def rebuild(root, identifier):
    if not re.fullmatch(r'[a-f0-9]{32}',identifier):raise ValueError('Choose a saved analysis ID')
    directory=Path(root)/'data/offline_runs'/identifier
    path=directory/'run.json';job=json.loads(path.read_text())
    if job['status']!='completed' or job['kind']!='hotspots' or job['provider']=='mock':
        raise ValueError('Choose a completed real hotspot analysis')
    backup=directory/'run.before-footprints.json'
    if not backup.exists():backup.write_bytes(path.read_bytes())
    if job.get('hotspot_method')!='changes':
        rebuild_sam(root,identifier)
        return
    samples=[sample for window in job['windows'] for sample in window.get('samples',[])]
    size=tuple((job.get('change_resolution') or {'width':160,'height':90})[key] for key in ('width','height'))
    video=directory/'sampled.mp4'
    if (directory/'background.png').exists():
        with Image.open(directory/'background.png') as saved:reference=saved.convert('L')
    else:
        reference=background(read_frames(video,len(samples),size),max_frames=job.get('background_reference_frames') or 31)
    job['points']=analyse_changes(video,samples,Event(),size=size,reference=reference)
    write_json(path,job)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id',nargs='+')
    for identifier in parser.parse_args().run_id:
        rebuild(ROOT,identifier)
        print(f'{identifier}: saved footprints rebuilt; no API calls.',flush=True)
