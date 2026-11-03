import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from pawception.event_inputs import input_settings, route_event
from pawception.server import ReplayHandler
from tests.test_monitor_api import Request


class EventInputTests(unittest.TestCase):
    def test_legacy_events_are_visual_and_auto_requires_an_explicit_resolution(self):
        self.assertEqual(input_settings({}),{'input_mode':'visual','resolved_mode':'visual'})
        self.assertEqual(input_settings({'input_mode':'auto','resolved_mode':'audio'})['resolved_mode'],'audio')
        for value in [{'input_mode':'auto'},{'input_mode':'invalid'},{'input_mode':'auto','resolved_mode':'auto'}]:
            with self.assertRaises(ValueError):input_settings(value)

    def test_mock_routes_without_credentials_or_requests(self):
        with patch('pawception.event_inputs.load_dotenv',side_effect=AssertionError('No key')), \
             patch('pawception.event_inputs.OpenAI',side_effect=AssertionError('No API')):
            for text,mode in [('Loud barking','audio'),('Chewing furniture','visual'),('Barking while scratching the door','both')]:
                self.assertEqual(route_event({'description':text},root=Path('.'))['resolved_mode'],mode)

    def test_muse_route_is_one_text_only_request_and_does_not_rewrite_the_event(self):
        response=Mock(status='completed',output_text=json.dumps({'resolved_mode':'both','reason':'Sound and visible interaction.'}))
        with patch.dict('os.environ',{'MODEL_API_KEY':'test-secret'}), patch('pawception.event_inputs.load_dotenv'), \
             patch('pawception.event_inputs.OpenAI') as client:
            call=client.return_value.__enter__.return_value.responses.create;call.return_value=response
            result=route_event({'provider':'muse','name':'Mixed','description':'Barking OR chewing furniture'},root=Path('.'))
        self.assertEqual(result['resolved_mode'],'both')
        call.assert_called_once()
        payload=call.call_args.kwargs
        self.assertEqual(payload['reasoning'],{'effort':'minimal'})
        self.assertEqual(payload['input'][1]['content'][0]['type'],'input_text')
        self.assertIn('Barking OR chewing furniture',payload['input'][1]['content'][0]['text'])
        self.assertNotIn('test-secret',json.dumps(result))

    def test_invalid_route_is_not_silently_visual(self):
        with patch.dict('os.environ',{'MODEL_API_KEY':'test-secret'}), patch('pawception.event_inputs.load_dotenv'), \
             patch('pawception.event_inputs.OpenAI') as client:
            client.return_value.__enter__.return_value.responses.create.return_value=Mock(status='completed',output_text='{}')
            with self.assertRaisesRegex(ValueError,'choose the inputs yourself'):
                route_event({'provider':'muse','description':'Barking'},root=Path('.'))

    def test_route_endpoint_returns_resolved_choice(self):
        request=Request('/api/monitor/route',b'{"provider":"mock","description":"loud barking"}')
        ReplayHandler.do_POST(request)
        self.assertEqual(request.status,200)
        self.assertEqual(request.content['resolved_mode'],'audio')
