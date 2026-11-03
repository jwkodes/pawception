from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

from pawception.hotspot_sampling import plan_samples,prepare_samples
from pawception.offline_heatmap import PositionCollector,heatmap
from pawception.video import ffmpeg, probe_video


class SamplingTests(unittest.TestCase):
    def test_one_hour_is_capped_at_1000_frames_and_four_requests(self):
        timeline=[(Fraction(index,30),Fraction(1,30)) for index in range(108000)]
        samples,windows,summary=plan_samples(timeline,0,3600,1000)
        self.assertEqual(len(samples),1000)
        self.assertEqual(len({sample['source_index'] for sample in samples}),1000)
        self.assertEqual(len(windows),4)
        self.assertAlmostEqual(sum(sample['end']-sample['start'] for sample in samples),3600)
        self.assertAlmostEqual(summary['mean_interval_seconds'],3.6)
        self.assertTrue(summary['sampled'])
        self.assertLess(samples[0]['source_seconds'],2)
        self.assertGreater(samples[-1]['source_seconds'],3598)

    def test_short_sources_keep_every_frame_and_actual_time_weights(self):
        samples,windows,summary=plan_samples([(0,0.1),(0.1,0.3),(0.4,0.6)],0,1,1000)
        self.assertEqual([sample['source_index'] for sample in samples],[0,1,2])
        self.assertFalse(summary['sampled'])
        self.assertEqual([sample['end']-sample['start'] for sample in samples],[0.1,0.30000000000000004,0.6])

    def test_sampled_video_indices_map_to_source_time_not_compressed_time(self):
        source=[(Fraction(index),Fraction(1)) for index in range(60)]
        samples,_,_=plan_samples(source,10,50,4)
        collector=PositionCollector((10,10),[(0,0.2),(0.2,0.2),(0.4,0.2),(0.6,0.2)],10,end=50,samples=samples)
        collector.frames={0:{'pet':(0.1,0.1)},2:{'pet':(0.9,0.9)}}
        result=heatmap(collector.points(),[[10,50]],10,50,2,2)
        self.assertEqual(result['seen_seconds'],20)
        self.assertEqual(result['cells'][0]['first_seen'],15)
        self.assertEqual(result['cells'][0]['percentage'],50)
        filtered=heatmap(collector.points(),[[10,50]],12,18,2,2)
        self.assertEqual(filtered['seen_seconds'],6)

    def test_vfr_held_frames_are_not_uploaded_repeatedly(self):
        source=[(0,8)]+[(Fraction(8)+Fraction(index,10),Fraction(1,10)) for index in range(20)]
        samples,_,_=plan_samples(source,0,10,10)
        self.assertEqual(len({sample['source_index'] for sample in samples}),len(samples))
        self.assertLessEqual(len(samples),10)
        self.assertAlmostEqual(sum(s['end']-s['start'] for s in samples),10)

    def test_encoder_sends_only_the_planned_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.mp4'
            ffmpeg('-y','-f','lavfi','-i','testsrc2=size=64x48:rate=10:duration=2','-c:v','libx264',source)
            metadata,timeline,_=probe_video(source)
            samples,windows,_=plan_samples(timeline,0,2,5,batch_size=3)
            prepare_samples(source,root/'run',samples,windows,metadata)
            self.assertEqual(probe_video(root/'run/sampled.mp4')[0]['frame_count'],5)
            self.assertEqual([probe_video(root/'run'/window['file'])[0]['frame_count'] for window in windows],[3,2])
            self.assertEqual([window['samples'][0]['source_index'] for window in windows],[samples[0]['source_index'],samples[3]['source_index']])

    def test_caps_are_validated(self):
        for cap in [0,5001,1.5,True]:
            with self.assertRaises(ValueError):plan_samples([(0,1)],0,1,cap)

    def test_local_preparation_creates_one_sample_video_without_sam_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.mp4'
            ffmpeg('-y','-f','lavfi','-i','testsrc2=size=64x48:rate=10:duration=2','-c:v','libx264',source)
            metadata,timeline,_=probe_video(source)
            samples,windows,_=plan_samples(timeline,0,2,5,batch_size=3)
            prepare_samples(source,root/'run',samples,windows,metadata,encode_batches=False)
            self.assertEqual(probe_video(root/'run/sampled.mp4')[0]['frame_count'],5)
            self.assertFalse((root/'run/windows').exists())
