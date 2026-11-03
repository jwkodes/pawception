from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.evaluation import validate_dataset
from pawception.labeling import LabelStore
from tests.test_monitor_api import Request
from pawception.server import ReplayHandler


class LabelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root/'data/videos/test.mp4'
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(b'test video')
        source = Path(__file__).resolve().parents[1]/'data/ground_truth/submission_eval.v1.json'
        self.dataset = json.loads(source.read_text())
        self.clip = deepcopy(self.dataset['clips'][0])
        self.clip.update(enabled=True,annotation_status='unreviewed',annotation_revision=0)
        for annotation in self.clip['annotations'].values():
            annotation.update(label='unlabeled',intervals=[],review_status='proposed',timing_status='not_applicable',reviewed_by='Pending user review',basis='Awaiting manual review.')
        self.clip['media'] = [{'variant':'480p','file':'videos/test.mp4','sha256':hashlib.sha256(b'test video').hexdigest()}]
        self.dataset['clips'] = [self.clip]
        self.dataset['revision'] = 0
        self.store = LabelStore(self.root)
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text(json.dumps(self.dataset))

    def request(self, confirm=False):
        annotations = {event:{'label':'not_observed','intervals':[],'basis':''} for event in self.dataset['behaviors']}
        annotations['barking']['label'] = 'unassessable'
        annotations['drinking'].update(label='observed',intervals=[{'start':0,'end':1},{'start':2,'end':3}])
        return {'revision':0,'annotations':annotations,'confirm':confirm,'reviewed':True,'enabled':True}

    def test_snapshot_never_confirms_labels_and_serves_only_hashed_catalog_media(self):
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['confirmed_clips'],0)
        self.assertTrue(snapshot['clips'][0]['available'])
        self.assertTrue(all(a['label']=='unlabeled' for a in snapshot['clips'][0]['annotations'].values()))
        self.assertEqual(self.store.media(self.clip['id']),self.media)
        with self.assertRaises(ValueError):self.store.media('../test')
        self.media.write_bytes(b'changed video')
        with self.assertRaisesRegex(ValueError,'differs'):self.store.media(self.clip['id'])

    def test_draft_and_confirmation_are_distinct_and_previous_revision_is_retained(self):
        value = self.request()
        draft = self.store.save_clip(self.clip['id'],value)
        self.assertEqual(draft['clips'][0]['annotation_status'],'draft')
        self.assertTrue(all(a['review_status']=='proposed' for a in draft['clips'][0]['annotations'].values()))
        value.update(confirm=True,revision=1)
        confirmed = self.store.save_clip(self.clip['id'],value)
        self.assertEqual(confirmed['confirmed_clips'],1)
        self.assertEqual(confirmed['revision'],2)
        self.assertEqual(confirmed['clips'][0]['annotations']['drinking']['intervals'],value['annotations']['drinking']['intervals'])
        self.assertEqual(confirmed['clips'][0]['annotations']['barking']['label'],'unassessable')
        saved = self.store.load()
        validate_dataset(saved)
        previous = self.store.path.parent/'revisions'/saved['dataset_id']/'000002.json'
        self.assertEqual(json.loads(previous.read_text())['clips'][0]['annotation_status'],'draft')

    def test_incomplete_positive_can_be_saved_as_draft_but_never_confirmed(self):
        value = self.request()
        value['annotations']['drinking']['intervals'] = []
        result = self.store.save_clip(self.clip['id'],value)
        annotation = result['clips'][0]['annotations']['drinking']
        self.assertIsNone(annotation['intervals'])
        self.assertEqual(annotation['timing_status'],'unlabeled')
        value.update(confirm=True,revision=1)
        with self.assertRaisesRegex(ValueError,'at least one'):self.store.save_clip(self.clip['id'],value)

    def test_requires_explicit_review_of_all_labels_and_audio_availability(self):
        for mutate, message in [
            (lambda v:v.update(reviewed=False),'reviewed'),
            (lambda v:v['annotations']['eating'].update(label='unlabeled'),'Review all'),
            (lambda v:v['annotations']['barking'].update(label='not_observed'),'no audio')]:
            with self.subTest(message=message):
                value = self.request(True)
                mutate(value)
                with self.assertRaisesRegex(ValueError,message):self.store.save_clip(self.clip['id'],value)
        self.assertEqual(self.store.load()['revision'],0)

    def test_rejects_invalid_overlapping_or_out_of_bounds_intervals(self):
        for intervals in [[{'start':0,'end':0}],[{'start':0,'end':100}],
                          [{'start':0,'end':2},{'start':1,'end':3}],[{'start':False,'end':1}],
                          [{'start':0,'end':float('nan')}]]:
            value = self.request(True)
            value['annotations']['drinking']['intervals'] = intervals
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):self.store.save_clip(self.clip['id'],value)
        value = self.request(True)
        value['annotations']['drinking'].update(label='not_observed')
        with self.assertRaisesRegex(ValueError,'Only observed'):self.store.save_clip(self.clip['id'],value)

    def test_conflicting_tab_cannot_overwrite_a_newer_review(self):
        value = self.request(True)
        self.store.save_clip(self.clip['id'],value)
        with self.assertRaisesRegex(ValueError,'another tab'):self.store.save_clip(self.clip['id'],value)
        self.assertEqual(self.store.load()['revision'],1)

    def test_excluding_an_ambiguous_clip_does_not_require_invented_labels(self):
        value = {'revision':0,'confirm':True,'enabled':False,'reviewed':False,'annotations':self.clip['annotations']}
        result = self.store.save_clip(self.clip['id'],value)
        self.assertEqual(result['included_clips'],0)
        self.assertEqual(result['clips'][0]['annotation_status'],'excluded')

    def test_post_route_saves_only_the_selected_annotation_clip(self):
        request = Request('/api/labels/'+self.clip['id'],json.dumps(self.request()).encode())
        with patch('pawception.server.LABELS',self.store):ReplayHandler.do_POST(request)
        self.assertEqual(request.status,200)
        self.assertEqual(request.content['clips'][0]['annotation_status'],'draft')


if __name__ == '__main__':
    unittest.main()
