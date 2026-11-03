from array import array
from pathlib import Path
import math
import tempfile
import unittest
import wave

from pawception.media_inputs import has_audio, media_window
from pawception.video_windows import frame_windows
from pawception.video import ffmpeg, probe_video
from pawception.pipeline_runner import prepare_windows
from pawception.live_webcam import encode_clip


class MediaInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.mp4'
        # Sound starts at 1.5 seconds, so a window starting at .5 must contain
        # one second of silence before the tone. No private recordings needed.
        ffmpeg('-f', 'lavfi', '-i', 'color=c=red:size=160x120:rate=10:duration=4',
               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2.5',
               '-af', 'adelay=1500:all=1', '-c:v', 'libx264', '-c:a', 'aac', '-t', 4, self.source)

    def assert_aligned_audio(self, window):
        self.assertTrue(has_audio(self.root / window['file']))
        audio = media_window(self.root, window, 'audio', True)
        with wave.open(str(self.root / audio['file'])) as stream:
            self.assertEqual((stream.getnchannels(), stream.getframerate(), stream.getsampwidth()), (1, 16000, 2))
            duration = stream.getnframes()/16000
            samples = array('h', stream.readframes(stream.getnframes()))
        self.assertAlmostEqual(duration, 3, delta=.05)
        def rms(start, end):
            part = samples[int(start*16000):int(end*16000)]
            return math.sqrt(sum(value*value for value in part)/len(part))
        self.assertLess(rms(.2, .8), 30)
        self.assertGreater(rms(1.2, 1.8), 500)
        self.assertGreater(rms(2.7, 2.9), 500)
        silent = media_window(self.root, window, 'visual', True)
        self.assertFalse(has_audio(self.root / silent['file']))
        self.assertEqual([pts for pts, _ in probe_video(self.root / silent['file'])[1]],
                         [pts for pts, _ in probe_video(self.root / window['file'])[1]])
        self.assertEqual(media_window(self.root, window, 'both', True)['file'], window['file'])

    def test_playback_windows_preserve_sound_and_source_time(self):
        timeline = probe_video(self.source)[1]
        window = frame_windows(timeline, start=.5, end=3.5, window_seconds=3, overlap_seconds=0)[0]
        prepare_windows(self.source, [window], self.root, include_audio=True)
        self.assert_aligned_audio(window)

    def test_camera_windows_preserve_sound_and_source_time(self):
        target = self.root/'windows/0000/clip.mp4'
        encode_clip(self.source, target, .5, 3.5, include_audio=True)
        self.assert_aligned_audio({'file':'windows/0000/clip.mp4', 'input_duration_seconds':3})

    def test_silent_source_remains_unavailable_instead_of_synthetic_silence(self):
        silent = self.root/'silent.mp4'
        ffmpeg('-i', self.source, '-map', '0:v:0', '-an', '-c:v', 'copy', silent)
        window = frame_windows(probe_video(silent)[1], window_seconds=3)[0]
        prepare_windows(silent, [window], self.root, include_audio=True)
        self.assertFalse(has_audio(self.root / window['file']))

    def test_delayed_audio_track_keeps_its_offset(self):
        source = self.root/'delayed.mp4'
        ffmpeg('-f','lavfi','-i','color=c=red:size=160x120:rate=10:duration=4',
               '-itsoffset',1.5,'-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2.5',
               '-c:v','libx264','-c:a','aac','-t',4,source)
        window = frame_windows(probe_video(source)[1], start=.5, end=3.5, window_seconds=3)[0]
        prepare_windows(source,[window],self.root,include_audio=True)
        self.assert_aligned_audio(window)
