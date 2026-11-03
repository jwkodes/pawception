import json
from pathlib import Path
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch

import httpx2 as httpx
from openai import OpenAI

from pawception.live_inputs import analyze_inputs
from pawception.live_muse import analyze_live_window
from pawception.muse import BASE_URL, analyze_video

VISUAL = 'muse-spark-1.3-contributor'
AUDIO = 'muse-spark-1.2-contributor'


class InputTransportTests(unittest.TestCase):
    def run_checks(self, same_model=False, mixed=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root/'windows/0000'
            folder.mkdir(parents=True)
            (folder/'clip.mp4').write_bytes(b'video with audio')
            (folder/'audio.wav').write_bytes(b'audio only')
            events = [{'id':'event_1', 'description':'A red screen', 'input_mode':'visual'},
                      {'id':'event_2', 'description':'A tone', 'input_mode':'both' if mixed else 'audio'}]
            job = {'id':'0000', 'file':'windows/0000/clip.mp4', 'input_duration_seconds':3,
                   'start_seconds':0, 'end_seconds':3, 'audio_model':AUDIO}
            calls, lock = [], Lock()
            def handler(request):
                with lock:
                    calls.append(request)
                    index = len(calls)
                if request.method == 'DELETE':
                    return httpx.Response(200, json={'id':request.url.path.split('/')[-1], 'deleted':True})
                if request.url.path == '/v1/files':
                    return httpx.Response(200, json={'id':f'file-{index}', 'object':'file'})
                payload = json.loads(request.read())
                audio_only = request.url.path == '/v1/chat/completions'
                schema = payload['response_format']['json_schema']['schema'] if audio_only else payload['text']['format']['schema']
                output = {key:{'detected':True, 'intervals':[{'start':0,'end':3}], 'evidence':'Test observation',
                          'pet_segmentation_prompt':None, 'object_segmentation_prompt':None,
                          'evidence_timestamp':None, 'pet_bbox':None, 'object_bbox':None}
                          for key in schema['properties']}
                if audio_only:
                    self.assertEqual(payload['messages'][0]['content'][1]['input_audio']['format'],'wav')
                    chunk = {'id':'chat-test','object':'chat.completion.chunk','created':0,'model':payload['model'],
                             'choices':[{'index':0,'delta':{'content':json.dumps(output)},'finish_reason':'stop'}]}
                    return httpx.Response(200,headers={'content-type':'text/event-stream'},
                                          content=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n'.encode())
                response = {'id':f'resp-{index}', 'object':'response', 'created_at':0, 'model':payload['model'],
                    'status':'completed', 'output':[{'id':'message', 'type':'message', 'role':'assistant', 'status':'completed',
                    'content':[{'type':'output_text', 'text':json.dumps(output), 'annotations':[]}]}]}
                event = {'type':'response.completed','sequence_number':0,'response':response}
                return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                      content=f'event: response.completed\ndata: {json.dumps(event)}\n\n'.encode())
            def client(**kwargs):
                return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            def media(directory, window, kind, present):
                return {**window, 'sha256':'test', 'file':'windows/0000/audio.wav' if kind=='audio' else window['file']}
            with patch('pawception.live_inputs.has_audio',return_value=True), \
                 patch('pawception.live_inputs.media_window',side_effect=media), \
                 patch('pawception.pipeline_runner.OpenAI',side_effect=client):
                result = analyze_inputs(root, job, events, AUDIO if same_model else VISUAL, 'test-key', 0)
            self.assertEqual(result['status'],'completed',result)
            self.assertEqual(set(result['output']),{'event_1','event_2'})
            self.assertTrue(all(check['upload_cleanup'] in {'deleted','not_required_inline'} for check in result['checks']))
            return calls

    def test_matching_models_use_one_upload_one_request_and_one_cleanup(self):
        calls = self.run_checks(same_model=True)
        self.assertEqual([(r.method,r.url.path) for r in calls],
                         [('POST','/v1/files'),('POST','/v1/responses'),('DELETE','/v1/files/file-1')])
        self.assertIn(b'filename="clip.mp4"',calls[0].read())
        self.assertEqual(json.loads(calls[1].read())['input'][0]['content'][1]['type'],'input_file')

    def test_different_models_upload_audio_only_for_sound_event(self):
        calls = self.run_checks()
        uploads = [r for r in calls if r.method=='POST' and r.url.path=='/v1/files']
        requests = {json.loads(r.read())['model']:json.loads(r.read()) for r in calls if r.url.path in {'/v1/responses','/v1/chat/completions'}}
        self.assertEqual(len(uploads),1)
        self.assertEqual(requests[AUDIO]['messages'][0]['content'][1]['type'],'input_audio')
        self.assertEqual(requests[VISUAL]['input'][0]['content'][1]['type'],'input_file')
        self.assertEqual(sum(r.method=='DELETE' for r in calls),1)

    def test_visual_and_audio_event_uses_video_on_audio_model(self):
        calls = self.run_checks(mixed=True)
        requests = [json.loads(r.read()) for r in calls if r.url.path=='/v1/responses']
        audio = next(value for value in requests if value['model']==AUDIO)
        self.assertEqual(audio['input'][0]['content'][1]['type'],'input_file')
        self.assertIn('both',audio['input'][0]['content'][0]['text'])

    def test_stop_during_preparation_prevents_any_upload(self):
        cancelled = Event()
        with patch('pawception.pipeline_runner.prepare_windows',side_effect=lambda *a,**k:cancelled.set()), \
             patch('pawception.pipeline_runner.OpenAI') as client:
            from concurrent.futures import CancelledError
            with self.assertRaises(CancelledError):
                analyze_live_window(None,None,{},[],VISUAL,'test',0,cancelled)
            client.assert_not_called()

    def test_stop_after_upload_still_deletes_file_and_skips_inference(self):
        cancelled, requests = Event(), []
        def handler(request):
            requests.append(request)
            if request.method=='DELETE':
                return httpx.Response(200,json={'id':'file-1','deleted':True})
            cancelled.set()
            return httpx.Response(200,json={'id':'file-1','object':'file'})
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'source.mp4';source.write_bytes(b'video')
            with OpenAI(api_key='test',base_url=BASE_URL,http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                result=analyze_video(client,source,{'describe':'Describe.'},root,VISUAL,100,cancelled=cancelled)
        self.assertEqual([r.method for r in requests],['POST','DELETE'])
        self.assertEqual(result['upload_cleanup'],'deleted')
