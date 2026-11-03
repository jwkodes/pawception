"""Human-reviewed, timestamped ground truth. This module never runs inference."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock

from pawception.evaluation import digest, interval_error, validate_dataset


class LabelStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root/'data/ground_truth/submission_eval.v1.json'
        self.lock, self.verified = RLock(), {}

    def load(self):
        return validate_dataset(json.loads(self.path.read_text()))

    def media(self, identifier):
        clip = next((c for c in self.load()['clips'] if c['id']==identifier), None)
        if clip is None:
            raise ValueError('Clip not found')
        version = clip['media'][0]
        path = self.root/'data'/version['file']
        base = (self.root/'data').resolve()
        if not path.is_file() or not path.resolve().is_relative_to(base) or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(self.root/'data')):
            raise ValueError('Video is unavailable on this computer')
        stat = path.stat()
        fingerprint = (str(path), stat.st_size, stat.st_mtime_ns)
        if self.verified.get(identifier) != fingerprint:
            if digest(path) != version['sha256']:
                raise ValueError('Video differs from the annotation source. Restore the original clip before labeling.')
            self.verified[identifier] = fingerprint
        return path

    def snapshot(self):
        with self.lock:
            dataset = self.load()
            for clip in dataset['clips']:
                clip['url'] = f"/label-media/{clip['id']}.mp4"
                try:
                    self.media(clip['id'])
                    clip['available'] = True
                except ValueError as error:
                    clip.update(available=False, media_error=str(error))
            dataset['confirmed_clips'] = sum(c.get('annotation_status')=='confirmed' and c.get('enabled',True) for c in dataset['clips'])
            dataset['included_clips'] = sum(c.get('enabled',True) for c in dataset['clips'])
            return dataset

    def save_clip(self, identifier, value):
        with self.lock:
            dataset = self.load()
            clip = next((c for c in dataset['clips'] if c['id']==identifier), None)
            if clip is None:
                raise ValueError('Clip not found')
            revision = value.get('revision')
            if type(revision) is not int or revision != clip.get('annotation_revision',0):
                raise ValueError('This clip changed in another tab. Reload the saved labels before editing again.')
            confirm, enabled = value.get('confirm',False), value.get('enabled',True)
            if type(confirm) is not bool or type(enabled) is not bool:
                raise ValueError('Choose whether to confirm and include this clip')
            if confirm and enabled and value.get('reviewed') is not True:
                raise ValueError('Confirm that you reviewed the video before confirming its labels')
            annotations = value.get('annotations')
            if not isinstance(annotations,dict) or set(annotations) != set(dataset['behaviors']):
                raise ValueError('Label every configured event explicitly')
            self.media(identifier)
            normalized = {}
            now = datetime.now(timezone.utc).isoformat()
            for event, supplied in annotations.items():
                if not isinstance(supplied,dict):
                    raise ValueError('Invalid event annotation')
                label, intervals, notes = supplied.get('label'), supplied.get('intervals',[]), supplied.get('basis','')
                if label not in {'observed','not_observed','unassessable','unlabeled'}:
                    raise ValueError('Choose Observed, Not observed, or Cannot tell')
                if not isinstance(notes,str) or len(notes)>2000:
                    raise ValueError('Keep event notes under 2,000 characters')
                if intervals is None:
                    intervals = []
                if not isinstance(intervals,list) or len(intervals)>100:
                    raise ValueError('Use at most 100 intervals per event')
                if any(interval_error(item,clip['duration_seconds']) for item in intervals):
                    raise ValueError('Intervals must satisfy 0 ≤ start < end ≤ video duration')
                intervals = sorted(deepcopy(intervals),key=lambda item:item['start'])
                if any(a['end']>b['start'] for a,b in zip(intervals,intervals[1:])):
                    raise ValueError('Intervals for the same event must not overlap; combine them or adjust the boundaries')
                if label!='observed' and intervals:
                    raise ValueError('Only observed events can have intervals')
                if confirm and enabled:
                    if label=='unlabeled':
                        raise ValueError('Review all events, including explicit negatives; use Cannot tell for ambiguous evidence')
                    if label=='observed' and not intervals:
                        raise ValueError('Every observed event needs at least one time interval')
                    if event=='barking' and label!='unassessable' and clip.get('has_audio') is False:
                        raise ValueError('This clip has no audio track. Mark barking as Cannot tell rather than assuming silence or absence.')
                normalized[event] = {'label':label,
                    'intervals':intervals if label!='observed' or intervals else None,
                    'timing_status':('confirmed' if confirm and enabled else 'proposed') if label=='observed' and intervals else 'unlabeled' if label=='observed' else 'not_applicable',
                    'review_status':'confirmed' if confirm and enabled else 'proposed',
                    'reviewed_by':'User via labeling UI' if confirm else 'User draft via labeling UI',
                    'basis':notes.strip() or ('Manually reviewed against the source video.' if confirm else 'Draft annotation; awaiting confirmation.'),
                    'reviewed_at':now}
            previous = deepcopy(dataset)
            clip.update(annotations=normalized, enabled=enabled,
                        annotation_status='excluded' if not enabled else 'confirmed' if confirm else 'draft',
                        annotation_revision=revision+1, updated_at=now)
            dataset.update(revision=dataset.get('revision',0)+1, updated_at=now)
            validate_dataset(dataset)
            backup = self.path.parent/'revisions'/dataset['dataset_id']/f"{dataset['revision']:06d}.json"
            backup.parent.mkdir(parents=True,exist_ok=True)
            backup.write_text(json.dumps(previous,indent=2)+'\n')
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dataset,indent=2)+'\n')
            temporary.replace(self.path)
            return self.snapshot()
