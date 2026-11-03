"""Saved SAM frame overlays and progress over the actual uploaded samples."""

from hashlib import sha256
import math

from PIL import Image, ImageChops, ImageFilter

from pawception.sam_evidence import mask_image

PALETTE = [(65,180,120),(245,173,57),(150,110,220),(65,160,220)]


def frame_count(window):
    if window.get('samples') is not None:
        return len(window['samples'])
    return window.get('frame_count',window.get('source_end_frame_exclusive',0)-window.get('source_start_frame',0))


def frame_progress(job):
    windows = job['windows']
    total = sum(frame_count(window) for window in windows)
    received = sum(frame_count(window) if window['status']=='completed' else
                   min(frame_count(window),window.get('frames_received',0)) for window in windows)
    stage=job.get('stage','preparing') if job['status'] in {'running','stopping'} else job['status']
    return {'received':received,'total':total,'stage':stage,
            'completed_batches':sum(window['status']=='completed' for window in windows),
            'total_batches':len(windows)}


def estimate_remaining(done,total,elapsed,minimum=1):
    if not total or done<min(minimum,total) or done>=total or elapsed is None or not math.isfinite(elapsed) or elapsed<=0:
        return None
    return elapsed*(total-done)/done


class FrameOverlays:
    """Keep per-object masks on disk so updates can replace an object's old mask."""
    def __init__(self, directory, size):
        self.directory,self.size = directory,size
        (directory/'masks').mkdir(parents=True,exist_ok=True)
        (directory/'frames').mkdir(parents=True,exist_ok=True)
        self.objects,self.colors,self.revision = {},{},0

    def add(self, index, records):
        objects = self.objects.setdefault(index,{})
        for record in records:
            identifier = str(record.object_id)
            mask = mask_image(record,self.size)
            self.colors.setdefault(identifier,PALETTE[len(self.colors)%len(PALETTE)])
            path = self.directory/'masks'/f'{index:06d}-{sha256(identifier.encode()).hexdigest()[:16]}.png'
            mask.save(path)
            objects[identifier] = path
        overlay = Image.new('RGBA',self.size)
        matches = []
        for identifier,path in objects.items():
            with Image.open(path) as saved:
                mask = saved.convert('L')
            if not mask.getbbox():
                continue
            color = self.colors[identifier]
            layer = Image.new('RGBA',self.size,(*color,0))
            outline = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)),mask)
            layer.putalpha(ImageChops.lighter(mask.point(lambda value:round(value*0.35)),outline))
            overlay = Image.alpha_composite(overlay,layer)
            matches.append({'id':identifier,'color':'#'+''.join(f'{value:02x}' for value in color)})
        target = self.directory/'frames'/f'{index:06d}.png'
        temporary = target.with_suffix('.partial.png')
        overlay.save(temporary);temporary.replace(target)
        self.revision += 1
        return {'frame_index':index,'revision':self.revision,'matches':matches}
