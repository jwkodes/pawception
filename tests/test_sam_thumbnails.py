import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

import httpx2 as httpx
from openai import OpenAI
from PIL import Image

from pawception.sam_evidence import create_evidence
from pawception.muse import BASE_URL
from pawception.files import write_json

FRAME=(Path(__file__).parent/'fixtures/sam_frame.txt').read_text()


class ThumbnailTests(unittest.TestCase):
    def test_household_hazard_without_targets_keeps_source_frame_without_sam_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'clip.mp4').write_bytes(b'video')
            def extract(*args):Image.new('RGB',(100,80),'gray').save(args[-1])
            with patch('pawception.sam_evidence.ffmpeg',side_effect=extract), patch('pawception.sam_evidence.OpenAI') as client:
                result=create_evidence(root/'clip.mp4',1,{'pet':None,'object':None},root,'secret')
            client.assert_not_called()
            self.assertEqual(result['status'],'no_matches')
            self.assertTrue((root/result['original']).is_file())
            self.assertTrue((root/result['zoom']).is_file())

    def test_object_only_hazard_submits_one_image_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'clip.mp4').write_bytes(b'video')
            def extract(*args):Image.new('RGB',(100,80),'white').save(args[-1])
            with patch('pawception.sam_evidence.ffmpeg',side_effect=extract), patch('pawception.sam_evidence.OpenAI'), \
                 patch('pawception.sam_evidence.segment_image',return_value={'segments':[]}) as segment:
                result=create_evidence(root/'clip.mp4',1,{'pet':None,'object':'silver toaster'},root,'secret')
            segment.assert_called_once()
            self.assertEqual(segment.call_args.args[2],'silver toaster')
            self.assertEqual(result['completed_concepts'],['object'])

    def test_both_requests_are_parallel_and_receive_the_same_image_not_a_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'clip.mp4').write_bytes(b'video')
            bodies=[]; parallel=Barrier(2)
            def extract(*args): Image.new('RGB',(490,480),'white').save(args[-1])
            def handler(request):
                body=json.loads(request.read());bodies.append(body)
                parallel.wait(timeout=3)
                response={'id':'resp-test','object':'response','created_at':0,'model':'sam-3.1','status':'completed',
                          'output':[{'id':'msg-test','type':'message','role':'assistant','status':'completed',
                                     'content':[{'type':'output_text','text':FRAME,'annotations':[]}]}]}
                delta=json.dumps({'type':'response.output_text.delta','delta':FRAME})
                complete=json.dumps({'type':'response.completed','response':response})
                return httpx.Response(200,headers={'content-type':'text/event-stream'},
                    content=f'data: {delta}\n\ndata: {complete}\n\ndata: [DONE]\n\n')
            client=OpenAI(api_key='test-secret',base_url=BASE_URL,max_retries=0,
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            with patch('pawception.sam_evidence.ffmpeg',side_effect=extract) as decode, \
                 patch('pawception.sam_evidence.OpenAI',return_value=client):
                result=create_evidence(root/'clip.mp4',2.5,{'pet':'cat','object':'can'},root,'test-secret')
            self.assertEqual(decode.call_count,1)
            self.assertEqual(result['status'],'ready')
            self.assertEqual(result['media_type'],'input_image')
            self.assertEqual(result['matches'],{'pet':1,'object':1})
            self.assertNotIn('tracking',result)
            self.assertEqual(len(bodies),2)
            for body in bodies:
                media=body['input'][0]['content'][1]
                self.assertEqual(media['type'],'input_image')
                self.assertNotIn('file_id',media)
                self.assertEqual(base64.b64decode(media['image_url'].split(',',1)[1]),(root/'original.jpg').read_bytes())
            self.assertNotIn('test-secret',json.dumps(result))

    def test_first_concept_publishes_preview_while_the_other_is_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'clip.mp4').write_bytes(b'video')
            both_started=Barrier(2); release=Event(); published=Event()
            def extract(*args): Image.new('RGB',(100,80),'white').save(args[-1])
            def segment(client,image,prompt,directory,cancelled):
                directory.mkdir(parents=True,exist_ok=True)
                Image.new('L',(100,80),255).save(directory/'mask.png')
                both_started.wait(timeout=3)
                if prompt=='can': release.wait(timeout=3)
                return {'segments':[{'box':[20,20,30,30],'mask_file':str(directory/'mask.png')}]}
            def publish(path,value):
                write_json(path,value)
                if path.name=='result.json' and value.get('completed_concepts')==['pet']:
                    published.set()
            with patch('pawception.sam_evidence.ffmpeg',side_effect=extract), \
                 patch('pawception.sam_evidence.OpenAI'), patch('pawception.sam_evidence.segment_image',side_effect=segment), \
                 patch('pawception.sam_evidence.write_json',side_effect=publish), ThreadPoolExecutor(max_workers=1) as worker:
                future=worker.submit(create_evidence,root/'clip.mp4',1,{'pet':'cat','object':'can'},root,'secret')
                try:
                    self.assertTrue(published.wait(timeout=3))
                    first=json.loads((root/'result.json').read_text())
                    self.assertEqual(first['status'],'processing')
                    self.assertTrue((root/first['zoom']).is_file())
                    self.assertFalse(future.done())
                finally: release.set()
                result=future.result(timeout=3)
            self.assertEqual(result['status'],'ready')
            self.assertNotEqual(first['zoom'],result['zoom'])

    def test_one_failed_concept_keeps_successful_highlights_without_retrying(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'clip.mp4').write_bytes(b'video')
            def extract(*args): Image.new('RGB',(100,80),'white').save(args[-1])
            def segment(client,image,prompt,directory,cancelled):
                if prompt=='can': raise ValueError('secret')
                directory.mkdir(parents=True,exist_ok=True);Image.new('L',(100,80),255).save(directory/'mask.png')
                return {'segments':[{'box':[20,20,30,30],'mask_file':str(directory/'mask.png')}]}
            with patch('pawception.sam_evidence.ffmpeg',side_effect=extract), patch('pawception.sam_evidence.OpenAI'), \
                 patch('pawception.sam_evidence.segment_image',side_effect=segment) as calls:
                result=create_evidence(root/'clip.mp4',1,{'pet':'cat','object':'can'},root,'secret')
            self.assertEqual(calls.call_count,2)
            self.assertEqual(result['status'],'ready')
            self.assertEqual(result['matches'],{'pet':1,'object':0})
            self.assertIn('object',result['errors'])
            self.assertNotIn('secret',json.dumps(result))
