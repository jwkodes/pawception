"""Add interval bar charts and a clickable HTML companion to a saved evaluation.

Uses matplotlib from the optional MoGe experiment environment. No inference.
"""

import argparse
from html import escape
from io import StringIO
import json
from pathlib import Path
import re
import textwrap

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

from pawception.evaluation import digest
from pawception.submission_eval import markdown_report
from pawception.files import write_json

GT, PRED, OUTLINE = '#397968', '#cc843b', '#425267'


def selected_cases(report, clip_id):
    return [case for case in report['cases'] if case['clip_id']==clip_id
            and (case['expected']=='observed' or case['prediction']=='observed')]


def chart(clip, cases, titles, gap):
    figure, ax = plt.subplots(figsize=(12.8, 1.8+1.1*max(1,len(cases))),dpi=150)
    figure.set_facecolor('#fffefa');ax.set_facecolor('#f5f7f2')
    labels, positions = [], []
    duration = clip['duration_seconds']
    if not cases:ax.text(duration/2,.5,'No labeled or predicted occurrences. True negatives are included in the metrics.',ha='center',va='center',fontsize=10,color='#627265')
    for index, case in enumerate(cases):
        base = (len(cases)-index-1)*3
        for y, parts, colour, state, name in [
                (base+1,case['expected_intervals'] or [],GT,case['expected'],'GT'),
                (base,case.get('predicted_intervals') or [],PRED,case['prediction'],'Predicted')]:
            title='\n'.join(textwrap.wrap(titles[case['behavior']],30))
            labels.append(f'{title}\n{name}');positions.append(y)
            ax.axhline(y,color='#dce4da',linewidth=1,zorder=0)
            if not parts:
                ax.text(duration*.015,y,state.replace('_',' ').capitalize(),va='center',fontsize=9,color='#727e74')
            for part in parts:
                patch=Rectangle((part['start'],y-.20),part['end']-part['start'],.4,facecolor=colour,edgecolor='none',zorder=3)
                patch.set_url(f"#t={part['start']},{part['end']}")
                ax.add_patch(patch)
                if (part['end']-part['start'])/duration>.09:
                    ax.text((part['start']+part['end'])/2,y,f"{part['start']:.2f}–{part['end']:.2f}",
                            color='white',fontsize=8,va='center',ha='center',zorder=4)
        for episode in case.get('predicted_episodes',[]):
            patch=Rectangle((episode['start'],base-.32),episode['end']-episode['start'],.64,
                            facecolor='none',edgecolor=OUTLINE,linewidth=1,linestyle=(0,(4,3)),zorder=2)
            patch.set_url(f"#t={episode['start']},{episode['end']}")
            ax.add_patch(patch)
        if index<len(cases)-1:ax.axhline(base-.85,color='#e1e6dc',linewidth=.8)
    ax.set_yticks(positions,labels=labels,fontsize=9)
    ax.tick_params(axis='y',length=0,pad=12)
    ax.set_xlim(0,duration);ax.set_ylim(-.7,max(1,len(cases))*3-.8)
    ticks=[duration*i/6 for i in range(7)]
    ax.set_xticks(ticks,labels=[f'{value:.2f}' for value in ticks],fontsize=9)
    ax.set_xlabel('Video time (seconds)',fontsize=9,labelpad=9,color='#4d5e51')
    ax.grid(axis='x',color='#d9e0d4',linewidth=.7,zorder=0)
    for spine in ax.spines.values():spine.set_visible(False)
    figure.suptitle(clip['title'],x=.03,y=.97,ha='left',fontsize=13,fontweight='bold',color='#283e31')
    figure.legend(handles=[Patch(color=GT,label='Ground truth'),Patch(color=PRED,label='Predicted time'),
                  Line2D([0],[0],color=OUTLINE,linestyle='--',label=f'Merged episode (gap ≤ {gap:g}s)')],
                  loc='upper left',bbox_to_anchor=(.023,.89),frameon=False,ncol=3,fontsize=8)
    figure.subplots_adjust(left=.25,right=.97,top=.72,bottom=.20)
    return figure


def html_report(report, dataset, charts):
    cards=[]
    revisions = ('<h2>Evaluation revisions</h2><p>These changes followed review of the initial results. Scores reflect a revised development evaluation.</p>'
        + ''.join('<p>'+escape(revision['note'])+'</p>' for revision in report['revisions'])) if report.get('revisions') else ''
    summary=report['summary']
    results=escape(f"{len(report['clips'])} videos · {summary['scored_cases']} scored clip/event decisions · "
        f"{summary['accuracy']:.1%} presence/absence accuracy · {summary['mean_positive_temporal_iou']:.3f} mean positive temporal IoU")
    limitations='<h2>Limitations</h2><ul>'+''.join('<li>'+escape(note)+'</li>' for note in report['limitations'])+'</ul>'
    for clip, svg in charts:
        cards.append(f'''<section data-clip="{escape(clip['id'])}"><h2>{escape(clip['title'])}</h2>
          <div class="chart">{svg}</div><details><summary>Video playback · click a bar to open</summary>
          <video controls preload="none" muted playsinline src="/label-media/{escape(clip['id'])}.mp4"></video>
          <p class="playing" aria-live="polite"></p></details></section>''')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Pawception · Interval comparison report</title><style>
    :root{font:15px system-ui,sans-serif;color:#283e31;background:#f5f4ee}body{max-width:1280px;margin:auto;padding:36px 24px}h1{font-size:32px;letter-spacing:-1px;margin:0 0 12px}p{color:#627265;line-height:1.6}section{background:#fffefa;border:1px solid #dce3d7;border-radius:14px;margin:24px 0;padding:20px}h2{font-size:18px;margin:0}svg{width:100%;height:auto;display:block}.chart{margin-top:12px;overflow-x:auto}.chart svg a{cursor:pointer}.chart svg g[id^="text_"]{pointer-events:none}.chart svg a:hover path{opacity:.7}video{display:block;max-width:850px;width:100%;max-height:500px;margin-top:16px;background:#16231b;border-radius:10px}summary{cursor:pointer;font-size:13px}details{margin-top:14px}.links{display:flex;gap:18px;font-size:13px}a{color:#315c49}@media(max-width:650px){.chart svg{min-width:750px}body{padding:24px 12px}section{padding:12px}}
    </style></head><body><div class="links"><a href="/label">Labeling page</a><a href="report.md">Markdown report</a><a href="report.json">Report JSON</a></div>
    <h1>Behavior and safety detection evaluation</h1><p>''' + escape(f"{report['run_id']} · annotation revision {report['dataset_revision']}") + '''</p>
    <p>This report compares Pawception's detections with human-reviewed behavior and hazard intervals. The videos form a small development set, not an independent benchmark of performance on unseen household footage.</p>
    <p>'''+results+'''</p>
    <p>Presence/absence accuracy measures whether each event occurs in a video. Temporal intersection over union (IoU) measures overlap between predicted and labeled time divided by their combined duration; correct event decisions can still have imperfect time boundaries. Unassessable annotations are excluded.</p>
    <p>Green bars show human annotations; orange bars show predicted detections. Dashed outlines show episodes merged across gaps of up to '''+escape(f"{report['postprocessing']['merge_gap_seconds']:g}")+'''s; gaps do not count as detected time. A predicted episode matches a labeled episode at temporal IoU ≥ '''+escape(f"{dataset['scoring']['interval_iou_threshold']:g}")+'''. Bars link to playback of the corresponding video interval. Only events with a labeled or predicted occurrence are plotted; all scored negatives remain in the metrics.</p>
    '''+revisions+'\n'.join(cards)+limitations+'''<script>
    document.addEventListener('click',event=>{const link=event.target.closest('a');if(!link)return;const href=link.getAttribute('href')||link.getAttribute('xlink:href')||'';if(!href.startsWith('#t='))return;
    event.preventDefault();const [start,end]=href.slice(3).split(',').map(Number),section=link.closest('section'),video=section.querySelector('video');if(!Number.isFinite(start)||!Number.isFinite(end))return;
    for(const other of document.querySelectorAll('video'))if(other!==video)other.pause();section.querySelector('details').open=true;video.currentTime=start;video.dataset.end=end;
    section.querySelector('.playing').textContent=`Playing ${start.toFixed(3)}–${end.toFixed(3)} seconds of the original recording.`;video.play().catch(()=>{});video.scrollIntoView({behavior:'smooth',block:'nearest'});});
    for(const video of document.querySelectorAll('video'))video.addEventListener('timeupdate',()=>{if(video.dataset.end&&video.currentTime>=Number(video.dataset.end)){video.pause();delete video.dataset.end;}});
    </script></body></html>'''


def render(output):
    report=json.loads((output/'report.json').read_text());dataset=json.loads((output/'ground_truth.snapshot.json').read_text())
    if not re.fullmatch(r'submission-\d{8}T\d{6}_\d{6}Z',output.name):raise ValueError('Choose a saved submission evaluation directory')
    folder=output/'timelines';folder.mkdir(exist_ok=True)
    assets, charts=[],[]
    for clip in dataset['clips']:
        if not clip.get('enabled',True):continue
        if not re.fullmatch(r'[a-zA-Z0-9_]+',clip['id']):raise ValueError('Invalid clip identifier')
        cases=selected_cases(report,clip['id'])
        figure=chart(clip,cases,dataset['behavior_titles'],report['postprocessing']['merge_gap_seconds'])
        path=folder/f"{clip['id']}.png";figure.savefig(path,dpi=150,facecolor=figure.get_facecolor())
        svg=StringIO();figure.savefig(svg,format='svg');plt.close(figure)
        content=svg.getvalue();content=content[content.index('<svg'):]
        content=re.sub(r'(<a xlink:href="#t=([^"]+)")',
            lambda match:match[1]+' aria-label="Play '+match[2].replace(',',' to ')+' seconds"',content)
        assets.append({'clip_id':clip['id'],'file':str(path.resolve()),'sha256':digest(path)})
        charts.append((clip,content))
    report['timeline_charts']=assets
    report['interactive_report_url']=f'http://127.0.0.1:8765/evaluation-reports/{output.name}/report.html'
    report['report_provenance']['visualization_sha256']=digest(Path(__file__))
    write_json(output/'report.json',report)
    (output/'report.md').write_text(markdown_report(report,dataset))
    (output/'report.html').write_text('\n'.join(line.rstrip() for line in html_report(report,dataset,charts).splitlines())+'\n')
    print(f"Rendered {len(assets)} video comparisons: {output/'report.md'}")
    print(report['interactive_report_url'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    render(parser.parse_args(argv).output.resolve())
