"""Frame previews for local foreground detection, including negative frames."""

import base64
from io import BytesIO

from PIL import Image, ImageChops, ImageFilter

FOREGROUND_COLOR = '#f5ad39'


def change_frame(index, sample, region):
    return {'frame_index':index, 'window_id':'changes', 'sample_number':index+1,
            'source_seconds':sample['frame_seconds'], 'region':region}


def change_frames(job):
    if 'change_frames' in job:
        return job['change_frames']
    # Completed older analyses already saved the masks used by their heatmap.
    if job['status']!='completed' or any(len(point)<6 for point in job['points']):
        return {}
    regions={point[0]:point[5] for point in job['points']}
    samples=[sample for window in job['windows'] for sample in window.get('samples',[])]
    job['change_frames']={str(index):change_frame(index,sample,regions.get(sample['start']))
                          for index,sample in enumerate(samples)}
    return job['change_frames']


def change_overlay(region, size):
    mask=Image.new('L',size)
    if region:
        with Image.open(BytesIO(base64.b64decode(region['png'],validate=True))) as crop:
            if crop.size!=(region['box'][2]-region['box'][0],region['box'][3]-region['box'][1]):
                raise ValueError('Preview mask does not match its bounds')
            mask.paste(crop.convert('L'),tuple(region['box'][:2]))
    overlay=Image.new('RGBA',size,FOREGROUND_COLOR)
    outline=ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)),mask)
    overlay.putalpha(ImageChops.lighter(mask.point(lambda value:round(value*0.35)),outline))
    return overlay
