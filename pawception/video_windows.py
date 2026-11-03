"""Plan overlapping windows on a source-video timeline, without wall-clock drift."""

from __future__ import annotations

from fractions import Fraction
import math
from bisect import bisect_left


def plan_windows(duration_seconds: float, window_seconds: float = 10, overlap_seconds: float = 2) -> list[dict]:
    values = (duration_seconds, window_seconds, overlap_seconds)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("Window times must be finite numbers")
    duration, size, overlap = (Fraction(str(v)) for v in values)
    if duration <= 0 or size <= 0 or not 0 <= overlap < size:
        raise ValueError("Require positive duration/window and 0 <= overlap < window")
    result = []
    start = Fraction(0)
    while start < duration:
        end = min(start + size, duration)
        result.append({"index": len(result), "start_seconds": float(start), "end_seconds": float(end),
                       "duration_seconds": float(end - start)})
        if end == duration:
            break  # Do not add a redundant tail wholly inside the previous window.
        start += size - overlap
    return result


def frame_windows(timeline, *, start=0, end=None, window_seconds=3, overlap_seconds=0):
    """Align requested boundaries to actual frame timestamps, including VFR.

    A requested 2s seek may begin at 2.03s. Return that measured source offset,
    rather than letting every subsequent event inherit a timestamp error.
    """
    if not timeline or any(length <= 0 for _, length in timeline):
        raise ValueError("Video needs timestamped frames with positive duration")
    timestamps = [pts for pts, _ in timeline]
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("Video frame timestamps must increase strictly")
    duration = float(max(pts + length for pts, length in timeline))
    end = duration if end is None else end
    if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in (start, end)):
        raise ValueError("Excerpt times must be finite numbers")
    if not 0 <= start < end <= duration:
        raise ValueError("Excerpt must lie within the source video")
    requested = plan_windows(end - start, window_seconds, overlap_seconds)
    result = []
    for window in requested:
        left = start + window["start_seconds"]
        right = start + window["end_seconds"]
        first = bisect_left(timestamps, Fraction(str(left)))
        stop = bisect_left(timestamps, Fraction(str(right)))
        if first == stop:
            raise ValueError("Window is shorter than the source frame spacing")
        actual_start = float(timeline[first][0])
        actual_end = float(timeline[stop - 1][0] + timeline[stop - 1][1])
        if result and actual_start <= result[-1]["start_seconds"]:
            raise ValueError("Window stride is shorter than the source frame spacing")
        result.append({
            "id": f"{len(result):04d}",
            "index": len(result),
            "requested_start_seconds": left,
            "requested_end_seconds": right,
            "start_seconds": actual_start,
            "end_seconds": actual_end,
            "input_duration_seconds": actual_end - actual_start,
            "source_start_frame": first,
            "source_end_frame_exclusive": stop,
        })
    return result
