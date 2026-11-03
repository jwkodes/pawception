"""Manage explicitly requested local 3D jobs in the optional MoGe environment."""

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import RLock
from uuid import uuid4

BUSY = {'queued', 'running', 'stopping'}


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


class SpatialJobs:
    def __init__(self, root):
        self.root, self.lock, self.processes = Path(root), RLock(), {}

    def available(self):
        return (self.root/'.venv-moge/bin/python').is_file() and (self.root/'data/models/moge-2-vits-normal/model.pt').is_file()

    def folder(self, identifier, token=None):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier) or token is not None and not re.fullmatch(r'[a-f0-9]{32}', token):
            raise ValueError('Invalid 3D analysis identifier')
        path = self.root/'data/offline_runs'/identifier/'spatial'
        if token:
            path /= token
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent.is_relative_to(self.root)):
            raise ValueError('Invalid 3D analysis path')
        return path

    def status(self, identifier):
        with self.lock:
            folder = self.folder(identifier)
            pointer = folder/'latest.json'
            if not pointer.is_file():
                return {'status':'idle', 'available':self.available()}
            metadata = json.loads(pointer.read_text())
            directory = self.folder(identifier, metadata['token'])
            state = json.loads((directory/'state.json').read_text())
            process = self.processes.get(metadata['token'])
            alive = process.poll() is None if process else False
            if not process:
                try:
                    os.kill(metadata.get('pid', 0), 0)
                    alive = metadata.get('pid', 0) > 0
                except (ProcessLookupError, PermissionError):
                    pass
            if state['status'] in BUSY and not alive:
                state.update(status='error', stage='3D worker stopped unexpectedly. Saved 2D results are retained.')
                write_json(directory/'state.json', state)
            return {**state, 'token':metadata['token'], 'settings':metadata['settings'], 'available':self.available(),
                    'scene_url':f"/offline-results/{identifier}/spatial/{metadata['token']}/scene.json" if state['status']=='completed' else None}

    def busy(self):
        for path in (self.root/'data/offline_runs').glob('*/spatial/latest.json'):
            if self.status(path.parents[1].name)['status'] in BUSY:
                return True
        return False

    def start(self, job, source, value):
        with self.lock:
            changes = job.get('hotspot_method') == 'changes'
            real = job['provider'] == 'local' if changes else job['provider'] == 'muse'
            if job['status'] != 'completed' or job['kind'] != 'hotspots' or not real:
                raise ValueError('Select a completed SAM or change-detection hotspot analysis first.')
            if changes and any(len(point)<6 for point in job.get('points', [])):
                raise ValueError('This older analysis has no saved foreground masks. Run change detection again first.')
            if not changes and not job.get('sam_frames'):
                raise ValueError('This analysis has no saved SAM masks. Run SAM hotspots first.')
            if not self.available():
                raise ValueError('The local MoGe environment and checkpoint must be installed first. See the README.')
            if self.busy():
                raise ValueError('A 3D analysis is already running. Stop it or wait for completion.')
            size, fov = value.get('voxel_size', .08), value.get('fov')
            if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or not .04 <= size <= .5:
                raise ValueError('Choose a voxel size between 0.04 and 0.5 predicted metres')
            if fov is not None and (isinstance(fov, bool) or not isinstance(fov, (int, float)) or not math.isfinite(fov) or not 20 <= fov <= 140):
                raise ValueError('Choose a horizontal FOV between 20 and 140 degrees, or leave it empty to estimate once')
            settings = {'voxel_size':size, 'fov':fov}
            token = uuid4().hex
            directory = self.folder(job['id'], token)
            directory.mkdir(parents=True)
            write_json(directory/'input.json', {'parent':str(directory.parents[1]), 'source':str(source), 'settings':settings})
            # A matching change-detection analysis already has a median image.
            # Reuse it instead of rebuilding a second background estimate.
            samples = [sample['frame_seconds'] for window in job.get('windows', []) for sample in window.get('samples', [])]
            candidates = []
            for path in (self.root/'data/offline_runs').glob('*/run.json'):
                background = path.parent/'background.png'
                if not background.is_file() or background.is_symlink() or path.parent.is_symlink():
                    continue
                other = json.loads(path.read_text())
                times = [sample['frame_seconds'] for window in other.get('windows', []) for sample in window.get('samples', [])]
                if other.get('status')=='completed' and other.get('hotspot_method')=='changes' and other.get('source_id')==job.get('source_id') and samples and samples==times:
                    candidates.append((path.parent.name==job['id'], other.get('created_at',''), background))
            if candidates:
                shutil.copyfile(max(candidates)[2], directory/'background.png')
            write_json(directory/'state.json', {'status':'queued', 'stage':'Starting local MoGe fast'})
            with (directory/'worker.log').open('w') as log:
                process = subprocess.Popen([str(self.root/'.venv-moge/bin/python'), '-u', '-m', 'pawception.spatial_worker',
                                            str(directory), '--parent-pid', str(os.getpid())],
                                           cwd=self.root, stdout=log, stderr=subprocess.STDOUT)
            self.processes[token] = process
            write_json(directory.parent/'latest.json', {'token':token, 'pid':process.pid, 'settings':settings})
            return self.status(job['id'])

    def stop(self, identifier):
        with self.lock:
            state = self.status(identifier)
            if state['status'] in BUSY:
                directory = self.folder(identifier, state['token'])
                (directory/'stop').touch()
                write_json(directory/'state.json', {**state, 'status':'stopping', 'stage':'Stopping after the current frame'})
            return self.status(identifier)

    def asset(self, identifier, token, name):
        if name not in {'scene.json', 'background.png'}:
            raise ValueError('3D asset not found')
        path = self.folder(identifier, token)/name
        if not path.is_file() or path.is_symlink():
            raise ValueError('3D scene not found')
        return path
