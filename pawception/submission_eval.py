"""Score product-pipeline observations against frozen human annotations."""

from copy import deepcopy
from pathlib import Path

from pawception.evaluation import match_event_groups, mean, score_case, summarize, union
from pawception.pipeline import merge_detections


def metrics(cases):
    result = summarize(cases)
    result['mean_positive_temporal_iou'] = mean([case['timing']['iou'] for case in cases
        if case['scored'] and case['expected']=='observed' and case['timing'] is not None
        and case['timing']['iou'] is not None])
    return result


def clip_predictions(dataset, records):
    predictions = {}
    for event in dataset['behaviors']:
        intervals, evidence, missing = [], [], False
        for window, record in records:
            output = record.get('output') or {}
            if dataset['behavior_types'][event] == 'safety_watch':
                hazards = output.get('hazards')
                if not isinstance(hazards,list):
                    missing = True
                    continue
                parts = [part for hazard in hazards for part in hazard['intervals']]
                evidence.extend(hazard.get('evidence','') for hazard in hazards)
            else:
                answer = output.get(event)
                if not isinstance(answer,dict):
                    missing = True
                    continue
                parts = answer['intervals']
                evidence.append(answer.get('evidence',''))
            intervals.extend({'start':window['start_seconds']+part['start'],
                              'end':window['start_seconds']+part['end']} for part in parts)
        intervals = union(intervals)
        predictions[event] = {'assessment':'observed' if intervals else 'not_observed',
                              'intervals':intervals, 'evidence':evidence,
                              'unavailable':missing or not records}
    return predictions


def build_report(dataset, run):
    cases, clips, timings = [], [], []
    gap = run['config'].get('merge_gap_seconds',1)
    by_id = {clip['clip_id']:clip for clip in run['clips']}
    for clip in dataset['clips']:
        if not clip.get('enabled',True):
            continue
        saved = by_id.get(clip['id'], {})
        records = [(item['window'],item['record']) for item in saved.get('windows',[])]
        predictions = clip_predictions(dataset,records)
        for event, annotation in clip['annotations'].items():
            prediction = predictions[event]
            case = score_case(annotation,prediction,clip['duration_seconds'],
                              unavailable=prediction['unavailable'],
                              iou_threshold=dataset['scoring']['interval_iou_threshold'])
            case.update(clip_id=clip['id'], behavior=event,
                        category=dataset['behavior_types'][event], evidence=prediction['evidence'])
            case['raw_timing'] = deepcopy(case['timing'])
            episodes = merge_detections([{'event_id':event,'window_id':'evaluation',
                         'detected':prediction['assessment']=='observed','intervals':prediction['intervals']}],gap)
            case['predicted_episodes'] = [{'start':episode['start'],'end':episode['end'],
                'intervals':episode['intervals'],'detected_seconds':episode['detected_seconds']} for episode in episodes]
            if case['timing'] is not None:
                matched = match_event_groups([[part] for part in annotation['intervals']],
                    [episode['intervals'] for episode in episodes],dataset['scoring']['interval_iou_threshold'])
                case['timing'].update(matched)
            cases.append(case)
        clip_cases = [case for case in cases if case['clip_id']==clip['id']]
        clips.append({'clip_id':clip['id'],'title':clip['title'],'status':saved.get('status','not_run'),
                      'metrics':metrics(clip_cases),'predictions':predictions,
                      'total_seconds':saved.get('total_seconds')})
        for window, record in records:
            published = [value for value in [*record.get('event_finished_after_run_seconds',{}).values(),
                         record.get('safety_finished_after_run_seconds')] if isinstance(value,(int,float))]
            timings.append({'clip_id':clip['id'],'window_id':window['id'],
                'comparable_to_single_window_run':not saved.get('mixed_inference_runs',False),
                'source_duration_seconds':window['input_duration_seconds'],
                'ready_seconds':max(published) if published else record.get('finished_after_run_seconds'),
                'safety_ready_seconds':record.get('safety_finished_after_run_seconds'),
                'event_ready_seconds':record.get('event_finished_after_run_seconds',{}),
                'cleanup_complete_seconds':record.get('cleanup_finished_after_run_seconds',record.get('finished_after_run_seconds')),
                'checks':[{key:check.get(key) for key in ['model','media_kind','status','upload_seconds',
                          'inference_seconds','result_ready_seconds','cleanup_seconds']} for check in record.get('checks',[])]})
    summary = metrics(cases)
    summary['false_positive_rate'] = summary['false_positives']/summary['negative_cases'] if summary['negative_cases'] else None
    eligible = [case for case in cases if case['expected'] in {'observed','not_observed'} and case['review_status']=='confirmed']
    summary['eligible_cases'] = len(eligible)
    summary['processing_coverage'] = summary['scored_cases']/len(eligible) if eligible else None
    raw_cases = [{**case,'timing':case['raw_timing']} for case in cases]
    return {'run_id':run['id'], 'status':run['status'], 'dataset_id':dataset['dataset_id'],
            'dataset_version':dataset['version'],'dataset_revision':dataset['revision'],
            'dataset_sha256':run['dataset_sha256'],'config':run['config'],
            'summary':summary,'custom':metrics([c for c in cases if c['category']=='custom']),
            'raw_interval_metrics':metrics(raw_cases),
            'postprocessing':{'merge_gap_seconds':gap,'gap_source':'saved run configuration' if 'merge_gap_seconds' in run['config'] else 'application default (not recorded by the original collector)',
                'episode_iou':'IoU of actual detected pieces within each merged episode; bridged gaps do not add detected time.',
                'ground_truth':'Original manually labeled episodes are unchanged.'},
            'safety_watch':metrics([c for c in cases if c['category']=='safety_watch']),
            'per_event':{event:metrics([c for c in cases if c['behavior']==event]) for event in dataset['behaviors']},
            'clips':clips,'cases':cases,'timings':timings,'revisions':run.get('revisions',[]),
            'latency':{'mean_window_ready_seconds':mean([t['ready_seconds'] for t in timings if t['ready_seconds'] is not None and t['comparable_to_single_window_run']]),
                       'note':'Processing from an already-available window, including preparation; no live capture wait. Cleanup is separate. Clips combining checks from separate runs are excluded from the latency mean.'},
            'limitations':['Small development set, not held-out evidence or a broad safety validation.',
                ('Revised development-set results: exclusions and prompt changes after initial results are listed in Revisions. No repeatability estimate.'
                 if run.get('revisions') else 'One pass; no repeatability estimate. No prompts were changed after seeing results.'),
                'Clip/event accuracy and false-positive counts are not alert-level false-alarm rates.',
                'Safety Watch is scored as any visible danger, not hazard kind or identity accuracy.',
                'Unavailable checks are excluded from classification accuracy and reported in processing coverage.',
                'Only the original labeled source-time intervals are used; long-term absence and hotspot accuracy are not evaluated.']}


def markdown_report(report, dataset):
    percent = lambda value:'n/a' if value is None else f'{100*value:.1f}%'
    summary = report['summary']
    positive_iou = summary['mean_positive_temporal_iou']
    positive_iou_text = f'{positive_iou:.3f}' if positive_iou is not None else 'n/a'
    lines = ['# Pawception: behavior and safety detection evaluation','',
        f"Run `{report['run_id']}` · Dataset revision {report['dataset_revision']} · Status {report['status']}",'',
        f"This report compares Pawception's event detections with human-reviewed behavior and hazard intervals across {len(report['clips'])} short videos. Results describe a small development set; they are not an estimate of performance on unseen household footage.",'',
        '## Results','',
        f"- Accuracy: {percent(summary['accuracy'])} across {summary['scored_cases']}/{summary['eligible_cases']} eligible clip/event decisions.",
        f"- Positive detections: {summary['true_positives']}/{summary['positive_cases']} ({percent(summary['positive_recall'])} recall).",
        f"- False-positive decisions: {summary['false_positives']}/{summary['negative_cases']} reviewed negatives.",
        f"- Mean temporal IoU on labeled positives (misses count as zero): {positive_iou_text}.",
        f"- Processing coverage: {percent(summary['processing_coverage'])}.",'',
        '## Method','',
        f"Visual events and Safety Watch use `{report['config'].get('visual_model','unspecified')}`; audio events use `{report['config'].get('audio_model','unspecified')}`. Model responses provide event decisions and time intervals; the evaluation measures both whether events occur and how closely their predicted durations match the annotations.",'',
        'Clip/event accuracy measures the presence or absence of each event in a video. Temporal intersection over union (IoU) measures the overlap between predicted and labeled time divided by their combined duration. Episode precision and recall measure one-to-one matches between predicted and labeled episodes. A correct presence decision does not imply exact time boundaries. Unassessable annotations are excluded from scoring.','',
        f"Episode matching applies the product's {report['postprocessing']['merge_gap_seconds']:g}s gap threshold before one-to-one matching at temporal IoU ≥ {dataset['scoring']['interval_iou_threshold']:g}. Gaps are not counted as detected time. Scoring does not alter labeled or predicted intervals.",'',
        '| Episode scoring | Precision | Recall | TP / FP / FN |',
        '| --- | --- | --- | --- |']
    for name,metrics in [('After pipeline gap merging',summary),('Raw intervals (diagnostic)',report['raw_interval_metrics'])]:
        timing=metrics['timing']
        lines.append(f"| {name} | {percent(timing['event_precision'])} | {percent(timing['event_recall'])} | {timing['event_true_positives']} / {timing['event_false_positives']} / {timing['event_false_negatives']} |")
    lines += ['',
        '| Event | Correct / scored | Positive hits | False positives | Positive mean IoU |',
        '| --- | --- | --- | --- | --- |']
    for event,metrics in report['per_event'].items():
        correct=metrics['true_positives']+metrics['true_negatives']
        iou=metrics['mean_positive_temporal_iou']
        lines.append(f"| {dataset['behavior_titles'][event]} | {correct}/{metrics['scored_cases']} | {metrics['true_positives']}/{metrics['positive_cases']} | {metrics['false_positives']} | {f'{iou:.3f}' if iou is not None else 'n/a'} |")
    if report.get('revisions'):
        lines += ['', '## Evaluation revisions','',
            'The following changes were made after reviewing the initial results. The reported scores therefore reflect a revised development evaluation.','']
        lines.extend('- '+revision['note'] for revision in report['revisions'])
    if report.get('timeline_charts'):
        lines += ['', '## Per-video interval comparisons','',
            f"[Interactive interval comparisons]({report['interactive_report_url']}) provide playback of the corresponding source-video intervals.",'',
            'Green = ground truth; orange = predicted time. Dashed outlines group predictions into episodes without filling their gaps.',
            'Each video shows events with a labeled or predicted occurrence. No-event/no-event decisions remain included in the summary above. Times below are source-video seconds, rounded to three decimal places.','']
        for clip in dataset['clips']:
            chart = next((c for c in report['timeline_charts'] if c['clip_id']==clip['id']),None)
            if chart is None:continue
            chart_file='timelines/'+Path(chart['file']).name
            lines += [f"### {clip['title']}",'',f"![Ground-truth and predicted intervals: {clip['title']}]({chart_file})",'',
                      '| Event | Ground truth | Predicted detected pieces | Merged episode spans |',
                      '| --- | --- | --- | --- |']
            for case in report['cases']:
                if case['clip_id']!=clip['id'] or case['expected']!='observed' and case['prediction']!='observed':continue
                def times(parts):return ', '.join(f"{p['start']:.3f}–{p['end']:.3f}" for p in parts) or '—'
                truth=times(case['expected_intervals'] or []) if case['expected']=='observed' else case['expected'].replace('_',' ')
                predicted=times(case.get('predicted_intervals',[])) if case['prediction']=='observed' else case['prediction'].replace('_',' ')
                lines.append(f"| {dataset['behavior_titles'][case['behavior']]} | {truth} | {predicted} | {times(case.get('predicted_episodes',[]))} |")
            lines.append('')
    lines += ['', '## Detection errors and temporal differences','',
        'The following cases contain an incorrect event decision or a temporal IoU below 0.8. Evidence text is model output, not an additional ground-truth annotation.','']
    for case in report['cases']:
        if not case['scored']:
            continue
        timing=case['timing']
        if not case['correct'] or timing and timing['iou'] is not None and timing['iou']<.8:
            title=next(clip['title'] for clip in dataset['clips'] if clip['id']==case['clip_id'])
            times=lambda parts:', '.join(f"{part['start']:.3f}–{part['end']:.3f}s" for part in parts or []) or 'None'
            lines += [f"### {title} / {dataset['behavior_titles'][case['behavior']]}",
                f"Ground truth: {case['expected'].replace('_',' ')}; intervals: {times(case['expected_intervals'])}.",
                f"Prediction: {case['prediction'].replace('_',' ')}; intervals: {times(case.get('predicted_intervals',[]))}.",'']
            lines.extend('- '+text.replace('\n',' ') for text in case['evidence'] if text)
            lines.append('')
    lines += ['## Limitations','']+['- '+text for text in report['limitations']]
    return '\n'.join(lines)+'\n'
