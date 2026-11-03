"""Export reviewable snapshots of the actual runtime prompts and JSON schemas."""

import argparse
import json

from pawception.event_inputs import FORMAT as ROUTING_FORMAT, PROMPT as ROUTING_PROMPT
from pawception.event_suggestions import FORMAT as SUGGESTION_FORMAT, PROMPT as SUGGESTION_PROMPT
from pawception.monitor import monitor_format
from pawception.pipeline import perception_prompt
from pawception.safety import DEFAULT_HAZARD_PROMPT
from pawception.paths import ROOT


def snapshots():
    task=json.loads((ROOT/'prompts/examples/monitor_task.json').read_text())
    audio={**task,'input_kind':'audio','safety_watch':False,'events':[{
        'id':'barking','description':'A dog barks repeatedly in an audible bout.','input_mode':'audio'}]}
    pretty=lambda value:json.dumps(value,indent=2,ensure_ascii=False)+'\n'
    return {
        'event_enrichment.system.txt':SUGGESTION_PROMPT,
        'event_enrichment.schema.json':pretty(SUGGESTION_FORMAT),
        'event_routing.system.txt':ROUTING_PROMPT,
        'event_routing.schema.json':pretty(ROUTING_FORMAT),
        'safety_watch.default.txt':DEFAULT_HAZARD_PROMPT,
        'visual_monitor.prompt.txt':perception_prompt(task,60),
        'visual_monitor.schema.json':pretty(monitor_format(task,'binary')),
        'audio_monitor.prompt.txt':perception_prompt(audio,60),
        'audio_monitor.schema.json':pretty(monitor_format(audio,'binary')),
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Fail if committed snapshots differ from the runtime builders')
    args=parser.parse_args();directory=ROOT/'prompts/examples'
    for name,content in snapshots().items():
        path=directory/name
        if args.check:
            if not path.is_file() or path.read_text()!=content:
                raise SystemExit(f'Stale prompt snapshot: {name}. Run python -m scripts.export_prompts.')
        else:path.write_text(content)
    print('Runtime prompt snapshots verified.' if args.check else 'Exported runtime prompts and schemas. No API calls.')
