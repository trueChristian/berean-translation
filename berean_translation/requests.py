"""Structured API requests and conservative, stage-by-stage budget reservations."""
from __future__ import annotations
import math
from .common import ContractError, canonical, json_hash, loads, read_json

TRANSLATION_SCHEMA = {
    'type':'object','additionalProperties':False,
    'properties':{'html':{'type':'string'}, **{k:{'type':['string','null']} for k in ('title','subtitle','section')}},
    'required':['html','title','subtitle','section']}
REVIEW_SCHEMA = {
    'type':'object','additionalProperties':False,
    'properties':{'score':{'type':'integer'},'passed':{'type':'boolean'},'findings':{'type':'array','items':{
        'type':'object','additionalProperties':False,
        'properties':{'severity':{'type':'string','enum':['minor','major','critical']},
                      **{k:{'type':'string'} for k in ('location','source_quote','translation_quote','suggested_fix')}},
        'required':['severity','location','source_quote','translation_quote','suggested_fix']}}},
    'required':['score','passed','findings']}


def build_request(config, state, task):
    source = state.source(task)
    review = task['stage'] in ('review1','review2')
    chosen = task['review_model'] if review else task['model']
    model = task['models'][chosen]
    # Prompts, configuration and glossary are frozen when the campaign is accepted.
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    lang = campaign['language_settings'][task['language']]
    payload = {'target_language':lang['name'],'language_tag':lang['tag'],'language_guidance':lang['guidance'],
               'terminology_glossary':campaign['glossaries'].get(task['language'],{}),
               'source':{'html':source['html'], 'title':source['article'].get('title'),
                         'subtitle':source['article'].get('subtitle'),'section':source['article'].get('section'),
                         'byline':source['article'].get('byline')}}
    if review or task['stage'] == 'correct':
        payload['translation'] = state.candidate(task)
    if task['stage'] == 'correct':
        payload['correction_findings'] = task.get('findings',[])
    output_limit = campaign['review_output_tokens'] if review else campaign['max_output_tokens']
    output_limit = min(output_limit,model['max_output_tokens'])
    body = {'model':model['api_model'],
            'messages':[{'role':'system','content':campaign['prompts']['review' if review else 'translation']},
                        {'role':'user','content':canonical(payload).decode('utf-8')}],
            'max_completion_tokens':output_limit,
            'response_format':{'type':'json_schema','json_schema':{'name':'article_review' if review else 'article_translation',
                                    'strict':True,'schema':REVIEW_SCHEMA if review else TRANSLATION_SCHEMA}}}
    if model.get('reasoning_effort'):
        body['reasoning_effort'] = model['reasoning_effort']
    # One UTF-8 byte per input token plus generous framing allowance deliberately
    # overestimates ordinary byte-level tokenization. Not a promise about future pricing.
    input_bound = len(canonical(body)) + 4096
    if input_bound + output_limit > model['context_tokens']:
        raise ContractError('Conservative request token bound exceeds the selected model context')
    estimate = math.ceil((input_bound*model['input_batch_usd_per_million'] +
                          output_limit*model['output_batch_usd_per_million'])) / 1000000
    line = {'custom_id':task['id'] + ':' + task['stage'],'method':'POST','url':'/v1/chat/completions','body':body}
    return line,estimate,input_bound


def parse_response(row: dict, maximum_bytes: int) -> tuple[dict,dict]:
    if row.get('error') or not isinstance(row.get('response'),dict):
        code = (row.get('error') or {}).get('code','missing_response')
        raise ContractError(f'Batch request failed: {code}')
    response = row['response']
    if response.get('status_code') != 200:
        raise ContractError(f'Batch request HTTP status {response.get("status_code")}')
    body = response.get('body',{})
    choices = body.get('choices',[])
    if len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
        raise ContractError('Missing, ambiguous, or truncated model response')
    message = choices[0].get('message',{})
    if message.get('refusal') or not isinstance(message.get('content'),str):
        raise ContractError('Model refused or did not return a textual structured result')
    if len(message['content'].encode('utf-8')) > maximum_bytes:
        raise ContractError('Model result exceeds configured safety limit')
    data = loads(message['content'])
    if not isinstance(data,dict) or not isinstance(body.get('model'),str) or not body['model']:
        raise ContractError('Structured result or actual model identity is missing')
    provenance = {'model':body['model'],'request_id':response.get('request_id'),
                  'response_id':body.get('id'),'usage':body.get('usage',{})}
    return data,provenance


def accepted_review(review: dict, threshold: int = 95) -> bool:
    if set(review) != {'score','passed','findings'} or type(review['score']) is not int or not 0 <= review['score'] <= 100:
        raise ContractError('Invalid review score or schema')
    if type(review['passed']) is not bool or not isinstance(review['findings'],list) or len(review['findings']) > 30:
        raise ContractError('Invalid review verdict or findings')
    for item in review['findings']:
        keys = {'severity','location','source_quote','translation_quote','suggested_fix'}
        if not isinstance(item,dict) or set(item) != keys or not all(isinstance(v,str) for v in item.values()):
            raise ContractError('Invalid review finding')
        if item['severity'] not in ('minor','major','critical'):
            raise ContractError('Unknown finding severity')
    return review['passed'] and review['score'] >= threshold and not any(
        x['severity'] in ('major','critical') for x in review['findings'])
