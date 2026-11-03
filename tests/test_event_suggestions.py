import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from openai import APIError

from pawception.event_suggestions import MODEL, SuggestionError, suggest_descriptions, validate_suggestions
from pawception.server import ReplayHandler
from tests.test_monitor_api import Request


OPTIONS = [{"label": f"Choice {i}", "description": f"A pet chewing furniture, option {i}."} for i in range(3)]


class EventSuggestionTests(unittest.TestCase):
    def test_mock_returns_three_distinct_options_without_loading_credentials(self):
        with patch("pawception.event_suggestions.load_dotenv", side_effect=AssertionError("No credentials")), \
             patch("pawception.event_suggestions.OpenAI", side_effect=AssertionError("No API call")):
            result = suggest_descriptions({"provider": "mock", "name": "Chewing", "description": "x"*1000}, root=Path("."))
        self.assertEqual(len(result["suggestions"]), 3)
        self.assertEqual(result["provider"], "mock")
        self.assertTrue(all(len(option["description"])<=1000 for option in result["suggestions"]))

    def test_contributor_request_is_text_only_and_never_creates_a_monitor(self):
        response = Mock(status="completed", output_text=json.dumps({"suggestions": OPTIONS}))
        with patch.dict("os.environ", {"MODEL_API_KEY": "test-secret"}), \
             patch("pawception.event_suggestions.load_dotenv"), patch("pawception.event_suggestions.OpenAI") as client:
            create = client.return_value.__enter__.return_value.responses.create
            create.return_value = response
            result = suggest_descriptions({"provider": "muse", "name": "Chewing", "description": "Messing with the couch"}, root=Path("."))
        self.assertEqual(result["suggestions"], OPTIONS)
        self.assertEqual(create.call_args.kwargs["model"], MODEL)
        self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "minimal"})
        self.assertFalse(create.call_args.kwargs["store"])
        self.assertEqual({item["type"] for message in create.call_args.kwargs["input"] for item in message["content"]}, {"input_text"})
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)

    def test_selected_effort_reaches_suggestion_api(self):
        response = Mock(status="completed", output_text=json.dumps({"suggestions": OPTIONS}))
        with patch.dict("os.environ", {"MODEL_API_KEY": "test-secret"}), \
             patch("pawception.event_suggestions.load_dotenv"), patch("pawception.event_suggestions.OpenAI") as client:
            create = client.return_value.__enter__.return_value.responses.create
            create.return_value = response
            result = suggest_descriptions({"provider": "muse", "name": "Chewing", "reasoning_effort": "high"}, root=Path("."))
        self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "high"})
        self.assertEqual(result["reasoning_effort"], "high")

    def test_unsupported_suggestion_effort_is_rejected_before_credentials(self):
        with patch("pawception.event_suggestions.load_dotenv", side_effect=AssertionError("No credentials")):
            for effort in ("none", "max", None, [], "typo"):
                with self.subTest(effort=effort), self.assertRaisesRegex(ValueError, "Reasoning effort"):
                    suggest_descriptions({"provider": "muse", "name": "Chewing", "reasoning_effort": effort}, root=Path("."))

    def test_each_mock_rewrite_keeps_the_entire_combined_event(self):
        draft = "pet biting, chewing or pulling on furniture"
        result = suggest_descriptions({"provider": "mock", "description": draft}, root=Path("."))
        for option in result["suggestions"]:
            self.assertTrue(option["description"].startswith(draft))
        self.assertEqual([option["label"] for option in result["suggestions"]],
                         ["Plain language", "Visual cues", "Explicit boundaries"])

    def test_missing_key_and_bad_drafts_fail_before_request(self):
        with patch.dict("os.environ", {"MODEL_API_KEY": ""}), patch("pawception.event_suggestions.load_dotenv"), \
             patch("pawception.event_suggestions.OpenAI") as client:
            with self.assertRaisesRegex(ValueError, "MODEL_API_KEY"):
                suggest_descriptions({"provider": "muse", "name": "Chewing"}, root=Path("."))
            client.assert_not_called()
        for draft in [{}, {"name": []}, {"name": "x"*81}, {"description": "x"*1001}, {"provider": "other", "name": "Chewing"}]:
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                suggest_descriptions(draft, root=Path("."))

    def test_incomplete_duplicate_and_invalid_outputs_are_rejected(self):
        for value in [{"suggestions": OPTIONS[:2]}, {"suggestions": [OPTIONS[0]]*3}, {"suggestions": [{"label": "x", "description": ""}]*3}, {"suggestions": OPTIONS, "extra": True}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_suggestions(value)

    def test_provider_errors_are_safe_and_do_not_echo_private_request_details(self):
        with patch.dict("os.environ", {"MODEL_API_KEY": "test-secret"}), \
             patch("pawception.event_suggestions.load_dotenv"), patch("pawception.event_suggestions.OpenAI") as client:
            client.return_value.__enter__.return_value.responses.create.side_effect = APIError("private test-secret", Mock(), body=None)
            with self.assertRaises(SuggestionError) as raised:
                suggest_descriptions({"provider": "muse", "name": "Chewing"}, root=Path("."))
        self.assertNotIn("test-secret", str(raised.exception))

    def test_suggestion_endpoint_does_not_touch_live_session(self):
        request = Request("/api/monitor/suggestions", b'{"provider":"mock","name":"Chewing"}')
        with patch("pawception.server.MONITOR") as manager:
            ReplayHandler.do_POST(request)
            manager.assert_not_called()
            self.assertEqual(manager.mock_calls, [])
        self.assertEqual(request.status, 200)
        self.assertEqual(len(request.content["suggestions"]), 3)


if __name__ == "__main__":
    unittest.main()
