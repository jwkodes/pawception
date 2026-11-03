"""Duration-weighted image-space pet locations, with explicit coverage."""

import math

from pawception.sam_evidence import mask_image
from pawception.hotspot_footprint import footprint, occupied_cells


def heatmap(points, completed, start, end, columns=24, rows=14):
    if not 0 <= start < end or columns < 1 or rows < 1:
        raise ValueError('Choose a valid heatmap range and grid')
    cells = [{'seconds':0.0, 'first_seen':None} for _ in range(columns * rows)]
    seen, position_samples, footprint_samples = 0.0, 0, 0
    previous_end = start
    for point in sorted(points, key=lambda point:point[0]):
        left, right, x, y = point[:4]
        observed_at = point[4] if len(point)>4 else max(start,left)
        if not all(math.isfinite(v) for v in (left, right, x, y)) or not 0 <= left < right or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError('Invalid pet location')
        duration = max(0, min(end, right) - max(start, left))
        if not duration:
            continue
        position_samples += 1
        if max(start,left) < previous_end - 0.000001:
            raise ValueError('Pet location intervals overlap')
        previous_end = min(end,right)
        if len(point)>5:
            indices = occupied_cells(point[5], columns, rows)
            footprint_samples += 1
        else:
            indices = [min(rows-1, int(y*rows))*columns + min(columns-1, int(x*columns))]
        for index in indices:
            cell = cells[index]
            cell['seconds'] += duration
            cell['first_seen'] = observed_at if cell['first_seen'] is None else min(cell['first_seen'], observed_at)
        seen += duration
    # Tracking windows do not overlap; missing/failed windows remain unprocessed.
    processed, previous_end = 0.0, start
    for left,right in sorted(completed):
        duration = max(0, min(end,right)-max(start,left))
        if duration:
            if max(start,left) < previous_end - 0.000001:
                raise ValueError('Processed tracking intervals overlap')
            processed += duration
            previous_end = min(end,right)
    if seen > processed + 0.00001 or processed > end-start + 0.00001:
        raise ValueError('Tracking intervals overlap or exceed processed coverage')
    for cell in cells:
        cell['percentage'] = 100*cell['seconds']/seen if seen else None
    return {'columns':columns, 'rows':rows, 'cells':cells, 'seen_seconds':seen,
            'position_samples':position_samples,
            'occupancy_mode':'footprint' if footprint_samples==position_samples else 'mixed' if footprint_samples else 'centre',
            'processed_seconds':processed, 'not_detected_seconds':max(0, processed-seen),
            'unprocessed_seconds':max(0, end-start-processed), 'start_seconds':start, 'end_seconds':end}


class PositionCollector:
    """Keep masks by frame/object so repeated stream updates never double-count."""
    def __init__(self, size, timeline, offset, end=None, samples=None):
        self.size, self.offset = size, offset
        self.samples = samples
        # Encoded packet durations can be rounded shorter than the display
        # interval. A frame remains visible until the next presentation time.
        stop = float(timeline[-1][0]+timeline[-1][1]) if end is None else end-offset
        starts = [float(pts) for pts,_ in timeline]
        if samples is not None:
            if len(samples) != len(timeline):
                raise ValueError('SAM input does not match its source-frame map')
            self.timeline = [(sample['start']-offset,sample['end']-sample['start']) for sample in samples]
        else:
            self.timeline = [(pts,(starts[index+1] if index+1<len(starts) else stop)-pts)
                             for index,pts in enumerate(starts)]
        if any(length<=0 for _,length in self.timeline):
            raise ValueError('Invalid frame presentation intervals')
        self.frames = {}

    def add(self, index, records):
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.timeline):
            raise ValueError('SAM frame index is outside the uploaded clip')
        frame = self.frames.setdefault(index, {})
        for record in records:
            mask = mask_image(record, self.size)
            box = mask.getbbox()
            if box:
                frame[str(record.object_id)] = ((box[0]+box[2])/(2*self.size[0]),
                                                (box[1]+box[3])/(2*self.size[1]), footprint(mask))
            else:
                frame.pop(str(record.object_id),None)

    def points(self):
        result = []
        for index, matches in sorted(self.frames.items()):
            if len(matches) != 1:
                continue  # Multiple matching pets are not silently combined.
            pts, length = self.timeline[index]
            match = next(iter(matches.values()))
            x, y = match[:2]
            point = [self.offset+float(pts), self.offset+float(pts+length), x, y]
            point.append(self.samples[index]['source_seconds'] if self.samples is not None else self.offset+float(pts))
            if len(match)>2:
                point.append(match[2])
            result.append(point)
        return result

    def ambiguous_frames(self):
        return sum(len(matches)>1 for matches in self.frames.values())
