from fractions import Fraction
import json
from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image

from pawception.sam_evidence import segment_video
from pawception.sam_tracks import TrackWriter
from meta_sam_parser import CompletedOutcome, video_segmentation_format

FRAME = (Path(__file__).parent / 'fixtures/sam_frame.txt').read_text()


def records(text=FRAME):
    parser = video_segmentation_format().create_parser()
    parser.push(text)
    return [r for r in parser.finish(CompletedOutcome()).result.records if r.kind == 'mask']


class StreamingTests(unittest.TestCase):
    def test_split_deltas_publish_a_frame_before_the_stream_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new('RGB', (490,480)).save(root/'frame.jpg')
            writer = TrackWriter(root/'pet', (490,480), [(Fraction(0),Fraction(1,30)), (Fraction(1,30),Fraction(1,30))])
            published = []
            def publish(index, masks):
                writer.add(index, masks)
                published.append(index)
            response = SimpleNamespace(status='completed', output_text=FRAME+FRAME.replace('<0f>','<1f>'), model_dump=lambda **kw: {})
            def events():
                for delta in (FRAME[:100], FRAME[100:], FRAME.replace('<0f>','<1f>')):
                    yield SimpleNamespace(type='response.output_text.delta', delta=delta, model_dump=lambda **kw: {})
                # This runs before the terminal response is delivered to the consumer.
                self.assertEqual(published,[0,1])
                manifest=json.loads((root/'pet/tracking.json').read_text())
                self.assertEqual(manifest['status'],'processing')
                self.assertEqual(len(manifest['frames']),2)
                self.assertTrue((root/'pet/frames/000000.png').is_file())
                yield SimpleNamespace(type='response.completed', response=response, model_dump=lambda **kw: {})
            stream=Mock(); stream.__enter__=Mock(side_effect=events); stream.__exit__=Mock(return_value=False)
            client=Mock(); client.responses.create.return_value=stream
            result=segment_video(client, root/'frame.jpg', 'file-shared', 1, 'gray cat', root/'pet', publish)
            self.assertEqual(result['tracking_frames'],2)
            self.assertIsNotNone(result['first_frame_seconds'])
            self.assertEqual(len(result['segments']),1)
            self.assertNotIn('reasoning',client.responses.create.call_args.kwargs)
            self.assertEqual(client.responses.create.call_args.kwargs['input'][0]['content'][1]['file_id'],'file-shared')

    def test_vfr_and_out_of_order_frames_keep_their_original_timestamps(self):
        with tempfile.TemporaryDirectory() as temp:
            writer=TrackWriter(Path(temp),(490,480),[(Fraction(0),Fraction(1,10)),(Fraction(1,10),Fraction(1,5))])
            writer.add(1,records(FRAME.replace('<0f>','<1f>')))
            writer.add(0,records())
            writer.finish()
            manifest=json.loads((Path(temp)/'tracking.json').read_text())
            self.assertEqual([(r['start'],r['end']) for r in manifest['frames']],[(0,0.1),(0.1,0.3)])
            with Image.open(Path(temp)/'frames/000000.png') as mask:
                self.assertEqual(mask.mode,'RGBA')
                self.assertEqual(mask.getpixel((0,0))[3],0)
            with self.assertRaises(ValueError): writer.add(2,records())

    def test_ambiguous_pets_are_not_silently_selected_and_ids_do_not_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            first=records(); other=records(FRAME.replace('<0f>0','<0f>1'))
            writer=TrackWriter(Path(temp),(490,480),[(Fraction(0),Fraction(1))])
            writer.add(0,first+other); writer.finish()
            self.assertEqual(writer.summary()['status'],'ambiguous')
            self.assertEqual(writer.summary()['frames_received'],0)
        with tempfile.TemporaryDirectory() as temp:
            writer=TrackWriter(Path(temp),(490,480),[(Fraction(0),Fraction(1)),(Fraction(1),Fraction(1))])
            writer.add(0,first); writer.add(1,other); writer.finish('unavailable')
            self.assertEqual(writer.summary()['frames_received'],1)
            self.assertEqual(writer.summary()['status'],'unavailable')

    def test_cancellation_stops_stream_consumption(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); Image.new('RGB',(490,480)).save(root/'frame.jpg')
            cancelled=Event()
            def events():
                yield SimpleNamespace(type='response.output_text.delta',delta=FRAME,model_dump=lambda **kw: {})
                cancelled.set()
                yield SimpleNamespace(type='response.output_text.delta',delta=FRAME,model_dump=lambda **kw: {})
            stream=Mock(); stream.__enter__=Mock(side_effect=events); stream.__exit__=Mock(return_value=False)
            client=Mock(); client.responses.create.return_value=stream
            from concurrent.futures import CancelledError
            with self.assertRaises(CancelledError):
                segment_video(client,root/'frame.jpg','file-id',0,'cat',root/'pet',cancelled=cancelled)
