"""Frame-specific object locations returned by Muse, separate from SAM masks."""

import math

FIELDS = ('evidence_timestamp', 'pet_bbox', 'object_bbox')


def schema_fields():
    box = {'type':['array','null'], 'items':{'type':'number','minimum':0,'maximum':1000},
           'minItems':4, 'maxItems':4}
    return {'evidence_timestamp':{'type':['number','null'],'minimum':0},
            'pet_bbox':box, 'object_bbox':box}


def validate_boxes(answer, duration, *, audio_only=False):
    timestamp = answer['evidence_timestamp']
    boxes = [answer[field] for field in ('pet_bbox','object_bbox')]
    if answer.get('detected') is False or audio_only:
        if timestamp is not None or any(box is not None for box in boxes):
            raise ValueError('Undetected and audio-only events need null evidence timestamp and boxes')
        return
    if timestamp is not None:
        if (type(timestamp) not in (int,float) or not math.isfinite(timestamp)
                or not 0 <= timestamp < duration
                or not any(part['start'] <= timestamp < part['end'] for part in answer['intervals'])):
            raise ValueError('Evidence timestamp must lie inside a detected interval')
    for box in boxes:
        if box is None:
            continue
        if (not isinstance(box,list) or len(box) != 4
                or any(type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 1000 for value in box)
                or not (box[0] < box[2] and box[1] < box[3])):
            raise ValueError('Evidence boxes need [x_min,y_min,x_max,y_max] on a 0–1000 grid')
        if timestamp is None:
            raise ValueError('Evidence boxes need an evidence timestamp')


PROMPT = """
For every event and hazard, also return evidence_timestamp, pet_bbox, and
object_bbox. Choose ONE representative frame inside that detection's intervals;
evidence_timestamp is its playback time in seconds relative to this attachment.
Locate the pet involved in pet_bbox and the relevant object or source of danger
in object_bbox, both in THAT SAME FRAME. Boxes are [x_min, y_min, x_max, y_max]
on a normalized 0–1000 grid: origin at the top-left, x increasing rightward,
y increasing downward. Use tight boxes around the visible targets, with
x_min < x_max and y_min < y_max. These are object locations, not an outline of
the entire event or its motion path. Identify targets from the footage; no
specific pet, object, color, or position is assumed in advance.
Use null for each target that cannot be visually located. Do not invent a pet
for a household hazard. Audio-only or off-screen detections need null boxes.
For audio-only events, or detected=false, all three fields must be null. When
neither target is visible, evidence_timestamp may also be null. Never return
boxes for different frames under the same evidence_timestamp.
"""


def render_evidence(video, answer, directory):
    """Inspect predicted boxes on the corresponding decoded video frame."""
    from bisect import bisect_left
    from PIL import Image, ImageDraw
    from pawception.video import ffmpeg, probe_video
    from pawception.sam_evidence import crop_bounds

    timestamp = answer.get('evidence_timestamp')
    if timestamp is None or not any(answer.get(key) is not None for key in ('pet_bbox','object_bbox')):
        return None
    _, timeline, _ = probe_video(video)
    index = min(bisect_left([float(pts) for pts, _ in timeline], timestamp), len(timeline)-1)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory/'frame.jpg'
    ffmpeg('-y','-i',video,'-vf',f'select=eq(n\\,{index})','-frames:v',1,source)
    with Image.open(source) as image:
        original = image.convert('RGB')
    overlay = original.copy()
    draw = ImageDraw.Draw(overlay)
    boxes = []
    for field, label, color in [('pet_bbox','Pet','#36df85'),('object_bbox','Object','#ffbc3b')]:
        box = answer.get(field)
        if box is None:
            continue
        left, top = math.floor(box[0]*original.width/1000), math.floor(box[1]*original.height/1000)
        right, bottom = math.ceil(box[2]*original.width/1000), math.ceil(box[3]*original.height/1000)
        bounds = (left,top,right,bottom)
        boxes.append(bounds)
        draw.rectangle(bounds,outline=color,width=3)
        draw.text((left+4,max(0,top-16)),label,fill=color,stroke_width=1,stroke_fill='#16251b')
    overlay.save(directory/'boxes.jpg',quality=95)
    crop = crop_bounds(original.size,boxes)
    original.crop(crop).save(directory/'crop.jpg',quality=95)
    return {'requested_seconds':timestamp,'frame_seconds':float(timeline[index][0]),'frame_index':index,
            'boxes':str((directory/'boxes.jpg').resolve()),'crop':str((directory/'crop.jpg').resolve())}
