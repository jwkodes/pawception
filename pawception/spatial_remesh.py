"""Rebuild a saved room mesh from cached depth; no model inference or API calls."""

import argparse
import json
from pathlib import Path
import shutil
import time

import numpy as np

from pawception.spatial_hotspots import DEPTH_EDGE_TOLERANCE, surface_mesh
from pawception.offline_spatial import write_json


def remesh(folder):
    state = json.loads((folder/'state.json').read_text())
    if state['status'] != 'completed':
        raise ValueError('Only a completed 3D analysis can be remeshed')
    started = time.monotonic()
    scene = json.loads((folder/'scene.json').read_text())
    with np.load(folder/'background.npz') as cached:
        mesh = surface_mesh(cached['depth'], cached['colour'], cached['intrinsics'])
    for name in ['scene.json', 'state.json']:
        backup = folder/name.replace('.json', '.before-edge-filter.json')
        if not backup.exists():
            shutil.copyfile(folder/name, backup)
    before = len(scene['mesh']['triangles'])//3
    scene['mesh'] = mesh
    scene['mesh_edge_tolerance'] = DEPTH_EDGE_TOLERANCE
    scene['stats'].update(mesh_vertices=len(mesh['positions'])//3, mesh_triangles=len(mesh['triangles'])//3)
    scene['mesh_rebuild_seconds'] = round(time.monotonic()-started, 2)
    state['stats'] = scene['stats']
    state['mesh_rebuild_seconds'] = scene['mesh_rebuild_seconds']
    write_json(folder/'scene.json', scene)
    write_json(folder/'state.json', state)
    return {'triangles_before':before, 'triangles_after':scene['stats']['mesh_triangles'],
            'edge_tolerance':DEPTH_EDGE_TOLERANCE, 'model_calls':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    print(json.dumps(remesh(args.folder)))
