import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pawception.offline_spatial import SpatialJobs


class SpatialJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'data/offline_runs').mkdir(parents=True)
        self.jobs = SpatialJobs(self.root)
        self.job = {'id':'a'*32, 'status':'completed', 'kind':'hotspots', 'provider':'muse',
                    'hotspot_method':'sam', 'sam_frames':{'0':{}}}

    def test_missing_environment_has_no_side_effects(self):
        self.assertEqual(self.jobs.status(self.job['id']), {'status':'idle', 'available':False})
        with self.assertRaisesRegex(ValueError, 'environment'):
            self.jobs.start(self.job, 'source.mp4', {})
        self.assertEqual(list((self.root/'data/offline_runs').iterdir()), [])

    def test_only_completed_real_hotspot_analyses_are_accepted(self):
        for changes in [{'status':'running'}, {'provider':'mock'}, {'hotspot_method':'changes'}, {'kind':'events'}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'completed SAM or change-detection'):
                self.jobs.start({**self.job, **changes}, 'source.mp4', {})

    def test_accepts_saved_change_masks_without_sam(self):
        class Process:
            pid = 123
            def poll(self): return None
        job = {**self.job, 'provider':'local', 'hotspot_method':'changes', 'sam_frames':{},
               'points':[[0, 1, .5, .5, .5, {'png':'cached'}]]}
        with patch.object(self.jobs, 'available', return_value=True), patch('pawception.offline_spatial.subprocess.Popen', return_value=Process()):
            self.assertEqual(self.jobs.start(job, 'source.mp4', {})['status'], 'queued')

    def test_old_centre_only_change_results_need_saved_masks(self):
        job = {**self.job, 'provider':'local', 'hotspot_method':'changes', 'points':[[0, 1, .5, .5, .5]]}
        with self.assertRaisesRegex(ValueError, 'no saved foreground masks'):
            self.jobs.start(job, 'source.mp4', {})

    def test_untrusted_settings_and_paths_are_rejected(self):
        with patch.object(self.jobs, 'available', return_value=True):
            for settings in [{'voxel_size':float('nan')}, {'voxel_size':True}, {'fov':0}, {'fov':float('inf')}, {'fov':True}]:
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    self.jobs.start(self.job, 'source.mp4', settings)
        with self.assertRaises(ValueError):
            self.jobs.folder('../outside')

    def test_start_reuses_saved_run_and_stop_is_cooperative(self):
        class Process:
            pid = 123
            def poll(self): return None
        with patch.object(self.jobs, 'available', return_value=True), patch('pawception.offline_spatial.subprocess.Popen', return_value=Process()) as spawn:
            state = self.jobs.start(self.job, self.root/'source.mp4', {})
            folder = self.jobs.folder(self.job['id'], state['token'])
            inputs = json.loads((folder/'input.json').read_text())
            self.assertEqual(inputs['parent'], str(self.root/'data/offline_runs'/self.job['id']))
            self.assertIsNone(inputs['settings']['fov'])
            self.assertIn('pawception.spatial_worker', spawn.call_args.args[0])
            with self.assertRaisesRegex(ValueError, 'already running'):
                self.jobs.start(self.job, 'source.mp4', {})
            self.assertEqual(self.jobs.stop(self.job['id'])['status'], 'stopping')
            self.assertTrue((folder/'stop').exists())

    def test_reuses_background_only_for_the_same_sampling_plan(self):
        class Process:
            pid = 123
            def poll(self): return None
        job = {**self.job, 'source_id':'cat', 'windows':[{'samples':[{'frame_seconds':.3}, {'frame_seconds':.9}]}]}
        for identifier, times, created in [('b'*32, [.3, .9], '2026-01-01'), ('c'*32, [.3, 1.2], '2026-02-01')]:
            folder = self.root/'data/offline_runs'/identifier
            folder.mkdir()
            (folder/'background.png').write_bytes(identifier.encode())
            (folder/'run.json').write_text(json.dumps({'source_id':'cat', 'status':'completed', 'hotspot_method':'changes',
                'created_at':created, 'windows':[{'samples':[{'frame_seconds':value} for value in times]}]}))
        with patch.object(self.jobs, 'available', return_value=True), patch('pawception.offline_spatial.subprocess.Popen', return_value=Process()):
            state = self.jobs.start(job, 'source.mp4', {})
        self.assertEqual((self.jobs.folder(job['id'], state['token'])/'background.png').read_bytes(), ('b'*32).encode())


if __name__ == '__main__':
    unittest.main()
