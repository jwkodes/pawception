"""A bounded set of time-stratified frames, mapped back to the source video."""

from bisect import bisect_right
import math

from pawception.video import ffmpeg, probe_video

FRAME_RATE = 5
BATCH_SIZE = 250


def plan_samples(timeline, start, end, cap=1000, batch_size=BATCH_SIZE):
    if isinstance(cap,bool) or not isinstance(cap,int) or not 1 <= cap <= 5000:
        raise ValueError('Choose a frame cap from 1 to 5000')
    if isinstance(batch_size,bool) or not isinstance(batch_size,int) or not 1 <= batch_size <= 250:
        raise ValueError('Choose a batch size from 1 to 250')
    if not timeline or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError('Choose a valid sampling range')
    times = [float(pts) for pts,_ in timeline]
    if end > float(timeline[-1][0]+timeline[-1][1])+0.000001:
        raise ValueError('Sampling range exceeds the recording')
    first = max(0,bisect_right(times,start)-1)
    stop = bisect_right(times,end-0.000000001)
    total = stop-first
    if total <= cap:
        samples = [{'source_index':index, 'frame_seconds':times[index], 'source_seconds':max(start,times[index]),
                    'start':max(start,times[index]),
                    'end':min(end,times[index+1] if index+1<len(times) else end)}
                   for index in range(first,stop)]
    else:
        samples = []
        for index in range(cap):
            left = start+(end-start)*index/cap
            right = start+(end-start)*(index+1)/cap
            centre = (left+right)/2
            frame = max(first,min(stop-1,bisect_right(times,centre)-1))
            if samples and samples[-1]['source_index'] == frame:
                samples[-1]['end'] = right  # One held VFR frame can span several bins.
            else:
                samples.append({'source_index':frame,'frame_seconds':times[frame],'source_seconds':centre,'start':left,'end':right})
    windows = []
    for left in range(0,len(samples),batch_size):
        batch = samples[left:left+batch_size]
        windows.append({'id':f'{len(windows):04d}','index':len(windows),
            'start_seconds':batch[0]['start'],'end_seconds':batch[-1]['end'],
            'sample_start_frame':left,'sample_end_frame_exclusive':left+len(batch),
            'input_duration_seconds':len(batch)/FRAME_RATE,'samples':batch})
    summary = {'max_frames':cap,'source_frames':total,'sample_count':len(samples),
               'sampled':len(samples)<total, 'mean_interval_seconds':(end-start)/len(samples),
               'batch_size':batch_size,'requests':len(windows)}
    return samples, windows, summary


def prepare_samples(source, directory, samples, windows, metadata, *, encode_batches=True):
    """Decode once and encode only selected frames; never upload the full source."""
    directory.mkdir(parents=True,exist_ok=True)
    target = directory/'sampled.mp4'
    if not target.exists():
        scale = f"-2:{min(480,metadata['height'])}" if metadata['width']>=metadata['height'] else f"{min(480,metadata['width'])}:-2"
        terms = [f'eq(n,{sample["source_index"]-samples[0]["source_index"]})' for sample in samples]
        while len(terms)>1:
            terms = [f'({terms[index]}+{terms[index+1]})' if index+1<len(terms) else terms[index]
                     for index in range(0,len(terms),2)]
        selection = terms[0]
        temporary = directory/'sampled.partial.mp4'
        ffmpeg('-y','-ss',f'{samples[0]["frame_seconds"]:.12f}','-i',source,'-an','-vf',f"select='{selection}',scale={scale},setpts=N/({FRAME_RATE}*TB)",
               '-frames:v',len(samples),'-r',FRAME_RATE,'-fps_mode','cfr',
               '-c:v','libx264','-crf',23,'-preset','fast','-pix_fmt','yuv420p',
               '-map_metadata','-1','-movflags','+faststart',temporary)
        if probe_video(temporary)[0]['frame_count'] != len(samples):
            raise ValueError('Sampled video does not match the source-frame plan')
        temporary.replace(target)
    if not encode_batches:
        return
    for window in windows:
        folder = directory/'windows'/window['id']
        folder.mkdir(parents=True,exist_ok=True)
        path = folder/'clip.mp4'
        count = len(window['samples'])
        if not path.exists():
            temporary = folder/'clip.partial.mp4'
            ffmpeg('-y','-ss',window['sample_start_frame']/FRAME_RATE,'-i',target,
                   '-an','-frames:v',count,'-vf','setpts=PTS-STARTPTS',
                   '-c:v','libx264','-crf',23,'-preset','fast','-r',FRAME_RATE,
                   '-fps_mode','cfr','-pix_fmt','yuv420p','-movflags','+faststart',temporary)
            if probe_video(temporary)[0]['frame_count'] != count:
                raise ValueError('Tracking input frame count differs from the sample map')
            temporary.replace(path)
        window['file'] = str(path.relative_to(directory))
