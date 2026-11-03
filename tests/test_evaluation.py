import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path


from pawception.evaluation import evaluate_runs, load_dataset, match_events, parse_predictions, score_case, summarize, temporal_iou, validate_dataset
from pawception.monitor import monitor_format


def annotation(label="observed", review="confirmed", timing="confirmed"):
    return {"label": label, "review_status": review, "reviewed_by": "Test human annotator", "basis": "DO_NOT_SEND_THIS_EXPECTED_LABEL_TO_MODEL",
            "intervals": [{"start": 0, "end": 10}] if label == "observed" else [],
            "timing_status": timing if label == "observed" else "not_applicable"}


def fixture(root):
    media = []
    for variant, name, content in (("480p", "videos/clip.mp4", b"small fixture"), ("original", "archive/high_res/clip.mp4", b"original fixture")):
        path = root / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        media.append({"variant": variant, "file": name, "sha256": hashlib.sha256(content).hexdigest()})
    return {"schema_version": 1, "dataset_id": "test", "version": "1", "behaviors": {"drinking": "A pet drinks."},
            "scoring": {"minimum_accuracy": 1, "interval_iou_threshold": 0.5, "boundary_tolerance_seconds": 0.5},
            "clips": [{"id": "clip", "duration_seconds": 10, "media": media, "annotations": {"drinking": annotation()}}]}


def prediction(outcome="observed", start=0, end=10):
    return {"assessment": outcome, "intervals": [{"start": start, "end": end}] if outcome == "observed" else [], "evidence": "Visible behavior", "uncertainty": None}


def make_run(root, dataset, value=None, *, number=1, variant="480p", sha=None, raw=None, planned_count=1):
    directory = root / "data/experiments" / f"20261028T120000_{number:06d}Z"
    folder = directory / "01_clip"
    folder.mkdir(parents=True)
    media = next(m for m in dataset["clips"][0]["media"] if m["variant"] == variant)
    fmt = monitor_format({"rules": [{"id": "drinking"}]}, "simple")
    path = str(root / "data" / media["file"])
    run = {"model": "test-model", "modes": ["monitor"], "prompts": {"monitor": "Detect drinking."}, "max_output_tokens": 4096,
           "response_format": fmt, "planned_videos": [path] * planned_count}
    (directory / "run.json").write_text(json.dumps(run))
    result = {"video": path, "sha256": sha or media["sha256"], "analyses": {"monitor": {"status": "completed", "response_status": "completed", "response_format": fmt}}}
    (folder / "result.json").write_text(json.dumps(result))
    (folder / "monitor.txt").write_text(raw if raw is not None else json.dumps({"drinking": value or prediction()}))
    return directory


class ScoringTests(unittest.TestCase):
    def test_unknown_is_an_abstention_and_a_positive_miss(self):
        case = score_case(annotation(), prediction("unknown"), 10)
        m = summarize([case])
        self.assertFalse(case["correct"])
        self.assertEqual(case["prediction"], "unknown")
        self.assertEqual(m["accuracy"], 0)
        self.assertEqual(m["positive_recall"], 0)
        self.assertEqual(m["decision_coverage"], 0)
        self.assertEqual(m["abstentions"], 1)
        self.assertIsNone(m["precision"])

    def test_unknown_on_negative_truth_does_not_pass_as_negative(self):
        case = score_case(annotation("not_observed"), prediction("unknown"), 10)
        self.assertFalse(case["correct"])
        self.assertEqual(summarize([case])["true_negatives"], 0)

    def test_false_alarm_and_correct_negative_are_counted(self):
        cases = [score_case(annotation("not_observed"), prediction(), 10), score_case(annotation("not_observed"), prediction("not_observed"), 10)]
        m = summarize(cases)
        self.assertEqual(m["false_positives"], 1)
        self.assertEqual(m["true_negatives"], 1)
        self.assertEqual(m["accuracy"], 0.5)

    def test_zero_length_interval_keeps_classification_but_fails_timing(self):
        case = score_case(annotation(), prediction(start=0, end=0), 10)
        self.assertTrue(case["correct"])
        self.assertTrue(case["interval_errors"])
        self.assertFalse(case["timing"]["valid"])
        self.assertEqual(case["timing"]["false_negatives"], 1)

    def test_nan_negative_and_outside_intervals_are_invalid(self):
        for start, end in ((0, float("nan")), (-1, 1), (0, 11), (True, 10)):
            with self.subTest(start=start, end=end):
                self.assertTrue(score_case(annotation(), prediction(start=start, end=end), 10)["interval_errors"])

    def test_proposed_labels_and_timing_need_explicit_opt_in(self):
        proposed = score_case(annotation(review="proposed"), prediction(), 10)
        self.assertFalse(proposed["scored"])
        truth = annotation(timing="proposed")
        self.assertIsNone(score_case(truth, prediction(), 10)["timing"])
        case = score_case(truth, prediction(), 10, include_proposed=True)
        self.assertTrue(case["timing"]["provisional"])
        self.assertEqual(case["timing"]["iou"], 1)

    def test_unassessable_truth_never_awards_free_unknown_points(self):
        for outcome in ("unknown", "observed", "not_observed"):
            case = score_case(annotation("unassessable"), prediction(outcome), 10, include_proposed=True)
            self.assertFalse(case["scored"])
            self.assertIsNone(summarize([case])["accuracy"])

    def test_missing_or_malformed_behavior_is_not_silently_ignored(self):
        missing = score_case(annotation(), None, 10)
        self.assertTrue(missing["scored"])
        self.assertFalse(missing["correct"])
        for invalid in ({"assessment": []}, {"assessment": "probably"}, []):
            self.assertEqual(score_case(annotation(), invalid, 10)["prediction"], "invalid")

    def test_duplicate_keys_and_duplicate_rule_ids_are_rejected(self):
        for text in ('{"drinking": {}, "drinking": {}}', '{"assessments":[{"rule_id":"drinking","outcome":"unknown","events":[]},{"rule_id":"drinking","outcome":"unknown","events":[]}]}'):
            with self.assertRaises(ValueError):
                parse_predictions(text)

    def test_temporal_iou_unions_overlaps_without_double_counting(self):
        expected = [{"start": 0, "end": 10}]
        predicted = [{"start": 0, "end": 7}, {"start": 5, "end": 10}]
        self.assertEqual(temporal_iou(expected, predicted), 1)
        self.assertAlmostEqual(temporal_iou(expected, [{"start": 0, "end": 8}]), 0.8)

    def test_one_prediction_cannot_match_two_episodes(self):
        expected = [{"start": 0, "end": 5}, {"start": 5, "end": 10}]
        matched = match_events(expected, [{"start": 0, "end": 10}], 0.5)
        self.assertEqual(matched["true_positives"], 1)
        self.assertEqual(matched["false_negatives"], 1)

    def test_duplicate_predictions_count_as_extra_events(self):
        event = {"start": 0, "end": 10}
        matched = match_events([event], [event, event], 0.5)
        self.assertEqual(matched["true_positives"], 1)
        self.assertEqual(matched["false_positives"], 1)

    def test_episode_matching_recovers_a_match_that_greedy_scoring_would_miss(self):
        expected = [{"start": 0, "end": 10}, {"start": 0, "end": 5}]
        predicted = [{"start": 0, "end": 8}, {"start": 5, "end": 10}]
        self.assertEqual(match_events(expected, predicted, 0.5)["true_positives"], 2)

    def test_both_saved_output_formats_are_supported(self):
        text = json.dumps({"assessments": [{"rule_id": "drinking", "outcome": "observed", "events": [{"start_seconds": 0, "end_seconds": 10}]}]})
        self.assertEqual(parse_predictions(text)["drinking"]["intervals"], [{"start": 0, "end": 10}])


class DatasetAndRunTests(unittest.TestCase):


    def test_duplicate_footage_is_not_an_independent_test_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = fixture(Path(directory))
            duplicate = copy.deepcopy(dataset["clips"][0]); duplicate["id"] = "duplicate"
            dataset["clips"].append(duplicate)
            with self.assertRaisesRegex(ValueError, "Duplicate footage"):
                validate_dataset(dataset)

    def test_media_hash_verification_and_path_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            p = root / "labels.json"; p.write_text(json.dumps(dataset))
            self.assertEqual(load_dataset(p, root=root, verify_media=True), dataset)
            (root / "data/videos/clip.mp4").write_bytes(b"wrong")
            with self.assertRaisesRegex(ValueError, "mismatched"):
                load_dataset(p, root=root, verify_media=True)
            dataset["clips"][0]["media"][0]["file"] = "../secret.mp4"
            with self.assertRaisesRegex(ValueError, "relative"):
                validate_dataset(dataset)

    def test_zero_intervals_fail_gate_even_with_correct_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            run = make_run(root, dataset, prediction(start=0, end=0))
            report = evaluate_runs(dataset, [run])
            self.assertEqual(report["summary"]["accuracy"], 1)
            self.assertEqual(report["gate"]["status"], "fail")
            self.assertEqual(report["summary"]["invalid_interval_cases"], 1)

    def test_valid_but_wrong_boundaries_fail_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            run = make_run(root, dataset, prediction(end=8))
            report = evaluate_runs(dataset, [run])
            self.assertEqual(report["summary"]["accuracy"], 1)
            self.assertEqual(report["summary"]["timing"]["mean_iou"], 0.8)
            self.assertEqual(report["gate"]["status"], "fail")

    def test_repeat_variability_and_distinct_clip_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            runs = [make_run(root, dataset, prediction(), number=1), make_run(root, dataset, prediction("unknown"), number=2)]
            report = evaluate_runs(dataset, runs)
            self.assertEqual(report["summary"]["accuracy"], 0.5)
            self.assertEqual(report["summary"]["independent_clips"], 1)
            self.assertTrue(report["repeat_consistency"][0]["changed"])
            self.assertEqual(report["repeat_consistency"][0]["agreement"], 0.5)

    def test_mismatched_video_hash_is_not_matched_by_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            run = make_run(root, dataset, sha="f" * 64)
            report = evaluate_runs(dataset, [run])
            self.assertEqual(len(report["unmatched_media"]), 1)
            self.assertEqual(report["summary"]["scored_cases"], 0)
            self.assertEqual(report["gate"]["status"], "fail")

    def test_omitting_a_confirmed_behavior_from_request_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            dataset["behaviors"]["eating"] = "A pet eats."
            dataset["clips"][0]["annotations"]["eating"] = annotation()
            run = make_run(root, dataset)
            report = evaluate_runs(dataset, [run])
            self.assertEqual(report["summary"]["accuracy"], 1)
            self.assertEqual(report["summary"]["unrequested_confirmed_cases"], 1)
            self.assertEqual(report["gate"]["status"], "fail")

    def test_missing_repeated_trials_are_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            run = make_run(root, dataset, planned_count=3)
            report = evaluate_runs(dataset, [run])
            self.assertEqual(report["summary"]["infrastructure_failure_cases"], 2)
            self.assertEqual(report["gate"]["status"], "fail")

    def test_prose_is_not_graded_by_guessing_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            run = make_run(root, dataset)
            data = json.loads((run / "run.json").read_text()); data["modes"] = ["describe"]
            (run / "run.json").write_text(json.dumps(data))
            report = evaluate_runs(dataset, [run])
            self.assertEqual(report["summary"]["scored_cases"], 0)
            self.assertEqual(len(report["skipped_runs"]), 1)
            self.assertEqual(report["gate"]["status"], "fail")



    def test_developer_view_scores_saved_output_against_current_labels(self):
        from pawception.experiments import experiment_detail
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); dataset = fixture(root)
            labels = root / "data/ground_truth/pet_behaviors.v1.json"
            labels.parent.mkdir(); labels.write_text(json.dumps(dataset))
            run = make_run(root, dataset, prediction(end=8))
            detail = experiment_detail(root, run.name)
            self.assertEqual(detail["evaluation"]["summary"]["accuracy"], 1)
            self.assertEqual(detail["evaluation"]["gate"]["status"], "fail")
            self.assertNotIn("evaluation", experiment_detail(root, run.name, include_outputs=False))
