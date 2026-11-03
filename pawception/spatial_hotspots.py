"""Single-image background meshing and time-weighted 3D pet occupancy.

NumPy is confined to the optional MoGe worker environment.
"""

import numpy as np


def backproject(depth, intrinsics):
    height, width = depth.shape
    y, x = np.mgrid[:height, :width]
    # MoGe intrinsics use normalized image coordinates, with pixel centres.
    return np.stack((((x + .5) / width - intrinsics[0, 2]) / intrinsics[0, 0] * depth,
                     ((y + .5) / height - intrinsics[1, 2]) / intrinsics[1, 1] * depth,
                     depth), axis=-1).astype(np.float32)


DEPTH_EDGE_TOLERANCE = .04


def depth_surface_mask(depth, tolerance=DEPTH_EDGE_TOLERANCE):
    """MoGe-style edge removal: reject a pixel if its 3×3 depth range exceeds 4%."""
    valid = np.isfinite(depth) & (depth > 0)
    safe = np.where(valid, depth, 0)
    padded = np.pad(safe, 1, mode='edge')
    low, high = np.full(depth.shape, np.inf), np.full(depth.shape, -np.inf)
    height, width = depth.shape
    for row in range(3):
        for column in range(3):
            neighbour = padded[row:row+height, column:column+width]
            np.minimum(low, neighbour, out=low)
            np.maximum(high, neighbour, out=high)
    return valid & (high-low <= tolerance*safe)


def surface_mesh(depth, rgb, intrinsics):
    """Leave gaps at occlusion boundaries instead of stretching triangles across them."""
    mask = depth_surface_mask(depth)
    points = backproject(np.where(np.isfinite(depth) & (depth > 0), depth, 0), intrinsics)
    height, width = depth.shape
    grid = np.arange(height * width).reshape(height, width)
    a, b, c, d = grid[:-1, :-1], grid[:-1, 1:], grid[1:, :-1], grid[1:, 1:]
    triangles = np.concatenate((np.stack((a, b, c), -1).reshape(-1, 3),
                                np.stack((b, d, c), -1).reshape(-1, 3)))
    triangles = triangles[mask.ravel()[triangles].all(axis=1)]
    used = np.unique(triangles)
    remap = np.full(height * width, -1, dtype=np.int32)
    remap[used] = np.arange(len(used))
    return {'positions':points.reshape(-1, 3)[used].round(5).ravel().tolist(),
            'colours':rgb.reshape(-1, 3)[used].ravel().tolist(),
            'triangles':remap[triangles].ravel().tolist()}


def append_interval(intervals, start, end):
    if intervals and start <= intervals[-1][1] + 1e-6:
        intervals[-1][1] = max(end, intervals[-1][1])
    else:
        intervals.append([start, end])


class PetVoxels:
    """One robust centre per sample: voxel percentages partition located time."""
    def __init__(self, size=.08):
        self.size, self.cells, self.seen, self.processed = size, {}, [], []

    def add(self, depth, pet, intrinsics, sample):
        start, end = sample['start'], sample['end']
        append_interval(self.processed, start, end)
        valid = pet & np.isfinite(depth) & (depth > 0) & (depth < 50)
        if not valid.any():
            return
        append_interval(self.seen, start, end)
        centre = np.median(backproject(depth, intrinsics)[valid], axis=0)
        key = tuple(np.floor(centre / self.size).astype(np.int32))
        cell = self.cells.setdefault(key, {'intervals':[], 'first_seen':sample['frame_seconds']})
        append_interval(cell['intervals'], start, end)

    def export(self):
        return [{'key':list(map(int, key)), **value} for key, value in sorted(self.cells.items())]
