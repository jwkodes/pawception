from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.live_inputs import plan_checks, analyze_inputs
from tests import test_live_muse

VISUAL='muse-spark-1.3-contributor'
AUDIO='muse-spark-1.2-contributor'

def event(mode,index=1):
    return {'id':f'event_{index}','description':'An event','active_from':0,'input_mode':mode,'resolved_mode':mode}


class InputPlanTests(unittest.TestCase):
    def test_same_model_combines_every_mode_once_and_uses_higher_effort(self):
        checks,errors=plan_checks([event('visual'),event('audio',2),event('both',3)],AUDIO,AUDIO,'low','high',{'enabled':True},True)
        self.assertEqual(len(checks),1)
        self.assertEqual(checks[0]['media_kind'],'both')
        self.assertEqual(checks[0]['effort'],'high')
        self.assertTrue(checks[0]['safety'])
        self.assertEqual(errors,{})

    def test_both_is_always_assigned_to_audio_model(self):
        checks,_=plan_checks([event('visual'),event('both',2),event('audio',3)],VISUAL,AUDIO,'medium','low',{},True)
        groups={check['model']:check for check in checks}
        self.assertEqual(groups[VISUAL]['media_kind'],'visual')
        self.assertEqual(groups[AUDIO]['media_kind'],'both')
        self.assertEqual([e['id'] for e in groups[AUDIO]['events']],['event_2','event_3'])

    def test_audio_only_sends_no_video_even_when_visual_model_is_selected(self):
        checks,_=plan_checks([event('audio')],VISUAL,AUDIO,'medium','low',{},True)
        self.assertEqual(len(checks),1)
        self.assertEqual(checks[0]['media_kind'],'audio')

    def test_no_audio_does_not_turn_an_audio_event_into_a_negative_visual_check(self):
        checks,errors=plan_checks([event('visual'),event('audio',2),event('both',3)],AUDIO,AUDIO,'medium','low',{'enabled':True},False)
        self.assertEqual([e['id'] for e in checks[0]['events']],['event_1'])
        self.assertEqual(checks[0]['media_kind'],'visual')
        self.assertEqual(set(errors),{'event_2','event_3'})

    def test_explicit_visual_choice_overrides_barking_keywords(self):
        value={**event('visual'),'description':'A sign saying barking'}
        checks,_=plan_checks([value],VISUAL,AUDIO,'medium','low',{},True)
        self.assertEqual(checks[0]['model'],VISUAL)

    def test_same_weights_with_different_contributor_variants_are_separate_choices(self):
        checks,_=plan_checks([event('visual'),event('audio',2)],'muse-spark-1.2',AUDIO,'medium','low',{},True)
        self.assertEqual(len(checks),2)


class InputObservationTests(unittest.TestCase):
    def analyze(self,events,*,audio=True,fail_audio=False,same_model=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);folder=root/'windows/0000';folder.mkdir(parents=True);(folder/'clip.mp4').write_bytes(b'video')
        job={'id':'0000','file':'windows/0000/clip.mp4','sha256':'test','start_seconds':0,'end_seconds':3,
             'input_duration_seconds':3,'safety':{'enabled':True},'audio_model':AUDIO}
        calls=[]
        def prepare(directory,window,kind,present):return window
        def worker(window,run,*args,**kwargs):
            calls.append(run)
            if fail_audio and run['request']['model']==AUDIO:return {'status':'error','error':{'status_code':500}}
            output={e['id']:{'detected':True,'intervals':[{'start':0,'end':3}],'evidence':'Observed',
                'pet_segmentation_prompt':None,'object_segmentation_prompt':None} for e in run['config']['task']['events']}
            if run['config']['task'].get('safety_watch'):output['hazards']=[]
            return {'status':'completed','output':output}
        with patch('pawception.live_inputs.has_audio',return_value=audio),patch('pawception.live_inputs.media_window',side_effect=prepare), \
             patch('pawception.pipeline_runner.analyze_window',side_effect=worker):
            result=analyze_inputs(root,job,events,AUDIO if same_model else VISUAL,'secret',0)
        return result,calls

    def test_mismatched_models_make_two_checks_and_keep_visual_results_on_audio_failure(self):
        result,calls=self.analyze([event('visual'),event('audio',2)],fail_audio=True)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(set(result['output']),{'event_1','hazards'})
        self.assertIn('event_2',result['event_errors'])

    def test_matching_models_make_one_combined_check(self):
        result,calls=self.analyze([event('visual'),event('audio',2)],same_model=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['config']['task']['input_kind'],'both')

    def test_missing_audio_is_visible_without_a_second_request(self):
        result,calls=self.analyze([event('visual'),event('audio',2)],audio=False)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['status'],'partial')
        self.assertIn('Audio unavailable',result['event_errors']['event_2'])

    def test_partial_window_keeps_visual_alert_and_audio_is_not_counted_as_analyzed(self):
        session,_=test_live_muse.MuseSessionTests.make_session(self)
        session.add_event({'name':'Barking','description':'Barking','threshold_seconds':1,'input_mode':'audio'},now=0)
        session.snapshot(3)
        result,_=self.analyze([event('visual'),event('audio',2)],audio=False)
        result['finished_after_run_seconds']=4
        session.future.set_result((deepcopy(session.active),result))
        state=session.snapshot(4)
        self.assertEqual(state['events'][0]['analyzed_windows'],1)
        self.assertEqual(state['events'][1]['analyzed_windows'],0)
        self.assertIn('Audio unavailable',state['events'][1]['input_issue'])
        self.assertEqual(state['windows'][0]['status'],'partial')
        self.assertEqual(state['events'][0]['longest_detected_seconds'],3)
