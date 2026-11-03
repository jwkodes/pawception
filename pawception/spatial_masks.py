"""Read saved SAM or change-detection masks for the shared 3D pipeline."""

import base64
from hashlib import sha256
from io import BytesIO

from PIL import Image

from pawception.hotspot_previews import change_frames


def sample_frames(job):
    frames = change_frames(job) if job.get('hotspot_method') == 'changes' else job.get('sam_frames', {})
    return {frame['sample_number']-1:frame for frame in frames.values()}


def saved_mask(folder, frame, method):
    if frame is None:
        return None
    if method == 'changes':
        region = frame.get('region')
        if not region:
            return None
        mask = Image.new('L', tuple(region['size']))
        left, top, right, bottom = region['box']
        with Image.open(BytesIO(base64.b64decode(region['png'], validate=True))) as crop:
            if crop.size != (right-left, bottom-top):
                raise ValueError('Saved foreground mask does not match its bounds')
            mask.paste(crop.convert('L'), (left, top))
        return mask
    matches = frame.get('matches', [])
    if len(matches) != 1:
        return None
    key = sha256(str(matches[0]['id']).encode()).hexdigest()[:16]
    path = folder/'windows'/frame['window_id']/'sam/masks'/f"{frame['frame_index']:06d}-{key}.png"
    if not path.is_file() or path.is_symlink():
        raise ValueError('A saved SAM mask is missing. Run SAM hotspots again before building a 3D map.')
    with Image.open(path) as image:
        return image.convert('L')
