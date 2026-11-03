from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from pawception.live_inputs import analyze_inputs
from pawception.files import write_json
from tests import test_live_muse
from tests.test_safety import hazard

VISUAL='muse-spark-1.3-contributor'
AUDIO='muse-spark-1.2-contributor'


def answer():
    return {'detected':True, 'intervals':[{'start':0,'end':3}], 'evidence':'Test detection',
            'pet_segmentation_prompt':None, 'object_segmentation_prompt':None}


class ParallelPublicationTests(unittest.TestCase):
    def test_audio_is_published_while_visual_request_is_still_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);folder=root/'windows/0000';folder.mkdir(parents=True)
            (folder/'clip.mp4').write_bytes(b'video')
            job={'id':'0000','file':'windows/0000/clip.mp4','start_seconds':0,'end_seconds':3,
                 'input_duration_seconds':3,'audio_model':AUDIO}
            events=[{'id':'event_1','description':'Chewing','input_mode':'visual'},
                    {'id':'event_2','description':'Barking','input_mode':'audio'}]
            visual_started,release_visual,published=Event(),Event(),Event()
            def worker(window,run,*args,**kwargs):
                visual=run['request']['model']==VISUAL
                if visual:
                    visual_started.set()
                    if not release_visual.wait(3):raise TimeoutError('Test did not release visual check')
                elif not visual_started.wait(3):raise TimeoutError('Checks did not run in parallel')
                return {'status':'completed','finished_after_run_seconds':8 if visual else 4,
                        'output':{'event_1' if visual else 'event_2':answer()}}
            def save(path,value):
                write_json(path,value)
                if path.name=='live.progress.json' and value['record']['checks_finished']==1:published.set()
            with patch('pawception.live_inputs.has_audio',return_value=True), \
                 patch('pawception.live_inputs.media_window',side_effect=lambda root,window,*args:window), \
                 patch('pawception.pipeline_runner.analyze_window',side_effect=worker), \
                 patch('pawception.live_inputs.write_json',side_effect=save), ThreadPoolExecutor(1) as executor:
                future=executor.submit(analyze_inputs,root,job,events,VISUAL,'test',0)
                try:
                    self.assertTrue(published.wait(3))
                    self.assertFalse(future.done())
                    early=json.loads((folder/'live.progress.json').read_text())['record']
                    self.assertEqual(early['output'],{'event_2':answer()})
                    self.assertEqual(early['event_finished_after_run_seconds'],{'event_2':4})
                    self.assertEqual([check['status'] for check in early['checks']],['running','completed'])
                finally:
                    release_visual.set()
                final=future.result(timeout=3)
                self.assertEqual(final['event_finished_after_run_seconds'],{'event_1':8,'event_2':4})


class LiveProgressTests(unittest.TestCase):
    def make_session(self, **changes):
        session,executor=test_live_muse.MuseSessionTests.make_session(self,safety_watch=True,events=[
            {'name':'Chewing','description':'Chewing furniture','threshold_seconds':1,'input_mode':'visual'},
            {'name':'Barking','description':'Barking','threshold_seconds':1,'input_mode':'audio'}],**changes)
        session.snapshot(3)
        return session,executor

    def progress(self,session,ready,at,*,safety=False):
        job=deepcopy(session.active)
        job.update(file=f"windows/{job['id']}/clip.mp4")
        output={f'event_{index}':answer() for index in ready}
        if safety:output['hazards']=[hazard()]
        record={'status':'running','output':output,'checks_finished':len(ready),'checks_total':2,
                'event_finished_after_run_seconds':{f'event_{index}':at for index in ready},
                'finished_after_run_seconds':at,'safety_finished_after_run_seconds':at if safety else None,
                'checks':[{'model':VISUAL if index==1 else AUDIO,'media_kind':'visual' if index==1 else 'audio',
                    'status':'completed' if index in ready else 'running','event_ids':[f'event_{index}']} for index in (1,2)]}
        folder=session.directory/'windows'/job['id'];folder.mkdir(parents=True,exist_ok=True)
        write_json(folder/'live.progress.json',{'window':job,'record':record})
        return job,record

    def test_early_audio_alert_survives_other_check_failure_with_original_time_and_viewed_state(self):
        session,_=self.make_session()
        job,record=self.progress(session,[2],4)
        early=session.snapshot(4)
        self.assertEqual(early['active_window']['status'],'running')
        self.assertEqual([event['analyzed_windows'] for event in early['events']],[0,1])
        alert=early['alerts'][0]
        self.assertEqual(alert['available_after_run_seconds'],4)
        session.view_alert(alert['id'],4)
        record.update(status='partial',checks_finished=2,finished_after_run_seconds=5,
                      event_errors={'event_1':'Visual check failed'})
        session.future.set_result((job,record))
        final=session.snapshot(5)
        self.assertEqual(len(final['alerts']),1)
        self.assertEqual(final['alerts'][0]['id'],alert['id'])
        self.assertEqual(final['alerts'][0]['available_after_run_seconds'],4)
        self.assertIn(alert['id'],final['viewed_alert_ids'])

    def test_early_safety_result_is_applied_once_when_audio_finishes_later(self):
        session,_=self.make_session()
        job,record=self.progress(session,[1],4,safety=True)
        early=session.snapshot(4)
        self.assertEqual(len(session.safety.detections),1)
        hazard_alert=next(a for a in early['alerts'] if a.get('source')=='safety')
        record['output']['event_2']=answer()
        record['event_finished_after_run_seconds']['event_2']=5
        record.update(status='completed',checks_finished=2,finished_after_run_seconds=5)
        session.future.set_result((job,record))
        final=session.snapshot(5)
        self.assertEqual(len(session.safety.detections),1)
        self.assertEqual(next(a for a in final['alerts'] if a.get('source')=='safety')['id'],hazard_alert['id'])
        self.assertEqual(len(final['alerts']),3)

    def test_stop_keeps_published_results_and_ignores_late_progress(self):
        session,_=self.make_session()
        job,record=self.progress(session,[2],4)
        early=session.snapshot(4)
        session.stop(5)
        final=session.snapshot(5)
        self.assertEqual(final['alerts'],early['alerts'])
        self.assertEqual(final['windows'][0]['status'],'partial')
        self.assertIn('stopped',final['events'][0]['input_issue'])
        record['output']['event_1']=answer()
        record['checks_finished']=2
        write_json(session.directory/'windows'/job['id']/'live.progress.json',{'window':job,'record':record})
        self.assertEqual(session.snapshot(6)['alerts'],early['alerts'])

    def test_activation_is_enforced_for_early_results(self):
        session,_=self.make_session()
        session.events[1]['active_from']=2
        self.progress(session,[2],4)
        state=session.snapshot(4)
        self.assertEqual(state['detections'][0]['start'],2)
        self.assertEqual(state['events'][1]['longest_detected_seconds'],1)

    def test_early_results_merge_with_next_window_without_another_alert(self):
        session,_=self.make_session()
        job,record=self.progress(session,[2],4)
        alert=session.snapshot(4)['alerts'][0]
        record.update(status='partial',checks_finished=2)
        session.future.set_result((job,record))
        session.snapshot(6)
        self.progress(session,[2],7)
        state=session.snapshot(7)
        self.assertEqual(len(state['alerts']),1)
        self.assertEqual(state['alerts'][0]['id'],alert['id'])
        self.assertEqual(state['events'][1]['longest_detected_seconds'],6)

    def test_negative_check_is_analyzed_without_treating_pending_events_as_negative(self):
        session,_=self.make_session()
        job,record=self.progress(session,[2],4)
        record['output']['event_2'].update(detected=False,intervals=[])
        write_json(session.directory/'windows'/job['id']/'live.progress.json',{'window':job,'record':record})
        state=session.snapshot(4)
        self.assertEqual(state['alerts'],[])
        self.assertEqual([event['analyzed_windows'] for event in state['events']],[0,1])

    def test_webcam_uses_the_same_early_result_path(self):
        from pawception.live_webcam import WebcamSession
        from tests.test_live_mock import settings
        from tests.test_live_muse import ManualExecutor
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        session=WebcamSession(settings(provider='muse',window_seconds=3,events=[
            {'name':'Chewing','description':'Chewing','threshold_seconds':1},
            {'name':'Barking','description':'Barking','input_mode':'audio','threshold_seconds':1}]),
            now=0,root=Path(temporary.name),api_key='test',executor=ManualExecutor())
        session.plans=[{'id':'0000','start_seconds':0,'end_seconds':3,'input_duration_seconds':3}]
        session.received_seconds=3.25
        session.snapshot(3)
        self.progress(session,[2],4)
        state=session.snapshot(4)
        self.assertEqual(state['source_type'],'webcam')
        self.assertEqual(state['alerts'][0]['event_id'],'event_2')
        self.assertFalse(session.future.done())
