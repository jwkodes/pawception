from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image,ImageDraw

from pawception.offline_sam import FrameOverlays,frame_progress,estimate_remaining


class OfflineSamTests(unittest.TestCase):
    def test_completion_estimate_uses_observed_throughput_and_waits_for_enough_data(self):
        self.assertEqual(estimate_remaining(20,100,5,10),20)
        self.assertIsNone(estimate_remaining(0,100,5,10))
        self.assertIsNone(estimate_remaining(2,100,5,10))
        self.assertIsNone(estimate_remaining(100,100,5,10))  # Cleanup duration is unknown.
        self.assertIsNone(estimate_remaining(20,100,None,10))

    def test_progress_counts_frames_including_completed_batches_with_no_matches(self):
        job={'status':'running','stage':'receiving','windows':[
            {'status':'completed','samples':[{}]*5},
            {'status':'running','samples':[{}]*3,'frames_received':2}]}
        result=frame_progress(job)
        self.assertEqual((result['received'],result['total']),(7,8))
        self.assertEqual(result['completed_batches'],1)
        job['status']='stopped';job['windows'][1]['status']='cancelled'
        self.assertEqual(frame_progress(job)['received'],7)
        self.assertEqual(frame_progress(job)['stage'],'stopped')

    def test_frame_updates_replace_one_object_without_erasing_other_objects(self):
        def mask(left):
            image=Image.new('L',(20,20));ImageDraw.Draw(image).rectangle((left,4,left+3,7),fill=255)
            return image
        with tempfile.TemporaryDirectory() as temporary:
            writer=FrameOverlays(Path(temporary),(20,20))
            first,second=SimpleNamespace(object_id='one'),SimpleNamespace(object_id='two')
            with patch('pawception.offline_sam.mask_image',side_effect=[mask(2),mask(12),mask(5)]):
                writer.add(0,[first,second]);result=writer.add(0,[first])
            self.assertEqual(len(result['matches']),2)
            self.assertEqual(result['revision'],2)
            with Image.open(Path(temporary)/'frames/000000.png') as image:
                self.assertEqual(image.mode,'RGBA')
                self.assertEqual(image.getpixel((3,5))[3],0)
                self.assertGreater(image.getpixel((6,5))[3],0)
                self.assertGreater(image.getpixel((13,5))[3],0)
