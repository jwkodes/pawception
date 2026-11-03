"""Rebuild frame previews from a completed run's cached SAM text; no API calls."""

import argparse
import json
from pathlib import Path
import re

from meta_sam_parser import CompletedOutcome,video_segmentation_format

from pawception.offline_sam import FrameOverlays
from pawception.offline_heatmap import PositionCollector
from pawception.video import probe_video
from pawception.files import write_json

from pawception.paths import ROOT


def rebuild(root,identifier):
    if not re.fullmatch(r'[a-f0-9]{32}',identifier):
        raise ValueError('Choose a saved analysis ID')
    directory=Path(root)/'data/offline_runs'/identifier
    path=directory/'run.json';job=json.loads(path.read_text())
    if job['status']!='completed' or job['kind']!='hotspots' or job['provider']!='muse' or job.get('hotspot_method')=='changes':
        raise ValueError('Choose a completed SAM hotspot analysis')
    frames,offset,points={},0,[]
    for window in job['windows']:
        if not re.fullmatch(r'[0-9]{4}',window['id']):raise ValueError('Invalid window ID')
        folder=directory/'windows'/window['id']/'sam'
        video=directory/window['file']
        if not video.resolve().is_relative_to(directory.resolve()):raise ValueError('Invalid cached video path')
        metadata,timeline,_=probe_video(video)
        parser=video_segmentation_format().create_parser();parser.push((folder/'output.txt').read_text())
        result=parser.finish(CompletedOutcome()).result
        if any(item.severity=='error' for item in result.diagnostics):raise ValueError('Invalid saved SAM response')
        groups={}
        for record in result.records:
            if record.kind=='mask' and record.frame is not None:
                index=record.frame.frame_index
                if not 0<=index<len(timeline):raise ValueError('Saved SAM frame lies outside its input')
                groups.setdefault(index,[]).append(record)
        writer=FrameOverlays(folder,(metadata['width'],metadata['height']))
        collector=PositionCollector((metadata['width'],metadata['height']),timeline,window['start_seconds'],
                                    end=window['end_seconds'],samples=window.get('samples'))
        for index,records in sorted(groups.items()):
            collector.add(index,records)
            frame=writer.add(index,records)
            sample=window.get('samples')
            frame.update(window_id=window['id'],sample_number=offset+index+1,
                         source_seconds=sample[index]['frame_seconds'] if sample else window['start_seconds']+float(timeline[index][0]))
            frames[f"{window['id']}:{index}"]=frame
        window['frame_count']=len(timeline);offset+=len(timeline)
        if window['status']=='completed':points.extend(collector.points())
    job['sam_frames']=frames
    job['points']=points
    write_json(path,job)
    return len(frames)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run_id',nargs='+')
    for identifier in parser.parse_args().run_id:
        print(f'{identifier}: {rebuild(ROOT,identifier)} cached SAM frame previews rebuilt; no API calls.')
