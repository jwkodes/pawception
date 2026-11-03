from pathlib import Path
import json
import shutil
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from pawception.hotspot_changes import background, foreground_location, foreground_mask, analyse_changes, analysis_size
from pawception.hotspot_footprint import footprint, occupied_cells
from pawception.hotspot_sampling import plan_samples, prepare_samples
from pawception.video import ffmpeg, probe_video


def frame(*boxes):
    image=Image.new('L',(160,90),40)
    for box in boxes:
        ImageDraw.Draw(image).rectangle(box,fill=200)
    return image


class ChangeDetectionTests(unittest.TestCase):
    def test_image_size_preserves_aspect_ratio_and_never_enlarges_the_source(self):
        self.assertEqual(analysis_size({'width':854,'height':480},160),(160,90))
        self.assertEqual(analysis_size({'width':854,'height':480},320),(320,180))
        self.assertEqual(analysis_size({'width':480,'height':854},320),(180,320))
        self.assertEqual(analysis_size({'width':64,'height':48},160),(64,48))
        for value in [0,100,1280,True,160.0]:
            with self.assertRaises(ValueError):analysis_size({'width':854,'height':480},value)

    def test_background_uses_all_1000_frames_instead_of_the_old_31_frame_subset(self):
        frames=[Image.new('L',(1,1),40) for _ in range(1000)]
        for index in range(31):frames[round(index*999/30)]=Image.new('L',(1,1),200)
        self.assertEqual(background(frames,max_frames=31).getpixel((0,0)),200)
        self.assertEqual(background(frames).getpixel((0,0)),40)

    def test_moving_subject_is_located_against_median_background(self):
        frames=[frame((x,30,x+15,50)) for x in (5,35,65,95,125)]
        reference=background(frames)
        positions=[foreground_location(image,reference) for image in frames]
        self.assertTrue(all(position is not None for position in positions))
        self.assertLess(positions[0][0],0.15)
        self.assertGreater(positions[-1][0],0.8)
        self.assertAlmostEqual(positions[0][1],40.5/90)

    def test_unchanged_scene_global_lighting_and_ambiguous_regions_are_excluded(self):
        reference=frame()
        self.assertIsNone(foreground_location(reference,reference))
        self.assertIsNone(foreground_location(Image.new('L',(160,90),200),reference))
        self.assertIsNone(foreground_location(frame((10,10,30,30),(100,50,120,70)),reference))

    def test_background_subtraction_keeps_foreground_without_interframe_movement(self):
        reference=frame()
        resting=frame((60,30,90,50))
        self.assertEqual(foreground_location(resting,reference),foreground_location(resting.copy(),reference))

    def test_change_footprint_preserves_shape_instead_of_filling_its_bounding_box(self):
        reference=frame();subject=frame((50,20,65,75),(50,60,120,75))
        mask=foreground_mask(subject,reference)
        self.assertIsNotNone(mask)
        self.assertEqual(mask.getpixel((100,30)),0)
        cells=occupied_cells(footprint(mask),4,3)
        self.assertIn(9,cells);self.assertIn(10,cells)
        self.assertNotIn(2,cells)

    def test_encoded_sample_locations_keep_original_recording_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.mp4'
            ffmpeg('-y','-f','lavfi','-i',"color=c=black:size=160x90:rate=5:duration=2,drawbox=x=10:y=30:w=20:h=20:color=white:t=fill:enable='lt(t,0.6)',drawbox=x=110:y=30:w=20:h=20:color=white:t=fill:enable='gt(t,1.4)'",'-c:v','libx264',source)
            metadata,timeline,_=probe_video(source)
            samples,windows,_=plan_samples(timeline,0,2,10)
            prepare_samples(source,root/'run',samples,windows,metadata)
            background_path=root/'run/background.png'
            updates=[];previews=[]
            points=analyse_changes(root/'run/sampled.mp4',samples,Event(),background_path=background_path,
                                   on_progress=lambda *value:updates.append(value),
                                   on_frame_ready=lambda *value:previews.append(value))
            self.assertEqual(len(previews),10)
            self.assertEqual(previews[-1][0],9)
            self.assertEqual(previews[-1][1],samples[-1])
            self.assertTrue(any(region is None for _,_,region in previews))
            self.assertEqual(sum(region is not None for _,_,region in previews),len(points))
            self.assertIn(('reading_frames',10,10),updates)
            self.assertIn(('building_background',90,90),updates)
            self.assertEqual(updates[-1],('comparing_frames',10,10))
            with Image.open(background_path) as saved:
                self.assertEqual(saved.size,(160,90))
                self.assertEqual(saved.mode,'L')
            self.assertFalse((root/'run/background.partial.png').exists())
            smaller=root/'run/smaller-background.png'
            analyse_changes(root/'run/sampled.mp4',samples,Event(),background_path=smaller,size=(80,45))
            with Image.open(smaller) as saved:self.assertEqual(saved.size,(80,45))
            self.assertGreaterEqual(len(points),4)
            self.assertLess(points[0][2],0.3)
            self.assertGreater(points[-1][2],0.6)
            self.assertEqual(points[-1][1],2)
            self.assertTrue(all(point[0]<=point[4]<point[1] for point in points))
            self.assertTrue(all(len(point)==6 and point[5]['png'] for point in points))
            from pawception.hotspot_rebuild import rebuild
            directory=root/'data/offline_runs'/('a'*32);directory.mkdir(parents=True)
            shutil.copyfile(root/'run/sampled.mp4',directory/'sampled.mp4')
            shutil.copyfile(background_path,directory/'background.png')
            old={'status':'completed','kind':'hotspots','provider':'local','hotspot_method':'changes',
                 'windows':[{'samples':samples}],'points':[point[:5] for point in points]}
            path=directory/'run.json';path.write_text(json.dumps(old))
            with patch('pawception.hotspot_rebuild.background',side_effect=AssertionError('Must reuse saved background')):
                rebuild(root,'a'*32)
            self.assertEqual(json.loads(path.read_text())['points'],points)
            self.assertEqual(json.loads((directory/'run.before-footprints.json').read_text()),old)

    def test_background_build_can_be_cancelled_between_rows(self):
        from concurrent.futures import CancelledError
        cancelled=Event()
        with self.assertRaises(CancelledError):
            background([frame(),frame()],cancelled=cancelled,
                       on_progress=lambda done,total:cancelled.set())
