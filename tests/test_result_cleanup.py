"""Validated live evidence is visible while the upload DELETE is blocked."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

import httpx2 as httpx
from openai import OpenAI

from pawception.live_muse import analyze_prepared_window
from pawception.files import write_json


class ResultCleanupTests(unittest.TestCase):
    def run_cleanup(self, *, grouped=False, invalid=False, cleanup_fails=False, stopped=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'windows/0000'
            folder.mkdir(parents=True)
            (folder / 'clip.mp4').write_bytes(b'video')
            job = {'id':'0000', 'file':'windows/0000/clip.mp4', 'sha256':'test',
                   'start_seconds':0, 'end_seconds':3, 'input_duration_seconds':3,
                   'audio_model':'muse-spark-1.3-contributor', 'safety':{'enabled':True}}
            events = [{'id':'event_1', 'description':'A dog barking', 'active_from':0,
                       'input_mode':'both' if grouped else 'visual'}]
            published, deleting, release, cancelled = Event(), Event(), Event(), Event()
            deletions = []

            def save(path, value):
                write_json(path, value)
                if path.name == 'live.progress.json' and value['record']['checks_finished'] == 1:
                    published.set()

            def handler(request):
                if request.method == 'DELETE':
                    deletions.append(request.url.path)
                    deleting.set()
                    if not release.wait(5):
                        raise TimeoutError('Test did not release cleanup')
                    return httpx.Response(500 if cleanup_fails else 200,
                                          json={'id':'file-test', 'deleted':not cleanup_fails})
                if request.url.path == '/v1/files':
                    return httpx.Response(200, json={'id':'file-test', 'object':'file'})
                payload = json.loads(request.read())
                answer = {'detected':True, 'intervals':[{'start':0,'end':3}], 'evidence':'Visible event',
                          'pet_segmentation_prompt':None, 'object_segmentation_prompt':None,
                          'evidence_timestamp':None, 'pet_bbox':None, 'object_bbox':None}
                output = {'event_1':answer, 'hazards':[]}
                response = {'id':'resp-test', 'object':'response', 'created_at':0, 'model':payload['model'],
                    'status':'completed', 'output':[{'id':'message', 'type':'message', 'role':'assistant',
                    'status':'completed', 'content':[{'type':'output_text', 'annotations':[],
                    'text':'{}' if invalid else json.dumps(output)}]}]}
                event = {'type':'response.completed', 'sequence_number':0, 'response':response}
                return httpx.Response(200, headers={'content-type':'text/event-stream'},
                    content=f'event: response.completed\ndata: {json.dumps(event)}\n\n'.encode())

            def client(**kwargs):
                return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

            with patch('pawception.pipeline_runner.OpenAI', side_effect=client), \
                 patch('pawception.live_inputs.has_audio', return_value=True), \
                 patch('pawception.live_inputs.media_window', side_effect=lambda root, job, *args:job), \
                 patch('pawception.live_inputs.write_json', side_effect=save), \
                 patch('pawception.live_muse.write_json', side_effect=save), \
                 ThreadPoolExecutor(1) as executor:
                future = executor.submit(analyze_prepared_window, root, job, events,
                    'muse-spark-1.3-contributor', 'test', time.monotonic(), cancelled)
                try:
                    self.assertTrue(deleting.wait(3))
                    self.assertFalse(future.done())
                    if invalid:
                        self.assertFalse(published.is_set())
                    else:
                        self.assertTrue(published.wait(3))
                        early = json.loads((folder / 'live.progress.json').read_text())['record']
                        self.assertTrue(early['output']['event_1']['detected'])
                        self.assertEqual(early['output']['hazards'], [])
                        self.assertEqual(early['checks_finished'], 1)
                        ready_time = early['event_finished_after_run_seconds']['event_1']
                        if stopped:
                            cancelled.set()
                finally:
                    release.set()
                _, final = future.result(timeout=3)
            self.assertEqual(len(deletions), 1)
            raw = json.loads((folder / ('checks/0/result.json' if grouped else 'result.json')).read_text())
            self.assertGreaterEqual(raw['cleanup_seconds'], 0)
            self.assertEqual(raw['upload_cleanup'], 'failed' if cleanup_fails else 'deleted')
            if invalid:
                self.assertEqual(final['status'], 'invalid')
            else:
                self.assertEqual(final['status'], 'completed')
                check = final['checks'][0] if grouped else final
                self.assertEqual(check['finished_after_run_seconds'], ready_time)
                self.assertGreaterEqual(check['cleanup_finished_after_run_seconds'], ready_time)
                self.assertTrue(check['output']['event_1']['detected'])

    def test_visual_result_published_before_cleanup(self):
        self.run_cleanup()

    def test_grouped_result_published_before_cleanup(self):
        self.run_cleanup(grouped=True)

    def test_cleanup_failure_preserves_published_result(self):
        self.run_cleanup(grouped=True, cleanup_fails=True)

    def test_stop_after_publication_still_cleans_up(self):
        self.run_cleanup(stopped=True)

    def test_invalid_response_is_not_published(self):
        self.run_cleanup(invalid=True)
