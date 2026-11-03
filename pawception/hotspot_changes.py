"""Local foreground locations against a temporal-median background."""

from statistics import median_low
import subprocess

import imageio_ffmpeg
from PIL import Image, ImageChops, ImageFilter

from pawception.live_muse import check_cancelled
from pawception.hotspot_footprint import footprint

BACKGROUND_REFERENCE_FRAMES = 1000


def analysis_size(metadata, long_edge=160):
    if isinstance(long_edge,bool) or not isinstance(long_edge,int) or long_edge not in {80,160,320,640}:
        raise ValueError('Choose a change-detection image size of 80, 160, 320 or 640 pixels')
    scale=min(1,long_edge/max(metadata['width'],metadata['height']))
    return tuple(max(1,int(metadata[key]*scale+0.5)) for key in ('width','height'))


def read_frames(video, count, size=(160,90)):
    """Small grayscale frames keep the bounded analysis independent of OpenCV."""
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
        '-i', str(video), '-an', '-vf', f'scale={size[0]}:{size[1]}', '-frames:v', str(count),
        '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], check=True, capture_output=True)
    pixels = size[0]*size[1]
    if len(result.stdout) != count*pixels:
        raise ValueError('Change-detection input does not match the frame plan')
    return [Image.frombytes('L', size, result.stdout[index*pixels:(index+1)*pixels])
            for index in range(count)]


def background(frames, max_frames=BACKGROUND_REFERENCE_FRAMES, *, on_progress=None, cancelled=None):
    # Evenly spaced references limit memory/work and avoid favouring the start.
    count = min(max_frames, len(frames))
    selected = [frames[round(index*(len(frames)-1)/max(1,count-1))].tobytes()
                for index in range(count)]
    width,height=frames[0].size
    pixels=iter(zip(*selected));result=bytearray()
    for row in range(height):
        if cancelled is not None:check_cancelled(cancelled)
        result.extend(median_low(next(pixels)) for _ in range(width))
        if on_progress:on_progress(row+1,height)
    return Image.frombytes('L',frames[0].size,bytes(result))


def foreground_mask(frame, reference, threshold=25, minimum_fraction=0.003):
    """Use the largest connected region; reject near ties and broad scene changes."""
    difference = ImageChops.difference(frame,reference)
    mask = difference.point(lambda value: 255 if value>=threshold else 0).filter(ImageFilter.MedianFilter(3))
    width,height = mask.size
    pixels = bytearray(mask.tobytes())
    if sum(bool(value) for value in pixels) > width*height*0.35:
        return None  # Lighting/camera changes are not interpreted as a pet.
    regions = []
    for seed in range(len(pixels)):
        if not pixels[seed]:
            continue
        pixels[seed] = 0
        stack,region = [seed],[]
        while stack:
            index = stack.pop()
            x,y = index%width,index//width
            region.append(index)
            for neighbour in (index-1 if x else -1, index+1 if x+1<width else -1,
                              index-width if y else -1, index+width if y+1<height else -1):
                if neighbour>=0 and pixels[neighbour]:
                    pixels[neighbour] = 0
                    stack.append(neighbour)
        if len(region) >= width*height*minimum_fraction:
            regions.append(region)
    regions.sort(key=len,reverse=True)
    if not regions or len(regions)>1 and len(regions[1])>=len(regions[0])*0.6:
        return None
    selected = bytearray(width*height)
    for index in regions[0]:selected[index]=255
    return Image.frombytes('L',mask.size,bytes(selected))


def foreground_location(frame, reference, threshold=25, minimum_fraction=0.003):
    mask=foreground_mask(frame,reference,threshold,minimum_fraction)
    if mask is None:return None
    left,top,right,bottom=mask.getbbox()
    return (left+right)/(2*mask.width),(top+bottom)/(2*mask.height)


def analyse_changes(video, samples, cancelled, *, background_path=None, size=(160,90), on_progress=None, reference=None, on_frame_ready=None):
    check_cancelled(cancelled)
    def progress(stage,done,total):
        if on_progress:on_progress(stage,done,total)
    progress('reading_frames',0,len(samples))
    frames = read_frames(video,len(samples),size)
    progress('reading_frames',len(frames),len(samples))
    check_cancelled(cancelled)
    if reference is None:
        progress('building_background',0,size[1])
        reference = background(frames,on_progress=lambda done,total:progress('building_background',done,total),cancelled=cancelled)
    elif reference.size!=size:
        raise ValueError('Cached background does not match the analysis image size')
    check_cancelled(cancelled)
    if background_path is not None:
        temporary = background_path.with_name('background.partial.png')
        reference.save(temporary)
        temporary.replace(background_path)
    points = []
    progress('comparing_frames',0,len(samples))
    for index,(frame,sample) in enumerate(zip(frames,samples)):
        check_cancelled(cancelled)
        mask = foreground_mask(frame,reference)
        region=footprint(mask) if mask is not None else None
        if mask is not None:
            left,top,right,bottom=mask.getbbox()
            points.append([sample['start'],sample['end'],(left+right)/(2*mask.width),
                           (top+bottom)/(2*mask.height),sample['source_seconds'],region])
        if on_frame_ready:on_frame_ready(index,sample,region)
        progress('comparing_frames',index+1,len(samples))
    return points
