from copy import deepcopy
import json
from pathlib import Path
import unittest

from pawception.submission_eval import build_report, clip_predictions
from pawception.evaluation_runner import events_from, stop_for_access_error


class SubmissionEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.dataset={'behaviors':{'chewing':'Pet chews furniture.','danger':'Visible danger.'},
            'behavior_modes':{'chewing':'visual','danger':'visual'},
            'behavior_types':{'chewing':'custom','danger':'safety_watch'}}

    def test_binary_and_hazard_intervals_share_source_time_without_double_counting(self):
        records=[({'id':'0','start_seconds':10},{'output':{'chewing':{'intervals':[{'start':0,'end':3}]},
                    'hazards':[{'intervals':[{'start':1,'end':3}]},{'intervals':[{'start':2,'end':4}]}]}}),
                 ({'id':'1','start_seconds':12},{'output':{'chewing':{'intervals':[{'start':0,'end':3}]},'hazards':[]}})]
        predictions=clip_predictions(self.dataset,records)
        self.assertEqual(predictions['chewing']['intervals'],[{'start':10,'end':15}])
        self.assertEqual(predictions['danger']['intervals'],[{'start':11,'end':14}])
        self.assertFalse(predictions['danger']['unavailable'])

    def test_missing_checks_are_unavailable_instead_of_false_negatives(self):
        predictions=clip_predictions(self.dataset,[({'start_seconds':0},{'output':{'hazards':[]}})])
        self.assertTrue(predictions['chewing']['unavailable'])
        self.assertFalse(predictions['danger']['unavailable'])
        self.assertTrue(clip_predictions(self.dataset,[])['danger']['unavailable'])

    def test_expected_answers_never_enter_the_event_task(self):
        self.dataset['clips']=[{'annotations':{'chewing':{'label':'observed','intervals':[{'start':9,'end':10}],
                                                         'basis':'SECRET EXPECTED ANSWER'}}}]
        events=events_from(self.dataset)
        self.assertEqual(events,[{'id':'chewing','description':'Pet chews furniture.','input_mode':'visual','active_from':0}])
        self.assertNotIn('SECRET',json.dumps(events))

    def test_reports_exclusions_and_separates_ready_time_from_cleanup(self):
        annotation={'label':'observed','intervals':[{'start':0,'end':1}],'review_status':'confirmed','timing_status':'confirmed'}
        self.dataset.update(dataset_id='test',version='1',revision=0,behavior_titles={'chewing':'Chewing','danger':'Danger'},
            scoring={'interval_iou_threshold':.5},clips=[{'id':'test','title':'Test','duration_seconds':2,'annotations':{
                'chewing':annotation,'danger':{**annotation,'label':'unassessable','intervals':[],'timing_status':'not_applicable'}}}])
        run={'id':'test','status':'completed','dataset_sha256':'hash','config':{},'clips':[{'clip_id':'test','status':'completed',
            'windows':[{'window':{'id':'0','start_seconds':0,'input_duration_seconds':2},'record':{
                'output':{'chewing':{'intervals':[{'start':0,'end':1}]},'hazards':[]},
                'event_finished_after_run_seconds':{'chewing':3},'safety_finished_after_run_seconds':4,
                'finished_after_run_seconds':9}}]}]}
        report=build_report(self.dataset,run)
        self.assertEqual(report['summary']['accuracy'],1)
        self.assertEqual(report['summary']['mean_positive_temporal_iou'],1)
        self.assertEqual(report['summary']['eligible_cases'],1)
        self.assertEqual(report['summary']['skipped_cases'],{'unassessable_ground_truth':1})
        self.assertEqual(report['latency']['mean_window_ready_seconds'],4)
        self.assertEqual(report['timings'][0]['cleanup_complete_seconds'],9)

    def test_access_credit_or_rate_limit_errors_stop_new_calls(self):
        self.assertTrue(stop_for_access_error({'checks':[{'error':{'status_code':429}}]}))
        self.assertTrue(stop_for_access_error({'error':{'status_code':402}}))
        self.assertFalse(stop_for_access_error({'error':{'status_code':500}}))

    def report_for_intervals(self, intervals, duration, gap):
        annotation={'label':'observed','intervals':[{'start':0,'end':duration}],
                    'review_status':'confirmed','timing_status':'confirmed'}
        dataset={**deepcopy(self.dataset),'dataset_id':'test','version':'1','revision':0,
            'behavior_titles':{'chewing':'Chewing','danger':'Danger'},'scoring':{'interval_iou_threshold':.5},
            'clips':[{'id':'test','title':'Test','duration_seconds':duration,'annotations':{
                'chewing':annotation,'danger':{**annotation,'label':'not_observed','intervals':[],'timing_status':'not_applicable'}}}]}
        run={'id':'test','status':'completed','dataset_sha256':'hash','config':{'merge_gap_seconds':gap},
             'clips':[{'clip_id':'test','status':'completed','windows':[{
                'window':{'id':'0','start_seconds':0,'input_duration_seconds':duration},
                'record':{'output':{'chewing':{'intervals':deepcopy(intervals)},'hazards':[]}}}]}]}
        report=build_report(dataset,run)
        self.assertEqual(run['clips'][0]['windows'][0]['record']['output']['chewing']['intervals'],intervals)
        self.assertEqual(annotation['intervals'],[{'start':0,'end':duration}])
        return report

    def test_gap_merging_recovers_four_fragments_without_inventing_detected_time(self):
        intervals=[{'start':.15,'end':3.8},{'start':4.2,'end':9.1},
                   {'start':9.6,'end':12.9},{'start':13.7,'end':18.9}]
        report=self.report_for_intervals(intervals,19.12,1)
        raw=report['raw_interval_metrics']['timing'];merged=report['summary']['timing']
        self.assertEqual((raw['event_true_positives'],raw['event_false_positives'],raw['event_false_negatives']),(0,4,1))
        self.assertEqual((merged['event_true_positives'],merged['event_false_positives'],merged['event_false_negatives']),(1,0,0))
        case=next(c for c in report['cases'] if c['behavior']=='chewing')
        self.assertEqual(len(case['predicted_episodes']),1)
        self.assertAlmostEqual(case['predicted_episodes'][0]['detected_seconds'],17.05)
        self.assertAlmostEqual(case['timing']['matched_ious'][0],17.05/19.12)

    def test_gap_threshold_preserves_separate_episodes_above_the_limit(self):
        touching=self.report_for_intervals([{'start':0,'end':2},{'start':3,'end':5}],5,1)
        separate=self.report_for_intervals([{'start':0,'end':2},{'start':3.01,'end':5}],5,1)
        self.assertEqual(touching['summary']['timing']['event_true_positives'],1)
        self.assertEqual(separate['summary']['timing']['event_true_positives'],0)
        self.assertEqual(separate['summary']['timing']['event_false_positives'],2)

    def test_bridging_a_large_gap_does_not_create_a_false_iou_match(self):
        report=self.report_for_intervals([{'start':0,'end':1},{'start':10,'end':11}],11,10)
        self.assertEqual(report['summary']['timing']['event_true_positives'],0)
        self.assertAlmostEqual(report['summary']['mean_positive_temporal_iou'],2/11)


if __name__=='__main__':unittest.main()
