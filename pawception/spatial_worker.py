"""Optional MoGe worker. Reuses saved hotspot masks; makes no Meta API calls."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

import imageio_ffmpeg
import numpy as np
from PIL import Image
import torch
from moge.model.v2 import MoGeModel

from pawception.spatial_hotspots import DEPTH_EDGE_TOLERANCE, PetVoxels, surface_mesh
from pawception.spatial_masks import sample_frames, saved_mask
from pawception.hotspot_sampling import prepare_samples
from pawception.video import probe_video


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, separators=(',', ':'), allow_nan=False))
    temporary.replace(path)


class Progress:
    def __init__(self, folder, parent):
        self.folder, self.parent, self.started = folder, parent, time.monotonic()
        self.last, self.state = 0, {}

    def check(self):
        if (self.folder/'stop').exists() or (self.parent and os.getppid() != self.parent):
            raise InterruptedError('3D analysis stopped. Existing 2D results are unchanged.')

    def update(self, stage, done=0, total=0, **extra):
        self.check()
        now = time.monotonic()
        changed = self.state.get('stage') != stage
        if changed:
            self.phase = now
        elapsed = now - self.started
        eta = (now-self.phase) / done * (total-done) if done >= 5 and total > done else None
        self.state = dict(status='running', stage=stage, done=done, total=total,
                          elapsed_seconds=round(elapsed, 2), estimated_remaining_seconds=eta, **extra)
        if changed or now-self.last >= .5 or done == total:
            write_json(self.folder/'state.json', self.state)
            self.last = now


def decode(video, width, height, selection=None):
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-i', str(video), '-an']
    if selection is not None:
        command += ['-vf', f'select=eq(n\\,{selection})', '-frames:v', '1', '-fps_mode', 'passthrough']
    command += ['-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            data = process.stdout.read(width*height*3)
            if not data:
                break
            if len(data) != width*height*3:
                raise ValueError('Incomplete cached video frame')
            yield np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3).copy()
        if process.wait() != 0:
            raise ValueError('Could not decode cached sampled video')
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate()


def background_image(video, folder, width, height, count, size, progress):
    """Reuse the saved median, or make the same simple estimate for a SAM run."""
    path = folder/'background.png'
    if path.is_file():
        with Image.open(path) as image:
            return np.asarray(image.convert('RGB')).copy(), 'Reused saved change-detection background'
    chosen = set(np.linspace(0, count-1, min(1000, count)).astype(int).tolist())
    references = []
    for index, rgb in enumerate(decode(video, width, height)):
        progress.update('Estimating background image', index, count)
        if index in chosen:
            references.append(np.asarray(Image.fromarray(rgb).resize(size, Image.Resampling.BILINEAR)))
    if len(references) != len(chosen):
        raise ValueError('Cached video ended while estimating the background')
    stack = np.stack(references)
    del references
    image = np.empty((size[1], size[0], 3), dtype=np.uint8)
    for row in range(0, size[1], 16):
        progress.update('Estimating background image', row, size[1])
        image[row:row+16] = np.median(stack[:, row:row+16], axis=0).astype(np.uint8)
    Image.fromarray(image).save(path)
    return image, f'Temporal median of {len(chosen)} sampled frames'


def build(folder, progress):
    config = json.loads((folder/'input.json').read_text())
    parent, settings = Path(config['parent']), config['settings']
    job = json.loads((parent/'run.json').read_text())
    root = Path(__file__).resolve().parents[1]
    samples = [sample for window in job['windows'] for sample in window['samples']]
    method = job.get('hotspot_method', 'sam')
    frames = sample_frames(job)
    progress.update('Preparing saved samples', 0, len(samples))
    if not (parent/'sampled.mp4').is_file():
        metadata = probe_video(config['source'])[0]
        prepare_samples(Path(config['source']), parent, samples, job['windows'], metadata, encode_batches=False)
    video = parent/'sampled.mp4'
    metadata = probe_video(video)[0]
    width, height = metadata['width'], metadata['height']
    if metadata['frame_count'] != len(samples):
        raise ValueError('Cached video does not match the saved sample plan')
    background_rgb, background_source = background_image(video, folder, width, height, len(samples), (width, height), progress)
    progress.update('Loading MoGe fast')
    device = torch.device('mps' if torch.backends.mps.is_available() else 'cuda' if torch.cuda.is_available() else 'cpu')
    model = MoGeModel.from_pretrained(root/'data/models/moge-2-vits-normal/model.pt').to(device).eval()
    model.enable_pytorch_native_sdpa()

    def infer(rgb, fov=None):
        progress.check()
        tensor = torch.from_numpy(rgb).to(device, dtype=torch.float32).permute(2, 0, 1) / 255
        with torch.inference_mode():
            result = model.infer(tensor, resolution_level=0, use_fp16=device.type != 'cpu', fov_x=fov)
        return result

    fixed_fov = settings['fov']
    if fixed_fov is None:
        indices = np.unique(np.linspace(0, len(samples)-1, min(5, len(samples))).astype(int))
        estimates = []
        for done, index in enumerate(indices):
            progress.update('Estimating one fixed FOV', done, len(indices))
            rgb = next(decode(video, width, height, int(index)))
            intrinsic = infer(rgb)['intrinsics'].float().cpu().numpy()
            estimates.append(float(np.degrees(2*np.arctan(.5/intrinsic[0, 0]))))
        fixed_fov = float(np.median(estimates))
    progress.update('Estimating depth of the background image', 0, 1, fixed_fov=fixed_fov)
    background_result = infer(background_rgb, fixed_fov)
    background_depth = background_result['depth'].float().cpu().numpy()
    background_depth[~background_result['mask'].cpu().numpy()] = 0
    background_intrinsics = background_result['intrinsics'].float().cpu().numpy()
    background_colour = np.asarray(Image.fromarray(background_rgb).resize((background_depth.shape[1], background_depth.shape[0]), Image.Resampling.BILINEAR))
    mesh = surface_mesh(background_depth, background_colour, background_intrinsics)
    voxels = PetVoxels(settings['voxel_size'])
    inference_seconds, accepted = 0., 0
    count = 0
    for index, rgb in enumerate(decode(video, width, height)):
        if index >= len(samples):
            raise ValueError('More cached frames than planned samples')
        progress.update('Estimating depth and accumulating 3D time', index, len(samples), fixed_fov=fixed_fov, device=str(device))
        count += 1
        frame = frames.get(index)
        # Missing/ambiguous detections add no pet time and need no depth call.
        mask = saved_mask(parent, frame, method)
        if mask is None:
            continue
        started = time.monotonic()
        result = infer(rgb, fixed_fov)
        depth = result['depth'].float().cpu().numpy()
        valid = result['mask'].cpu().numpy()
        depth[~valid] = np.nan
        inference_seconds += time.monotonic()-started
        intrinsics = result['intrinsics'].float().cpu().numpy()
        pet = np.asarray(mask.resize((depth.shape[1], depth.shape[0]), Image.Resampling.NEAREST)) > 0
        voxels.add(depth, pet, intrinsics, samples[index])
        accepted += int(pet.any())
    if count != len(samples):
        raise ValueError('Cached video ended before the saved sample plan')
    progress.update('Saving the 3D map', count, len(samples), fixed_fov=fixed_fov, device=str(device))
    scene = {'version':1, 'source_id':job['source_id'], 'range':[job['start_seconds'], job['end_seconds']],
             'fixed_fov':fixed_fov, 'intrinsics':background_intrinsics.tolist(), 'voxel_size':settings['voxel_size'],
             'background_mode':'single_image', 'background_source':background_source,
             'occupancy_mode':'centre',
             'hotspot_method':method,
             'background_resolution':[background_depth.shape[1], background_depth.shape[0]],
             'mesh_edge_tolerance':DEPTH_EDGE_TOLERANCE,
             'pet_resolution':[width, height],
             'mesh':mesh, 'voxels':voxels.export(), 'seen':voxels.seen, 'processed':voxels.processed,
             'stats':{'samples':count, 'pet_samples':accepted, 'background_depth_calls':1,
                      'mesh_vertices':len(mesh['positions'])//3,
                      'mesh_triangles':len(mesh['triangles'])//3, 'occupied_voxels':len(voxels.cells),
                      'seen_seconds':sum(end-start for start, end in voxels.seen),
                      'inference_seconds':round(inference_seconds, 2), 'device':str(device)},
             'notes':['Fixed FOV; no registration or scale alignment.',
                      'Approximate monocular geometry, in predicted metres; scale and depth can drift.',
                      'Only observed surfaces are reconstructed. Hidden space remains unknown.',
                      'The mesh inherits artifacts from the median background, including stationary pets.',
                      'One median masked 3D centre per sample; percentages partition located time.',
                      'Change detection assumes the single moving foreground object is the pet.' if method=='changes' else 'SAM masks identify the pet of interest.']}
    write_json(folder/'scene.json', scene)
    np.savez_compressed(folder/'background.npz', depth=background_depth, colour=background_colour, intrinsics=background_intrinsics)
    return scene


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--parent-pid', type=int, default=0)
    args = parser.parse_args()
    progress = Progress(args.folder, args.parent_pid)
    try:
        scene = build(args.folder, progress)
        write_json(args.folder/'state.json', dict(status='completed', stage='3D map ready',
                   elapsed_seconds=round(time.monotonic()-progress.started, 2), done=scene['stats']['samples'],
                   total=scene['stats']['samples'], estimated_remaining_seconds=0, stats=scene['stats'], fixed_fov=scene['fixed_fov']))
    except Exception as error:
        write_json(args.folder/'state.json', dict(status='stopped' if isinstance(error, InterruptedError) else 'error',
                   stage=str(error), elapsed_seconds=round(time.monotonic()-progress.started, 2)))
        if not isinstance(error, InterruptedError):
            raise


if __name__ == "__main__":
    main()
