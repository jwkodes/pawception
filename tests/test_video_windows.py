import math
import unittest
from fractions import Fraction

from pawception.video_windows import plan_windows, frame_windows


class VideoWindowTests(unittest.TestCase):
    def test_pipeline_default_windows_do_not_share_frames(self):
        timeline = [(Fraction(i, 25), Fraction(1, 25)) for i in range(1000)]
        windows = frame_windows(timeline)
        self.assertEqual(len(windows), 14)
        for left, right in zip(windows, windows[1:]):
            self.assertEqual(left["source_end_frame_exclusive"], right["source_start_frame"])
        self.assertEqual(windows[-1]["end_seconds"], 40)

    def test_pipeline_supports_three_seconds_with_one_second_overlap(self):
        timeline = [(Fraction(i, 25), Fraction(1, 25)) for i in range(1000)]
        windows = frame_windows(timeline, overlap_seconds=1)
        self.assertEqual(len(windows), 20)
        self.assertEqual(windows[0]["end_seconds"], 3)
        self.assertEqual(windows[1]["start_seconds"], 2)
        self.assertEqual(windows[-1]["end_seconds"], 40)

    def test_frame_alignment_uses_actual_pts_instead_of_requested_seek(self):
        step = Fraction(1000, 13794)
        windows = frame_windows([(i * step, step) for i in range(140)], end=7, overlap_seconds=1)
        self.assertNotEqual(windows[1]["start_seconds"], 2)
        self.assertAlmostEqual(windows[1]["start_seconds"], float(28 * step))
        self.assertEqual(windows[1]["source_start_frame"], 28)

    def test_subframe_stride_cannot_create_duplicate_paid_inputs(self):
        timeline = [(Fraction(i, 10), Fraction(1, 10)) for i in range(10)]
        with self.assertRaisesRegex(ValueError, "stride"):
            frame_windows(timeline, window_seconds=0.3, overlap_seconds=0.29)

    def test_long_fixture_covers_tail_with_overlap(self):
        windows = plan_windows(293.92)
        self.assertEqual(len(windows), 37)
        self.assertEqual(windows[0]["start_seconds"], 0)
        self.assertEqual(windows[-1]["end_seconds"], 293.92)
        self.assertEqual(windows[-1]["duration_seconds"], 5.92)
        for left, right in zip(windows, windows[1:]):
            self.assertAlmostEqual(left["end_seconds"] - right["start_seconds"], 2)

    def test_no_redundant_tail_or_empty_window(self):
        self.assertEqual(len(plan_windows(18)), 2)
        self.assertEqual(len(plan_windows(10)), 1)
        self.assertEqual(plan_windows(3)[0]["end_seconds"], 3)
        self.assertEqual([w["start_seconds"] for w in plan_windows(25, 10, 0)], [0, 10, 20])

    def test_fractional_steps_do_not_drift(self):
        windows = plan_windows(1, 0.3, 0.1)
        self.assertEqual([w["start_seconds"] for w in windows], [0, 0.2, 0.4, 0.6, 0.8])
        self.assertEqual(windows[-1]["end_seconds"], 1)

    def test_invalid_window_settings(self):
        for args in ((0, 10, 2), (10, 0, 0), (10, 5, 5), (10, 5, -1), (math.nan, 5, 1), (10, math.inf, 1), (True, 10, 2)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan_windows(*args)


if __name__ == "__main__":
    unittest.main()
