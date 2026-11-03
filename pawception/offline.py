"""Persistent, explicitly started searches and SAM tracking for saved recordings."""

from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from threading import Event, RLock
import time
from uuid import uuid4

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw

from pawception.event_inputs import MODELS, needs_audio, route_event
from pawception.live_muse import analyze_prepared_window, check_cancelled
from pawception.offline_heatmap import PositionCollector, heatmap
from pawception.hotspot_footprint import footprint
from pawception.offline_spatial import SpatialJobs
from pawception.hotspot_previews import change_frame, change_frames, change_overlay, FOREGROUND_COLOR
from pawception.offline_sam import FrameOverlays, frame_count, frame_progress, estimate_remaining
from pawception.hotspot_sampling import FRAME_RATE, plan_samples, prepare_samples
from pawception.hotspot_changes import BACKGROUND_REFERENCE_FRAMES, analyse_changes, analysis_size
from pawception.pipeline import merge_detections
from pawception.reasoning import validate_effort
from pawception.sam_evidence import segment_video
from pawception.video_windows import frame_windows
from pawception.files import checksum, write_json
from pawception.video import ffmpeg, probe_video
from pawception.muse import BASE_URL, safe_error
from pawception.pipeline_runner import prepare_windows

BUSY = {'queued', 'running', 'stopping'}


def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'Choose a number between {minimum:g} and {maximum:g}')
    return value


def event_definitions(value):
    """Canonical IDs are local to a search; older one-event requests still load."""
    supplied = value.get('events')
    if supplied is None:
        description = value.get('description','')
        if not isinstance(description,str):
            raise ValueError('Enter an event description')
        supplied = [{'name':description[:80], 'description':description,
                     'input_mode':value.get('input_mode','auto')}]
    if not isinstance(supplied,list) or not 1 <= len(supplied) <= 20:
        raise ValueError('Add between one and twenty events')
    events = []
    for index,item in enumerate(supplied):
        if not isinstance(item,dict):
            raise ValueError('Enter an event name and description')
        name,description,mode = item.get('name',''),item.get('description',''),item.get('input_mode','auto')
        if not isinstance(name,str) or not name.strip() or len(name)>80 or not isinstance(description,str) or not description.strip() or len(description)>1000:
            raise ValueError('Enter an event name up to 80 characters and description up to 1000 characters')
        if mode not in {'auto','visual','audio','both'}:
            raise ValueError('Choose Auto, Visual, Audio or Visual + Audio')
        events.append({'id':f'event_{index+1}','name':name.strip(),'description':description.strip(),
                       'input_mode':mode,'active_from':0})
    return events


def saved_events(job):
    return job.get('events') or event_definitions(job)


class Offline:
    def __init__(self, root, executor=None):
        self.root, self.data = Path(root), Path(root) / 'data'
        self.lock, self.jobs, self.cancelled, self.probes = RLock(), {}, {}, {}
        self.progress_saved = {}
        self.spatial = SpatialJobs(self.root)
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='offline')
        self.directory = self.data / 'offline_runs'
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.directory.glob('*/run.json')):
            try:
                job = json.loads(path.read_text())
                if not re.fullmatch(r'[a-f0-9]{32}',job['id']) or job['id'] != path.parent.name:
                    continue
                if job['status'] in BUSY:
                    job.update(status='interrupted', message='The server stopped. Saved results are retained; no API calls were restarted.')
                    write_json(path, job)
                self.jobs[job['id']] = job
            except (ValueError, KeyError, OSError):
                continue

    def sources(self):
        result = []
        manifest = self.data / 'candidates/offline_cat_camera_manifest.json'
        if manifest.exists():
            value = json.loads(manifest.read_text())
            result.append({'id':'cat_momi', 'name':'Cat Momi · First hour', **value['working'],
                           'credit':{'creator':value['source']['channel'],'license':'CC BY',
                                     'source_url':value['source']['webpage_url'],'license_url':value['source']['license_url']}})
        manifest = self.data / 'demo/online_manifest.json'
        if manifest.exists():
            result += [{'id':entry['id'], 'name':entry['scenario'], **entry['working'],
                        'credit':{key:entry[key] for key in ('creator','license','source_url','license_url')}}
                       for entry in json.loads(manifest.read_text())['clips']]
        for path in sorted((self.data / 'offline_sources').glob('*/source.json')):
            try:
                result.append(json.loads(path.read_text()))
            except (ValueError, OSError):
                continue
        return [{**entry, 'url':f"/offline-media/{entry['id']}/video.mp4",
                 'frame_url':f"/offline-media/{entry['id']}/frame.jpg"} for entry in result
                if (self.data / entry['file']).is_file()]

    def source(self, identifier):
        entry = next((entry for entry in self.sources() if entry['id'] == identifier), None)
        if entry is None:
            raise ValueError('Recording not found')
        path = self.data / entry['file']
        if not path.is_file() or not path.resolve().is_relative_to(self.data.resolve()) or any(
                part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(self.data)):
            raise ValueError('Recording is unavailable')
        return entry, path

    def register(self, path, identifier, name):
        metadata, timeline, time_base = probe_video(path)
        if metadata['duration_seconds'] > 8*3600:
            raise ValueError('Choose a recording up to eight hours long')
        entry = {'id':identifier, 'name':str(name).strip()[:160] or 'Saved recording', **metadata,
                 'file':str(path.relative_to(self.data)), 'sha256':checksum(path)}
        write_json(path.parent / 'source.json', entry)
        with self.lock:
            self.probes[identifier] = metadata, timeline, time_base
        return entry

    def probe(self, identifier):
        entry, source = self.source(identifier)
        if checksum(source) != entry['sha256']:
            raise ValueError('Recording differs from its saved source')
        if identifier not in self.probes:
            self.probes[identifier] = probe_video(source)
        return entry, source, self.probes[identifier]

    def preview(self, identifier, seconds=None):
        entry, source = self.source(identifier)
        seconds = min(30,entry['duration_seconds']/2) if seconds is None else number(seconds,0,entry['duration_seconds']-0.001)
        folder = self.data / 'offline_previews' / identifier
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f'frame-{round(seconds*1000)}.jpg'
        if not target.exists():
            temporary = target.with_suffix('.partial.jpg')
            ffmpeg('-y', '-ss', seconds, '-i', source, '-frames:v', 1, '-vf', 'scale=960:-2', temporary)
            temporary.replace(target)
        return target

    def index(self):
        with self.lock:
            return {'sources':self.sources(), 'models':MODELS, 'runs':[
                {key:job.get(key) for key in ('id','source_id','kind','status','provider','hotspot_method','created_at','message','description','pet_prompt')}
                for job in sorted(self.jobs.values(),key=lambda job:job['created_at'],reverse=True)]}

    def background_image(self, identifier):
        job = self.jobs.get(identifier)
        if not job or job.get('hotspot_method')!='changes':
            return None
        folder = self.directory/identifier
        path = folder/'background.png'
        if not path.is_file() or any(part.is_symlink() for part in (self.directory,folder,path)):
            return None
        return path

    def sam_frame_image(self, identifier, window_id, index):
        job = self.jobs.get(identifier)
        if not job or not any(window['id']==window_id for window in job['windows']):
            return None
        if f'{window_id}:{index}' not in job.get('sam_frames',{}):
            return None
        path = self.directory/identifier/'windows'/window_id/'sam'/'frames'/f'{index:06d}.png'
        if not path.is_file() or any(part.is_symlink() for part in [path,*path.parents] if part.is_relative_to(self.directory)):
            return None
        return path

    def change_frame_image(self, identifier, index):
        with self.lock:
            job=self.jobs.get(identifier)
            if not job or job.get('hotspot_method')!='changes':return None
            frame=change_frames(job).get(str(index))
            if frame is None:return None
            folder=self.directory/identifier/'changes'
            path=folder/f'{index:06d}.png'
            if any(part.is_symlink() for part in [path,*path.parents] if part.is_relative_to(self.directory)):
                return None
            if path.exists() and not path.is_file():return None
            if not path.exists():
                folder.mkdir(exist_ok=True)
                size=tuple((job.get('change_resolution') or {'width':160,'height':90})[key] for key in ('width','height'))
                overlay=change_overlay(frame['region'],size)
                temporary=path.with_suffix('.partial.png');overlay.save(temporary);temporary.replace(path)
            return path

    def save(self, job):
        write_json(self.directory / job['id'] / 'run.json', job)

    def publish_progress(self,job,stage,done,total,window=None):
        with self.lock:
            changed=job.get('stage')!=stage
            if changed:job['phase_started_at_unix']=time.time()
            job['stage']=stage
            if window is None:
                job['local_progress']={'stage':stage,'completed':done,'total':total}
            else:
                window.update(frames_received=done,frame_count=total)
            now=time.monotonic()
            if changed or done==total or now-self.progress_saved.get(job['id'],0)>=1:
                self.save(job);self.progress_saved[job['id']]=now

    def create(self, value):
        if self.spatial.busy():
            raise ValueError('Wait for the 3D analysis to finish, or stop it before starting another analysis.')
        provider, kind = value.get('provider', 'mock'), value.get('kind')
        if provider not in {'mock','muse'} or kind not in {'events','hotspots'}:
            raise ValueError('Choose Mock or Muse, and event search or hotspots')
        method = value.get('hotspot_method','sam') if kind == 'hotspots' else None
        if kind == 'hotspots' and method not in {'sam','changes'}:
            raise ValueError('Choose SAM or change detection')
        if method == 'changes':
            provider = 'local'
        entry, _, probe = self.probe(value.get('source_id'))
        duration = probe[0]['duration_seconds']
        change_resolution = analysis_size(probe[0],value.get('change_long_edge',160)) if method=='changes' else None
        start = number(value.get('start_seconds', 0), 0, duration)
        end = number(value.get('end_seconds', duration), start, duration)
        if end <= start:
            raise ValueError('End time must be after start time')
        size = number(value.get('window_seconds', 30 if kind == 'hotspots' else 60), 1, 120)
        overlap = number(value.get('overlap_seconds', 0) if kind == 'events' else 0, 0, size-0.001)
        gap = number(value.get('merge_gap_seconds', 1), 0, 30)
        events = event_definitions(value) if kind == 'events' else []
        text = ', '.join(event['name'] for event in events) if events else ('Moving foreground' if method == 'changes' else value.get('pet_prompt',''))
        if not isinstance(text, str) or not text.strip() or len(text)>2000:
            raise ValueError('Describe the event or the pet to track')
        mode = value.get('input_mode', 'visual')
        if mode not in {'auto','visual','audio','both'}:
            raise ValueError('Choose the event inputs')
        model = value.get('model', 'muse-spark-1.3-contributor')
        audio_model = value.get('audio_model', 'muse-spark-1.2-contributor')
        if model not in MODELS or audio_model not in MODELS:
            raise ValueError('Choose a supported model')
        effort = validate_effort(value.get('reasoning_effort', 'medium'), model)
        key = None
        if provider == 'muse':
            load_dotenv(self.root / '.env', override=False)
            key = os.environ.get('MODEL_API_KEY', '').strip()
            if not key:
                raise ValueError('Add MODEL_API_KEY to the local .env file')
        samples, sampling = [], None
        if kind == 'hotspots':
            samples, windows, sampling = plan_samples(probe[1],start,end,value.get('max_frames',1000),value.get('batch_frames',250))
            if method == 'changes':
                sampling['requests'] = 0
                sampling['batch_size'] = len(samples)
                # Local background subtraction is one analysis of all samples.
                windows = [{**windows[0], 'end_seconds':samples[-1]['end'],
                            'sample_end_frame_exclusive':len(samples),
                            'input_duration_seconds':len(samples)/FRAME_RATE,'samples':samples}]
        else:
            windows = frame_windows(probe[1], start=start, end=end, window_seconds=size, overlap_seconds=overlap)
        identifier = uuid4().hex
        job = {'id':identifier, 'source_id':entry['id'], 'source_duration':duration, 'kind':kind,
               'provider':provider, 'status':'queued', 'created_at':datetime.now(timezone.utc).isoformat(),
               'description':text.strip() if kind == 'events' else None,
               'pet_prompt':text.strip() if kind == 'hotspots' else None, 'input_mode':mode,
               'events':events, 'sampling':sampling, 'hotspot_method':method,
               'background_reference_frames':min(BACKGROUND_REFERENCE_FRAMES,len(samples)) if method=='changes' else None,
               'change_resolution':dict(zip(('width','height'),change_resolution)) if change_resolution else None,
               'model':model, 'audio_model':audio_model, 'reasoning_effort':effort, 'merge_gap_seconds':gap,
               'start_seconds':start, 'end_seconds':end, 'windows':[{**w,'status':'pending'} for w in windows],
               'records':{}, 'points':[], 'completed':[], 'ambiguous_frames':0,'sam_frames':{},'stage':'queued',
               'message':'Queued. Mock results are synthetic.' if provider == 'mock' else 'Queued for analysis.'}
        with self.lock:
            if any(run['status'] in BUSY for run in self.jobs.values()):
                raise ValueError('Finish or stop the current offline analysis first')
            (self.directory / identifier).mkdir()
            self.jobs[identifier], self.cancelled[identifier] = job, Event()
            self.save(job)
            self.executor.submit(self.run, identifier, key)
        return self.snapshot(identifier)

    def stop(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            if job is None:
                raise ValueError('Analysis not found')
            if job['status'] in BUSY:
                self.cancelled[identifier].set()
                job.update(status='stopping', message='Stopping after the active request cleans up. Completed results are retained.')
                self.save(job)
        return self.snapshot(identifier)

    def run(self, identifier, key):
        job, cancelled = self.jobs[identifier], self.cancelled[identifier]
        directory = self.directory / identifier
        run_started=time.monotonic()
        try:
            with self.lock:
                job['started_at_unix']=time.time()
                job['phase_started_at_unix']=time.time()
            _, source, probe = self.probe(job['source_id'])
            with self.lock:
                job.update(status='running',stage='preparing',message='Preparing the selected footage and event inputs.')
                self.save(job)
            events = saved_events(job) if job['kind'] == 'events' else []
            for event in events:
                check_cancelled(cancelled)
                if event['input_mode'] == 'auto':
                    event['resolved_mode'] = route_event({'provider':job['provider'],'name':event['name'],
                                             'description':event['description']},root=self.root)['resolved_mode']
            job['events'] = events
            check_cancelled(cancelled)
            with self.lock:
                job.update(status='running', message='Analyzing saved footage. No playback pacing or dropped windows.')
                self.save(job)
            started = time.monotonic()
            change_points = None
            if job['kind']=='hotspots' and job['provider']!='mock':
                samples = [sample for window in job['windows'] for sample in window.get('samples',[])]
                if samples:
                    prepare_samples(source,directory,samples,job['windows'],probe[0],
                                    encode_batches=job.get('hotspot_method')!='changes')
                if job.get('hotspot_method') == 'changes':
                    with self.lock:
                        job['message'] = 'Comparing sampled frames with the background locally. No API calls.'
                        self.save(job)
                    job['change_frames']={}
                    def publish_change(index,sample,region):
                        with self.lock:job['change_frames'][str(index)]=change_frame(index,sample,region)
                    change_points = analyse_changes(directory/'sampled.mp4',samples,cancelled,
                                                    background_path=directory/'background.png',
                                                    size=tuple((job.get('change_resolution') or {'width':160,'height':90})[key] for key in ('width','height')),
                                                    on_progress=lambda stage,done,total:self.publish_progress(job,stage,done,total),
                                                    on_frame_ready=publish_change)
            for window in job['windows']:
                check_cancelled(cancelled)
                with self.lock:
                    window['status'] = 'running'
                    job['stage'] = 'preparing'
                    job['message'] = 'Publishing local change-detection results.' if change_points is not None else 'Analyzing the next saved-video window. Results appear as they become available.'
                    self.save(job)
                if change_points is not None:
                    with self.lock:
                        job['points'].extend(point for point in change_points if window['start_seconds']<=point[0]<window['end_seconds'])
                        job['completed'].append([window['start_seconds'],window['end_seconds']])
                        window['status'] = 'completed'
                elif job['provider'] == 'mock':
                    self.mock_window(job, window, probe[0])
                else:
                    if job['kind']=='events' or not window.get('samples'):
                        prepare_windows(source,[window],directory,include_audio=job['kind']=='events' and needs_audio(events),source_probe=probe)
                    check_cancelled(cancelled)
                    if job['kind'] == 'events':
                        window.update(reasoning_effort=job['reasoning_effort'], audio_reasoning_effort='medium',
                                      audio_model=job['audio_model'], safety={'enabled':False})
                        _, record = analyze_prepared_window(directory, window, events, job['model'], key, started, cancelled)
                        with self.lock:
                            job['records'][window['id']] = {k:record.get(k) for k in ('status','output','error','event_errors')}
                            window['status'] = record['status']
                    else:
                        def publish_progress(stage, received, total):
                            self.publish_progress(job,stage,received,total,window)
                        def publish_frame(frame):
                            with self.lock:
                                offset = sum(frame_count(prior) for prior in job['windows'][:window['index']])
                                job.setdefault('sam_frames',{})[f"{window['id']}:{frame['frame_index']}"] = {
                                    **frame,'window_id':window['id'],'sample_number':offset+frame['frame_index']+1}
                        def publish_tracking(points, ambiguous):
                            with self.lock:
                                if window['status'] != 'completed':
                                    job['points'].extend(points)
                                    job['completed'].append([window['start_seconds'], window['end_seconds']])
                                    job['ambiguous_frames'] += ambiguous
                                    window['status'] = 'completed'
                                    job['message'] = 'Tracking results ready. Cleaning up this segment before continuing.'
                                    self.save(job)
                        points, ambiguous = self.track(directory, window, job['pet_prompt'], key, cancelled,
                                                       on_result_ready=publish_tracking,on_progress=publish_progress,
                                                       on_frame_ready=publish_frame)
                        publish_tracking(points, ambiguous)
                with self.lock:
                    self.save(job)
                record = job['records'].get(window['id'], {})
                if window['status'] in {'error','invalid'}:
                    raise ValueError('An input check failed; completed results are retained. See the failed window.')
                if record.get('status') == 'partial':
                    job['message'] = 'Some input checks failed. Successful detections are retained.'
            check_cancelled(cancelled)
            with self.lock:
                message = 'Analysis complete.'
                if job['provider']=='mock':
                    message = 'Mock analysis complete. Locations and events are synthetic, not observations of this video.'
                elif job['provider']=='local':
                    message = 'Local change detection complete. Locations describe foreground changes, not verified pet sightings. No API calls.'
                job.update(status='completed', stage='completed',message=message)
        except CancelledError:
            with self.lock:
                job.update(status='stopped', stage='stopped',message='Stopped. Completed results are retained.')
        except Exception as error:
            with self.lock:
                job.update(status='error',stage='error',error=safe_error(error),message='Analysis failed. Completed results are retained; failed windows add no negative observations.')
                for window in job['windows']:
                    if window['status'] == 'running':
                        window.update(status='error', error=safe_error(error))
        finally:
            with self.lock:
                job['elapsed_seconds']=time.monotonic()-run_started
                for window in job['windows']:
                    if window['status'] in {'pending','running'}:
                        window['status'] = 'cancelled'
                self.save(job)

    def mock_window(self, job, window, metadata):
        left, right = window['start_seconds'], window['end_seconds']
        with self.lock:
            if job['kind'] == 'events':
                length = right-left
                answers = {event['id']:{'detected':True,'intervals':[{'start':length*0.2,'end':length*0.65}],
                          'evidence':'Synthetic mock detection; not an observation of this footage.'} for event in saved_events(job)}
                job['records'][window['id']] = {'status':'completed','output':answers}
            else:
                # Deterministic positions and visibility gaps for testing the UI.
                samples = window.get('samples') or [{'start':t,'end':min(right,t+1),'source_seconds':t} for t in range(math.ceil(left),math.ceil(right))]
                for sample in samples:
                    t = sample['source_seconds']
                    if int(t)%30 < 23:
                        x,y = ((0.2,0.35),(0.55,0.65),(0.8,0.35))[int(t//20)%3]
                        mask=Image.new('L',(100,100))
                        ImageDraw.Draw(mask).ellipse((round(x*100)-8,round(y*100)-5,
                                                     round(x*100)+8,round(y*100)+5),fill=255)
                        job['points'].append([sample['start'],sample['end'],x,y,t,footprint(mask)])
                job['completed'].append([left,right])
            window['status'] = 'completed'

    def track(self, directory, window, prompt, key, cancelled, on_result_ready=None,on_progress=None,on_frame_ready=None):
        folder = directory / 'windows' / window['id'] / 'sam'
        folder.mkdir(parents=True, exist_ok=True)
        video = directory / window['file']
        metadata, timeline, _ = probe_video(video)
        image = folder / 'frame.jpg'
        ffmpeg('-y','-i',video,'-frames:v',1,image)
        collector = PositionCollector((metadata['width'],metadata['height']), timeline,
                                      window['start_seconds'], end=window.get('end_seconds'),samples=window.get('samples'))
        overlays = FrameOverlays(folder,(metadata['width'],metadata['height']))
        complete = False
        def progress(stage):
            if on_progress:on_progress(stage,len(timeline) if complete else len(collector.frames),len(timeline))
        def receive(index,records):
            collector.add(index,records)
            frame = overlays.add(index,records)
            samples = window.get('samples')
            frame['source_seconds'] = samples[index]['frame_seconds'] if samples else window['start_seconds']+float(timeline[index][0])
            if on_frame_ready:on_frame_ready(frame)
            progress('receiving')
        file_id = None
        with OpenAI(api_key=key, base_url=BASE_URL, timeout=300, max_retries=0) as client:
            try:
                check_cancelled(cancelled)
                progress('uploading')
                with video.open('rb') as stream:
                    file_id = client.files.create(file=('clip.mp4',stream,'video/mp4'),purpose='user_data').id
                check_cancelled(cancelled)
                progress('waiting')
                segment_video(client, image, file_id, 0, prompt, folder, on_frame=receive, cancelled=cancelled)
                complete = True
                progress('results_ready')
                if on_result_ready is not None:
                    on_result_ready(collector.points(),collector.ambiguous_frames())
            finally:
                if file_id:
                    try:
                        progress('cleanup')
                    finally:
                        try:
                            receipt = client.files.delete(file_id)
                            write_json(folder/'cleanup.json', {'file_id':file_id,'deleted':receipt.deleted})
                        except Exception as error:
                            write_json(folder/'cleanup.json', {'file_id':file_id,'error':safe_error(error)})
        progress('batch_complete')
        return collector.points(), collector.ambiguous_frames()

    def snapshot(self, identifier, start=None, end=None, cell_size=None):
        with self.lock:
            job = self.jobs.get(identifier)
            if job is None:
                raise ValueError('Analysis not found')
            public = {k:deepcopy(v) for k,v in job.items() if k not in {'points','completed','records','sam_frames','change_frames'}}
            if job['kind'] == 'hotspots':
                public['spatial'] = self.spatial.status(identifier)
            for window in public['windows']:
                window.pop('samples',None)
            elapsed=job.get('elapsed_seconds')
            if job['status'] in BUSY and job.get('started_at_unix'):
                elapsed=max(0,time.time()-job['started_at_unix'])
            public['elapsed_seconds']=elapsed
            public['estimated_remaining_seconds']=0 if job['status']=='completed' else None
            public['estimated_stage_remaining_seconds']=None
            if job['status']=='running':
                if job.get('hotspot_method')=='changes':
                    work=job.get('local_progress',{})
                    if work.get('stage') in {'building_background','comparing_frames'}:
                        phase_elapsed=max(0,time.time()-job.get('phase_started_at_unix',time.time()))
                        remaining=estimate_remaining(work['completed'],work['total'],phase_elapsed,10)
                        public['estimated_stage_remaining_seconds']=remaining
                        if work['stage']=='comparing_frames':public['estimated_remaining_seconds']=remaining
                elif job['kind']=='hotspots':
                    work=frame_progress(job)
                    public['estimated_remaining_seconds']=estimate_remaining(work['received'],work['total'],elapsed,10)
                else:
                    done=sum(window['status'] in {'completed','partial'} for window in job['windows'])
                    public['estimated_remaining_seconds']=estimate_remaining(done,len(job['windows']),elapsed)
            if job.get('hotspot_method') == 'changes':
                public['sampling']['requests'] = 0
                public['background_url'] = f'/offline-results/{identifier}/background.png' if self.background_image(identifier) else None
                frames=change_frames(job)
                public['mask_previews_available']='change_frames' in job
                public['mask_frames']=[{key:value for key,value in frame.items() if key!='region'} | {
                    'overlay_url':f"/offline-results/{identifier}/changes/{frame['frame_index']:06d}.png",
                    'matches':[{'id':'foreground','color':FOREGROUND_COLOR}] if frame['region'] else []}
                    for frame in sorted(frames.values(),key=lambda frame:frame['frame_index'])]
            if job['kind'] == 'hotspots':
                if job.get('hotspot_method')!='changes':
                    public['frame_progress'] = frame_progress(job)
                    public['sam_previews_available'] = 'sam_frames' in job
                    public['sam_frames'] = [{**frame,
                        'overlay_url':f"/offline-results/{identifier}/sam/{frame['window_id']}/{frame['frame_index']:06d}.png?revision={frame['revision']}"}
                        for frame in sorted(job.get('sam_frames',{}).values(),key=lambda frame:frame['sample_number'])]
                    public['mask_frames']=public['sam_frames']
                    public['mask_previews_available']=public['sam_previews_available']
                left = number(start if start is not None else job['start_seconds'], job['start_seconds'], job['end_seconds'])
                right = number(end if end is not None else job['end_seconds'], left, job['end_seconds'])
                columns,rows=24,14
                if cell_size is not None:
                    if isinstance(cell_size,bool) or not isinstance(cell_size,int) or not 16<=cell_size<=256:
                        raise ValueError('Choose a heatmap square size from 16 to 256 pixels')
                    source,_ = self.source(job['source_id'])
                    columns,rows=(math.ceil(source[key]/cell_size) for key in ('width','height'))
                    if max(columns,rows)>256:
                        raise ValueError('Choose a larger heatmap square size for this recording')
                public['heatmap'] = heatmap(job['points'], job['completed'], left, right,columns,rows)
                public['heatmap']['sample_count'] = sum(
                    1 for window in job['windows'] if window['status']=='completed'
                    for sample in window.get('samples',[]) if sample['start']<right and sample['end']>left) or None
                public['heatmap']['estimated'] = job.get('hotspot_method')=='changes' or bool((job.get('sampling') or {}).get('sampled'))
            else:
                events = saved_events(job)
                public['events'] = [{**event,'id':f"{identifier}_{event['id']}"} for event in events]
                detections, observations = [], []
                for window in job['windows']:
                    record = job['records'].get(window['id'],{})
                    if window['status'] == 'running' and job['status'] in BUSY:
                        progress = self.directory / identifier / 'windows' / window['id'] / 'live.progress.json'
                        if progress.exists():
                            record = json.loads(progress.read_text())['record']
                    for event in events:
                        answer = (record.get('output') or {}).get(event['id'])
                        if answer:
                            observations.append({'event_id':f"{identifier}_{event['id']}",'window_id':window['id'],
                                'start_seconds':window['start_seconds'],'end_seconds':window['end_seconds'],
                                'detected':answer['detected'],'evidence':answer.get('evidence','')})
                            detections.append({'event_id':f"{identifier}_{event['id']}",'window_id':window['id'], 'detected':answer['detected'],
                                'intervals':[{'start':window['start_seconds']+part['start'], 'end':min(window['end_seconds'],window['start_seconds']+part['end'])} for part in answer['intervals']]})
                public['detections'] = merge_detections(detections, job['merge_gap_seconds'])
                public['observations'] = observations
            return public
