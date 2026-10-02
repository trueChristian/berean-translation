"""Structured API requests and conservative, stage-by-stage budget reservations."""
from __future__ import annotations
from decimal import Decimal, ROUND_CEILING
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


def _batch_rates(model: dict, input_tokens: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Use the campaign's frozen prices, including whole-request long context."""
    ordinary = Decimal(str(model['input_batch_usd_per_million']))
    cached = Decimal(str(model.get('cached_input_batch_usd_per_million', ordinary)))
    written = Decimal(str(model.get('cache_write_batch_usd_per_million', ordinary)))
    output = Decimal(str(model['output_batch_usd_per_million']))
    threshold = model.get('long_context_threshold_tokens')
    if threshold is not None and input_tokens > threshold:
        multiplier = Decimal(str(model['long_context_input_multiplier']))
        ordinary, cached, written = (rate * multiplier for rate in (ordinary, cached, written))
        output *= Decimal(str(model['long_context_output_multiplier']))
    return ordinary, cached, written, output


def reserve_cost(model: dict, input_tokens: int, output_tokens: int) -> float:
    """Reserve the worst input category and all visible/reasoning output tokens."""
    if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
        raise ContractError('Cost token bounds must be nonnegative integers')
    ordinary, cached, written, output = _batch_rates(model, input_tokens)
    # Cache writes replace ordinary input billing; no cache hit is assumed.
    # Round up to a microdollar before the campaign ledger adds reservations.
    microdollars = input_tokens * max(ordinary, cached, written) + output_tokens * output
    return float(microdollars.to_integral_value(rounding=ROUND_CEILING) / 1000000)


def usage_cost(model: dict, usage: dict) -> float | None:
    """Price reported Chat Completions usage, conservatively filling cache gaps.

    Missing/invalid total counters return None, never a fabricated zero. When
    cache counters are absent or invalid, their unknown share uses the highest
    input rate. Legacy frozen entries without cache prices retain their former
    ordinary-input accounting. This is telemetry, not a release of reservations.
    """
    if not isinstance(usage, dict):
        return None
    input_tokens, output_tokens = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
        return None
    ordinary, cached, written, output = _batch_rates(model, input_tokens)
    unknown = max(ordinary, cached, written)
    details = usage.get('prompt_tokens_details')
    details = details if isinstance(details, dict) else {}
    counts = {key:details.get(key) for key in ('cached_tokens', 'cache_write_tokens')}
    # A reported malformed or contradictory breakdown cannot justify discounts.
    malformed = any(value is not None and (type(value) is not int or value < 0)
                    for value in counts.values())
    if not malformed:
        malformed = sum(value for value in counts.values() if value is not None) > input_tokens
    if malformed:
        input_cost = input_tokens * unknown
    else:
        cache_count, write_count = counts['cached_tokens'], counts['cache_write_tokens']
        remainder = input_tokens - (cache_count or 0) - (write_count or 0)
        # A missing cache-write count may conceal premium-rate input. If writes
        # are known, unknown cache reads cannot cost more than ordinary input.
        remainder_rate = max(ordinary, cached) if write_count is not None else unknown
        if cache_count is not None and write_count is not None:
            remainder_rate = ordinary
        input_cost = ((cache_count or 0) * cached + (write_count or 0) * written
                      + remainder * remainder_rate)
    # completion_tokens already includes reasoning tokens; never add them again.
    return float((input_cost + output_tokens * output) / 1000000)


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
                         'subtitle':source['article'].get('subtitle'),'section':source['article'].get('section')}}
    byline = source['article'].get('byline')
    if campaign.get('prompt_version') in (None, '1.0.0', '1.0.1'):
        # Preserve the request contract paired with historical frozen prompts.
        payload['source']['byline'] = byline
    else:
        # Attribution outside source HTML is context, rendered separately from
        # English by the website. It is deliberately outside translatable fields.
        payload['source_context'] = {'byline':byline}
    if review or task['stage'] == 'correct':
        payload['translation'] = state.candidate(task)
    if task['stage'] == 'correct':
        payload['correction_findings'] = task.get('findings',[])
        if task.get('downstream_recovery'):
            payload['rejection_reason'] = task.get('rejection_reason')
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
    estimate = reserve_cost(model, input_bound, output_limit)
    line = {'custom_id':task['id'] + ':' + task['stage'],'method':'POST','url':'/v1/chat/completions','body':body}
    return line,estimate,input_bound


def parse_response(row: dict, maximum_bytes: int) -> tuple[dict,dict]:
    if not isinstance(row,dict):
        raise ContractError('Batch result row must be an object')
    error = row.get('error')
    if error is not None and not isinstance(error,dict):
        raise ContractError('Batch request error must be an object or null')
    if error is not None or not isinstance(row.get('response'),dict):
        code = (error or {}).get('code','missing_response')
        raise ContractError(f'Batch request failed: {code}')
    response = row['response']
    if response.get('status_code') != 200:
        raise ContractError(f'Batch request HTTP status {response.get("status_code")}')
    body = response.get('body',{})
    if not isinstance(body,dict):
        raise ContractError('Batch response body must be an object')
    choices = body.get('choices',[])
    if (not isinstance(choices,list) or len(choices) != 1 or not isinstance(choices[0],dict)
            or choices[0].get('finish_reason') != 'stop'):
        raise ContractError('Missing, ambiguous, or truncated model response')
    message = choices[0].get('message',{})
    if not isinstance(message,dict):
        raise ContractError('Model response message must be an object')
    if message.get('refusal') or not isinstance(message.get('content'),str):
        raise ContractError('Model refused or did not return a textual structured result')
    try:
        content_bytes = message['content'].encode('utf-8')
    except UnicodeError as exc:
        raise ContractError('Model result contains invalid Unicode') from exc
    if len(content_bytes) > maximum_bytes:
        raise ContractError('Model result exceeds configured safety limit')
    data = loads(message['content'])
    if not isinstance(data,dict) or not isinstance(body.get('model'),str) or not body['model']:
        raise ContractError('Structured result or actual model identity is missing')
    usage = body.get('usage')
    if usage is not None and not isinstance(usage,dict):
        raise ContractError('Model usage must be an object or null')
    provenance = {'model':body['model'],'request_id':response.get('request_id'),
                  'response_id':body.get('id'),'usage':usage or {}}
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
