from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from pawception.hotspot_footprint import footprint
from pawception.spatial_masks import sample_frames, saved_mask


class SpatialMaskTests(unittest.TestCase):
    def test_completed_change_results_restore_masks_and_source_sample_indices(self):
        image = Image.new('L', (8, 4))
        image.paste(255, (2, 1, 4, 3))
        job = {'status':'completed', 'hotspot_method':'changes',
               'points':[[10, 12, .4, .5, 11., footprint(image)]],
               'windows':[{'samples':[{'start':10, 'end':12, 'frame_seconds':10.9},
                                      {'start':12, 'end':14, 'frame_seconds':12.9}]}]}
        frames = sample_frames(job)
        self.assertEqual(frames[0]['source_seconds'], 10.9)
        self.assertEqual(saved_mask(Path('.'), frames[0], 'changes').tobytes(), image.tobytes())
        self.assertIsNone(saved_mask(Path('.'), frames[1], 'changes'))
        self.assertIsNone(saved_mask(Path('.'), None, 'changes'))

    def test_ambiguous_sam_instances_are_not_combined(self):
        self.assertIsNone(saved_mask(Path('.'), {'matches':[{'id':'1'}, {'id':'2'}]}, 'sam'))

    def test_sam_uses_the_existing_object_mask(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            masks = folder/'windows/0000/sam/masks'
            masks.mkdir(parents=True)
            image = Image.new('L', (5, 3), 255)
            image.save(masks/f"000002-{sha256(b'cat').hexdigest()[:16]}.png")
            frame = {'matches':[{'id':'cat'}], 'window_id':'0000', 'frame_index':2, 'sample_number':3}
            self.assertEqual(saved_mask(folder, frame, 'sam').tobytes(), image.tobytes())
            self.assertEqual(sample_frames({'sam_frames':{'0000:2':frame}}), {2:frame})


if __name__ == '__main__':
    unittest.main()
