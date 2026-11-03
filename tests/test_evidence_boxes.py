from copy import deepcopy
import unittest

from pawception.evidence_boxes import FIELDS, validate_boxes
from pawception.monitor import monitor_format, validate_binary_observations
from pawception.pipeline import perception_prompt
from pawception.alert_images import evidence_request
from pawception.safety import SafetyWatch
from tests.test_safety import hazard


def answer(**changes):
    return {'detected':True, 'intervals':[{'start':1,'end':3}], 'evidence':'Visible interaction',
            'pet_segmentation_prompt':'tabby cat','object_segmentation_prompt':'metal can',
            'evidence_timestamp':2, 'pet_bbox':[100,200,600,900], 'object_bbox':[300,200,500,400], **changes}


class EvidenceBoxTests(unittest.TestCase):
    def task(self, **changes):
        return {'name':'Test', 'events':[{'id':'a','description':'Pet interaction'}],
                'safety_watch':True, 'visual_evidence':True, 'bounding_boxes':True, **changes}

    def test_both_custom_events_and_hazards_require_all_box_fields(self):
        task=self.task()
        h=hazard(pet_segmentation_prompt='cat',object_segmentation_prompt='can',
                 evidence_timestamp=2,pet_bbox=[100,200,600,900],object_bbox=[300,200,500,400])
        value={'a':answer(),'hazards':[h]}
        validate_binary_observations(value,task,3)
        schema=monitor_format(task,'binary')['schema']['properties']
        for field in FIELDS:
            self.assertIn(field,schema['a']['required'])
            self.assertIn(field,schema['hazards']['items']['required'])
            for key in ('a','hazards'):
                broken=deepcopy(value)
                del (broken['a'] if key=='a' else broken['hazards'][0])[field]
                with self.assertRaises(ValueError):validate_binary_observations(broken,task,3)

    def test_rejects_invalid_coordinates_and_times(self):
        for changes in ({'pet_bbox':[0,0,1001,100]}, {'pet_bbox':[500,0,100,100]},
                        {'pet_bbox':[0,0,100]}, {'pet_bbox':[False,0,100,100]},
                        {'pet_bbox':[0,0,float('nan'),100]}, {'evidence_timestamp':None},
                        {'evidence_timestamp':.9}, {'evidence_timestamp':3}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_boxes(answer(**changes),3)

    def test_false_and_audio_only_events_need_nulls(self):
        negative=answer(detected=False,intervals=[],pet_segmentation_prompt=None,object_segmentation_prompt=None,
                        evidence_timestamp=None,pet_bbox=None,object_bbox=None)
        validate_boxes(negative,3)
        with self.assertRaises(ValueError):validate_boxes({**negative,'evidence_timestamp':1},3)
        validate_boxes(answer(evidence_timestamp=None,pet_bbox=None,object_bbox=None),3,audio_only=True)
        with self.assertRaises(ValueError):validate_boxes(answer(),3,audio_only=True)

    def test_audio_only_event_inside_shared_video_request_cannot_have_boxes(self):
        task=self.task(events=[{'id':'a','description':'Barking','input_mode':'audio'}],input_kind='both',safety_watch=False)
        with self.assertRaisesRegex(ValueError,'audio-only'):validate_binary_observations({'a':answer()},task,3)

    def test_household_hazard_does_not_require_a_pet(self):
        h=hazard(kind='fire',subject='kitchen',target='toaster',pet_segmentation_prompt=None,
                 object_segmentation_prompt='toaster',pet_bbox=None,object_bbox=[200,200,400,500],evidence_timestamp=1)
        validate_binary_observations({'hazards':[h]},self.task(events=[]),3)
        watch=SafetyWatch(True)
        window={'id':'0','start_seconds':0,'end_seconds':3,'input_duration_seconds':3,'safety':watch.context()}
        watch.observe(window,{'status':'completed','output':{'hazards':[h]}},1)
        self.assertEqual(len(watch.snapshot(1)['alerts']),1)

    def test_prompt_is_generic_and_audio_prompt_requests_null_boxes(self):
        prompt=perception_prompt(self.task(),3)
        for phrase in ('evidence_timestamp','pet_bbox','object_bbox','0–1000','THAT SAME FRAME'):
            self.assertIn(phrase,prompt)
        self.assertNotIn('tabby',prompt)
        self.assertNotIn('metal can',prompt)
        self.assertIn('pet_bbox, and object_bbox must also be null',perception_prompt(self.task(input_kind='audio'),3))

    def test_image_evidence_uses_model_timestamp_only_inside_the_alert_span(self):
        alert={'event_id':'a','supporting_window_ids':['0'],'evidence_span':{'start':11,'end':13}}
        window={'id':'0','start_seconds':10,'file':'windows/0/clip.mp4'}
        record={'output':{'a':answer(evidence_timestamp=1.25)}}
        self.assertEqual(evidence_request(alert,[window],{'0':record})['source_seconds'],11.25)
        record['output']['a']['evidence_timestamp']=.5
        self.assertEqual(evidence_request(alert,[window],{'0':record})['source_seconds'],12)

    def test_activation_clipping_discards_boxes_for_an_earlier_frame(self):
        from pawception.live_muse import clip_to_activation
        record={'output':{'a':answer(evidence_timestamp=1.25)}}
        clip_to_activation(record,[{'id':'a','active_from':12}],{'start_seconds':10})
        self.assertTrue(record['output']['a']['detected'])
        self.assertEqual(record['output']['a']['intervals'],[{'start':2,'end':3}])
        self.assertTrue(all(record['output']['a'][key] is None for key in FIELDS))

    def test_renderer_uses_the_selected_frame_and_each_image_axis(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from PIL import Image
        from pawception.evidence_boxes import render_evidence
        def extract(*args):Image.new('RGB',(200,100),'black').save(args[-1])
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            with patch('pawception.video.probe_video',return_value=({},[(0,1),(1,1),(2,1)],None)), \
                 patch('pawception.video.ffmpeg',side_effect=extract) as decoder:
                preview=render_evidence(directory/'video.mp4',answer(evidence_timestamp=1.25),directory/'result')
            self.assertEqual(preview['frame_seconds'],2)
            self.assertIn('select=eq(n\\,2)',decoder.call_args.args)
            with Image.open(preview['boxes']) as image:
                red,green,blue=image.getpixel((20,50))
                self.assertGreater(green,red+50)
                red,green,blue=image.getpixel((60,30))
                self.assertGreater(red,blue+100)
