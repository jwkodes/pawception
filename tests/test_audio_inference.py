import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

import httpx2 as httpx
from openai import OpenAI

from pawception.pipeline import perception_prompt
from pawception.muse import analyze_video, BASE_URL


class AudioInferenceTests(unittest.TestCase):
    def run_audio(self, handler, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);audio=folder/'private_name.wav';audio.write_bytes(b'audio')
            with OpenAI(api_key='test-secret',base_url=BASE_URL,max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                result=analyze_video(client,audio,{'describe':'Detect barking.'},folder,
                                     'muse-spark-1.2-contributor',1024,media_kind='audio',**kwargs)
            artifacts='\n'.join(path.read_text() for path in folder.iterdir() if path.suffix in {'.json','.txt','.jsonl'})
        self.assertNotIn('test-secret',artifacts)
        return result

    def test_audio_prompt_only_asks_for_sound_events_and_timestamps(self):
        prompt=perception_prompt({'input_kind':'audio','visual_evidence':True,
                                 'events':[{'id':'barking','description':'A dog loudly barking.'}]},3)
        self.assertIn('Listen to',prompt)
        self.assertIn('0 <= start < end <= 3.',prompt)
        self.assertIn('must be null',prompt)
        self.assertNotIn('visible color',prompt)
        self.assertNotIn('transcrib',prompt)

    def test_audio_api_failure_is_an_error_with_no_file_upload_or_retry(self):
        requests=[]
        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.path,'/v1/chat/completions')
            return httpx.Response(402,json={'error':{'message':'test-secret','type':'insufficient_quota'}})
        result=self.run_audio(handler)
        self.assertEqual(len(requests),1)
        self.assertEqual(result['analyses']['describe']['error']['status_code'],402)
        self.assertEqual(result['status'],'has_errors')

    def test_interrupted_audio_stream_never_becomes_a_negative_observation(self):
        def handler(request):
            chunk={'id':'chat-1','object':'chat.completion.chunk','created':0,'model':'muse-spark-1.2-contributor',
                   'choices':[{'index':0,'delta':{'content':'partial'},'finish_reason':None}]}
            return httpx.Response(200,headers={'content-type':'text/event-stream'},
                                  content=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n'.encode())
        result=self.run_audio(handler,response_options={'stream':True})
        self.assertEqual(result['analyses']['describe']['status'],'error')

    def test_stop_before_audio_request_sends_nothing(self):
        cancelled=Event();cancelled.set()
        def handler(request):
            self.fail('Stopped audio must not be sent')
        result=self.run_audio(handler,cancelled=cancelled)
        self.assertEqual(result['analyses']['describe']['error']['type'],'CancelledError')
