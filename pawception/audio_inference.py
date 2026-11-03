"""Audio-only inference through Meta's working Chat Completions input path."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import time

from pawception.monitor import monitor_format, render_monitor_prompt, validate_observations


def chat_audio(client, audio, prompt, model, budget, options, output, mode):
    """Save streamed chunks and retain only the final content for validation."""
    schema = options.get('text', {}).get('format')
    request = {'model':model, 'store':False, 'max_completion_tokens':budget,
               'stream':options.get('stream', False),
               'messages':[{'role':'user','content':[{'type':'text','text':prompt},
                   {'type':'input_audio','input_audio':{
                       'data':base64.b64encode(audio.read_bytes()).decode(), 'format':'wav'}}]}]}
    if schema:
        request['response_format'] = {'type':'json_schema', 'json_schema':{k:v for k,v in schema.items() if k!='type'}}
    if 'reasoning' in options:
        request['reasoning_effort'] = options['reasoning']['effort']
    for key in ('temperature','top_p'):
        if key in options:
            request[key] = options[key]
    if request['stream']:
        request['stream_options'] = {'include_usage':True}
    response = client.chat.completions.create(**request)
    if not request['stream']:
        return response.model_dump(mode='json')
    content, finish, raw = [], None, {}
    with response as chunks, (output/f'{mode}.events.jsonl').open('w') as log:
        for chunk in chunks:
            item = chunk.model_dump(mode='json')
            log.write(json.dumps(item)+'\n')
            log.flush()
            raw.update({key:item[key] for key in ('id','model','created')})
            if item.get('usage'):
                raw['usage'] = item['usage']
            for choice in item['choices']:
                content.append(choice.get('delta', {}).get('content') or '')
                if choice.get('finish_reason'):
                    finish = choice['finish_reason']
    if finish is None:
        raise ValueError('Audio stream ended without a final completion')
    return {**raw, 'object':'chat.completion', 'choices':[{'index':0,'finish_reason':finish,
             'message':{'role':'assistant','content':''.join(content)}}]}


def analyze_audio(client, audio, prompts, output, model, budget, task, duration, output_format, options, cancelled):
    from pawception.muse import safe_error
    from pawception.files import write_json
    from pawception.live_muse import check_cancelled
    result = {'video':str(audio.resolve()), 'sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),
              'bytes':audio.stat().st_size, 'media_kind':'audio', 'endpoint':'chat/completions',
              'upload_seconds':0, 'upload_cleanup':'not_required_inline',
              'started_at':datetime.now(timezone.utc).isoformat(), 'status':'analyzing', 'analyses':{}}
    write_json(output/'result.json', result)
    for mode, prompt in prompts.items():
        request_options = dict(options or {})
        if mode == 'monitor':
            prompt = render_monitor_prompt(prompt, task, duration)
            request_options.setdefault('text', {'format':monitor_format(task, output_format)})
        record = {'status':'running', 'prompt':prompt, 'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
                  'requested_model':model, 'request_options':request_options,
                  'endpoint':'chat/completions', 'started_at':datetime.now(timezone.utc).isoformat()}
        if 'text' in request_options:
            record['response_format'] = request_options['text']['format']
        result['analyses'][mode] = record
        (output/f'{mode}.prompt.txt').write_text(prompt+'\n')
        write_json(output/'result.json', result)
        started = time.perf_counter()
        try:
            check_cancelled(cancelled)
            raw = chat_audio(client, audio, prompt, model, budget, request_options, output, mode)
            write_json(output/f'{mode}.response.json', raw)
            choice = raw['choices'][0]
            text = choice['message'].get('content') or ''
            (output/f'{mode}.txt').write_text(text+'\n')
            completed = choice['finish_reason'] == 'stop' and bool(text.strip())
            usage = raw.get('usage') or {}
            record.update(status='completed' if completed else 'incomplete', response_id=raw['id'],
                          returned_model=raw['model'], response_status=choice['finish_reason'],
                          usage={**usage, 'input_tokens':usage.get('prompt_tokens'), 'output_tokens':usage.get('completion_tokens')})
            if mode == 'monitor' and completed:
                try:
                    observation = validate_observations(text, task, duration, output_format)
                    record.update(validation_status='valid', structured_output=observation)
                    write_json(output/'monitor.observations.json', observation)
                except (ValueError, TypeError) as error:
                    record.update(status='invalid', validation_status='invalid', validation_error=str(error))
        except Exception as error:
            record.update(status='error', error=safe_error(error))
        finally:
            record['inference_seconds'] = time.perf_counter()-started
            write_json(output/'result.json', result)
    result.update(status='completed' if all(r['status']=='completed' for r in result['analyses'].values()) else 'has_errors',
                  finished_at=datetime.now(timezone.utc).isoformat())
    write_json(output/'result.json', result)
    return result
