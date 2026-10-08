"""Versioned, replayable OFFLINE component translation/review protocol.

Constructs exact Batch request previews and validates supplied response rows.
Never writes State, reserves money, invokes a provider, admits work or publishes.
The live v2 guards remain in force. Cross-cycle funding/lineage is separate work.
"""
from __future__ import annotations

import copy
import math
import re
from decimal import Decimal
from pathlib import Path

from .common import ContractError, canonical, json_hash
from .requests import TRANSLATION_SCHEMA, reserve_cost, parse_response, accepted_review
from .review_contract import review_schema, validate_review, frozen_version
from .scripture_component_evidence import validate_component_evidence, check_component_selections
from .scripture_evidence import ScriptureAttention, frozen_policy, _bounded_selection_input

PROCESSING_VERSION = '1'
STAGES = ('translate', 'review1', 'correct', 'review2')


def _same(left, right):
    """Exact JSON identity, including numeric types; Python bool/int equality is unsafe."""
    return canonical(left) == canonical(right)


def _object(properties):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(properties)}


def response_schemas():
    text, integer = {'type': 'string'}, {'type': 'integer'}
    def operation(kind, fields):
        return _object({'kind': {'type': 'string', 'enum': [kind]}, **fields})
    span = {'start': integer, 'end': integer, 'text': text}
    operations = {'type': 'array', 'maxItems': 128, 'items': {'anyOf': [
        operation('canonical', span),
        operation('omit', {**span, 'reason': {'type': 'string', 'enum': ['excerpt_boundary']}}),
        operation('replace', {**span, 'id': text, 'authored_text': text}),
        operation('insert', {'id': text, 'at': integer, 'authored_text': text}),
        operation('punctuation', {'id': text, 'at': integer, 'authored_text': text})]}}
    binding = _object({'version': {'type': 'string', 'enum': [PROCESSING_VERSION]},
        'stage': {'type': 'string', 'enum': list(STAGES)}, 'ordinal': integer,
        **{key: text for key in ('context_sha256', 'source_sha256', 'evidence_sha256',
                                 'processing_contract_sha256', 'input_sha256')},
        'candidate_sha256': {'type': ['string', 'null']},
        'selection_proof_sha256': {'type': ['string', 'null']}})
    selections = {'type': 'array', 'maxItems': 128, 'items': _object({
        'quote_id': text, 'block': text, 'start': integer, 'end': integer, 'operations': operations})}
    return {'generation': _object({'binding': binding, 'candidate': copy.deepcopy(TRANSLATION_SCHEMA),
                                    'scripture_selections': selections}),
            'review': _object({'binding': copy.deepcopy(binding), 'review': review_schema(2)})}


def processing_contract(root, campaign, task, *, maximum_result_bytes=1000000):
    """Freeze an unfunded offline protocol from existing campaign/model context."""
    scripture = frozen_policy(campaign)
    if not scripture or scripture.get('version') != '2' or frozen_version(campaign) != 2:
        raise ContractError('Component processing requires explicit evidence v2 and complete review v2')
    if task.get('campaign') != campaign.get('id') or not _same(task.get('models'), campaign.get('models')):
        raise ContractError('Component processing task and frozen campaign models disagree')
    snapshot = re.fullmatch(r'state/sources/([0-9a-f]{64})\.json', task.get('source_snapshot', ''))
    if (snapshot is None or task.get('stage') != 'translate' or task.get('status') != 'queued'
            or any(type(task.get(key)) is not int or task[key] != 0
                   for key in ('translation_attempts', 'review_attempts'))
            or task.get('batch')):
        raise ContractError('Offline first-line processing requires a never-attempted queued source-bound task')
    generation, review = task['model'], task['review_model']
    language = campaign['language_settings'][task['language']]
    generation_addendum = (Path(root) / 'prompts/scripture-components-generation-v1.txt').read_text(encoding='utf-8')
    review_addendum = (Path(root) / 'prompts/scripture-components-review-v1.txt').read_text(encoding='utf-8')
    prompts = {'generation': campaign['prompts']['translation'] + '\n\n' + generation_addendum,
               'review': campaign['prompts']['review'] + '\n\n' + review_addendum}
    result = {'version': PROCESSING_VERSION, 'schema_version': '1', 'review_contract_version': 2,
        'task_id': task['id'], 'campaign_id': campaign['id'], 'origin_task_sha256': json_hash(task),
        'article_id': task['article_id'], 'source_sha256': snapshot[1],
        'scripture_policy_sha256': json_hash(scripture),
        'origin_campaign_sha256': json_hash(campaign), 'language': task['language'],
        'language_settings': copy.deepcopy(language),
        'glossary': copy.deepcopy(campaign['glossaries'].get(task['language'], {})),
        'generation_model': generation, 'review_model': review,
        'models': {name: copy.deepcopy(campaign['models'][name]) for name in {generation, review}},
        'quality_threshold': campaign['quality_threshold'],
        'max_output_tokens': campaign['max_output_tokens'], 'review_output_tokens': campaign['review_output_tokens'],
        'maximum_result_bytes': maximum_result_bytes, 'maximum_context_bytes': 2000000,
        'maximum_snapshot_bytes': 16000000,
        'limits': {'generation': 2, 'review': 2, 'corrections': 1},
        'prompts': prompts, 'prompts_sha256': json_hash(prompts),
        'schemas': response_schemas(), 'schemas_sha256': json_hash(response_schemas()),
        'offline_only': True, 'funding_authorized': False, 'publication_authorized': False}
    validate_processing_contract(result)
    return result


def validate_processing_contract(contract):
    expected = {'version', 'schema_version', 'review_contract_version', 'task_id', 'campaign_id',
        'article_id', 'source_sha256', 'scripture_policy_sha256',
        'origin_task_sha256', 'origin_campaign_sha256', 'language', 'language_settings', 'glossary',
        'generation_model', 'review_model', 'models', 'quality_threshold', 'max_output_tokens',
        'review_output_tokens', 'maximum_result_bytes', 'maximum_context_bytes', 'maximum_snapshot_bytes',
        'limits', 'prompts', 'prompts_sha256', 'schemas', 'schemas_sha256', 'offline_only',
        'funding_authorized', 'publication_authorized'}
    if (not isinstance(contract, dict) or set(contract) != expected
            or contract['version'] != PROCESSING_VERSION or contract['schema_version'] != '1'
            or type(contract['review_contract_version']) is not int or contract['review_contract_version'] != 2
            or contract['limits'] != {'generation': 2, 'review': 2, 'corrections': 1}
            or any(type(value) is not int for value in contract['limits'].values())
            or contract['offline_only'] is not True or contract['funding_authorized'] is not False
            or contract['publication_authorized'] is not False):
        raise ContractError('Unsupported or activated component processing contract')
    if (type(contract['quality_threshold']) is not int or not 95 <= contract['quality_threshold'] <= 100
            or any(type(contract[key]) is not int or contract[key] <= 0
                   for key in ('max_output_tokens', 'review_output_tokens', 'maximum_result_bytes',
                               'maximum_context_bytes', 'maximum_snapshot_bytes'))
            or contract['maximum_result_bytes'] > 1000000 or contract['maximum_context_bytes'] != 2000000
            or contract['maximum_snapshot_bytes'] != 16000000):
        raise ContractError('Invalid frozen processing quality/resource bounds')
    prompts = contract['prompts']
    if (not isinstance(prompts, dict) or set(prompts) != {'generation', 'review'}
            or any(not isinstance(text, str) or not text.strip() for text in prompts.values())
            or json_hash(prompts) != contract['prompts_sha256']
            or not _same(contract['schemas'], response_schemas())
            or json_hash(contract['schemas']) != contract['schemas_sha256']):
        raise ContractError('Frozen component prompts or response schemas changed')
    for key in ('task_id', 'campaign_id', 'article_id', 'source_sha256', 'language',
                'origin_task_sha256', 'origin_campaign_sha256'):
        if not isinstance(contract[key], str) or not contract[key]:
            raise ContractError('Missing processing identity')
    for key in ('source_sha256', 'origin_task_sha256', 'origin_campaign_sha256', 'scripture_policy_sha256'):
        if not isinstance(contract[key], str):
            raise ContractError('Invalid processing provenance hash')
        if not re.fullmatch(r'[0-9a-f]{64}', contract[key]):
            raise ContractError('Invalid processing provenance hash')
    language = contract['language_settings']
    if (not isinstance(language, dict) or any(not isinstance(language.get(key), str)
            for key in ('name', 'tag', 'guidance')) or not isinstance(contract['glossary'], dict)):
        raise ContractError('Invalid frozen language settings')
    for name in (contract['generation_model'], contract['review_model']):
        if not isinstance(name, str) or not name:
            raise ContractError('Invalid frozen processing model name')
        model = contract['models'].get(name) if isinstance(contract['models'], dict) else None
        if (not isinstance(model, dict) or not isinstance(model.get('api_model'), str) or not model['api_model']
                or any(type(model.get(key)) is not int or model[key] <= 0
                       for key in ('context_tokens', 'max_output_tokens'))):
            raise ContractError('Invalid frozen processing model')
        try:
            for key in ('input_batch_usd_per_million', 'output_batch_usd_per_million',
                        'cached_input_batch_usd_per_million', 'cache_write_batch_usd_per_million'):
                if key not in model and key in ('cached_input_batch_usd_per_million', 'cache_write_batch_usd_per_million'):
                    continue
                if type(model[key]) not in (str, int, float):
                    raise ValueError('Invalid price type')
                rate = Decimal(str(model[key]))
                if not rate.is_finite() or rate < 0 or not math.isfinite(float(rate)):
                    raise ValueError('Nonfinite or negative price')
            threshold = model.get('long_context_threshold_tokens')
            if threshold is not None and (type(threshold) is not int or threshold <= 0):
                raise ValueError('Invalid long-context threshold')
            for key in ('long_context_input_multiplier', 'long_context_output_multiplier'):
                if key not in model and threshold is None:
                    continue
                if type(model[key]) not in (str, int, float):
                    raise ValueError('Invalid multiplier type')
                rate = Decimal(str(model[key]))
                if not rate.is_finite() or rate <= 0 or not math.isfinite(float(rate)):
                    raise ValueError('Nonfinite or nonpositive multiplier')
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ContractError('Invalid frozen processing prices') from exc
    return contract


class OfflineComponentCycle:
    """One translation, independent review, optional correction and final review.

Repeated request reads are idempotent. Copies returned to callers cannot alter
the frozen context or internal evidence. Snapshot restore replays every exact
request/response; it does not grant future/live funding or reset lineage limits.
"""
    def __init__(self, source, evidence, scripture_policy, processing):
        validate_processing_contract(processing)
        validate_component_evidence(source, evidence, scripture_policy)
        if (json_hash(scripture_policy) != processing['scripture_policy_sha256']
                or json_hash(source) != processing['source_sha256'] or source['article']['id'] != processing['article_id']
                or evidence['language_tag'] != processing['language_settings']['tag']):
            raise ContractError('Component evidence and processing language disagree')
        self._context = copy.deepcopy({'source': source, 'evidence': evidence,
                                        'scripture_policy': scripture_policy, 'processing': processing})
        _bounded_selection_input(self._context, processing['maximum_context_bytes'])
        self._context_sha = json_hash(self._context)
        self._records = []
        self._stage, self._status = 'translate', 'awaiting_request'
        self._candidate = self._selections = self._proof = self._review = None
        self._failure = None
        self._replayable = True

    def _make_request(self, *, require_fresh):
        ctx, contract = self._context, self._context['processing']
        validate_component_evidence(ctx['source'], ctx['evidence'], ctx['scripture_policy'],
                                    require_fresh=require_fresh)
        review = self._stage in ('review1', 'review2')
        generation_count = sum(row['stage'] in ('translate', 'correct') for row in self._records)
        review_count = len(self._records) - generation_count
        if (len(self._records) >= 4 or (review and review_count >= 2)
                or (not review and generation_count >= 2)):
            raise ContractError('Component cycle attempt ceiling exhausted')
        model = contract['models'][contract['review_model'] if review else contract['generation_model']]
        language, source = contract['language_settings'], ctx['source']
        payload = {'target_language': language['name'], 'language_tag': language['tag'],
            'language_guidance': language['guidance'], 'terminology_glossary': copy.deepcopy(contract['glossary']),
            'source': {'html': source['html'], **{key:source['article'].get(key)
                        for key in ('title', 'subtitle', 'section')}},
            'source_context': {'byline': source['article'].get('byline')},
            'scripture_evidence': copy.deepcopy(ctx['evidence'])}
        if self._stage != 'translate':
            if self._candidate is None or self._proof is None:
                raise ContractError('Review/correction requires a complete validated candidate')
            proof = check_component_selections(ctx['evidence'], self._candidate, self._selections,
                        source=source, contract=ctx['scripture_policy'])
            if not _same(proof, self._proof):
                raise ContractError('Component candidate proof changed before request')
            payload.update(translation=copy.deepcopy(self._candidate),
                scripture_selection_audit=copy.deepcopy(proof),
                scripture_selections=copy.deepcopy(self._selections))
        if self._stage == 'correct':
            payload['correction_findings'] = copy.deepcopy(self._review['findings'])
        schema_kind = 'review' if review else 'generation'
        output_limit = min(contract['review_output_tokens'] if review else contract['max_output_tokens'],
                           model['max_output_tokens'])
        body = {'model': model['api_model'], 'messages': [
            {'role': 'system', 'content': contract['prompts'][schema_kind]},
            {'role': 'user', 'content': canonical(payload).decode('utf-8')}],
            'max_completion_tokens': output_limit,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'component_' + schema_kind + '_v1', 'strict': True,
                'schema': copy.deepcopy(contract['schemas'][schema_kind])}}}
        if model.get('reasoning_effort'):
            body['reasoning_effort'] = model['reasoning_effort']
        binding = {'version': PROCESSING_VERSION, 'stage': self._stage, 'ordinal': len(self._records) + 1,
            'context_sha256': self._context_sha, 'source_sha256': json_hash(source),
            'evidence_sha256': json_hash(ctx['evidence']), 'processing_contract_sha256': json_hash(contract),
            'input_sha256': json_hash(body),
            'candidate_sha256': json_hash(self._candidate) if self._candidate is not None else None,
            'selection_proof_sha256': json_hash(self._proof) if self._proof is not None else None}
        payload['expected_binding'] = binding
        body['messages'][1]['content'] = canonical(payload).decode('utf-8')
        input_bound = len(canonical(body)) + 4096
        if input_bound + output_limit > model['context_tokens']:
            raise ContractError('Conservative component request bound exceeds model context; never truncate')
        line = {'custom_id': 'component-offline-' + self._context_sha[:24] + ':' + self._stage,
                'method': 'POST', 'url': '/v1/chat/completions', 'body': body}
        estimate = reserve_cost(model, input_bound, output_limit)
        if not math.isfinite(estimate) or estimate < 0:
            raise ContractError('Component request estimate is not a finite nonnegative bound')
        return {'stage': self._stage, 'line': line, 'request_sha256': json_hash(line),
            'binding': binding, 'input_bound': input_bound,
            'estimated_usd': estimate,
            'offline_only': True, 'funding_authorized': False, 'response': None}

    def request(self):
        if self._status == 'awaiting_response':
            return copy.deepcopy(self._records[-1])
        if self._status != 'awaiting_request':
            raise ContractError('Terminal component cycle cannot create another request')
        record = self._make_request(require_fresh=True)
        self._records.append(record)
        self._status = 'awaiting_response'
        return copy.deepcopy(record)

    def receive(self, row):
        if self._status != 'awaiting_response':
            if (self._records and self._records[-1].get('response') is not None
                    and _same(row, self._records[-1]['response'])):
                return self.snapshot()
            raise ContractError('No matching pending component request; history cannot be overwritten')
        record = self._records[-1]
        if not isinstance(row, dict) or row.get('custom_id') != record['line']['custom_id']:
            raise ContractError('Response belongs to a different component request')
        contract, ctx = self._context['processing'], self._context
        try:
            _bounded_selection_input(row, contract['maximum_result_bytes'] + 65536)
            record['response'] = copy.deepcopy(row)
            record['response_sha256'] = json_hash(row)
        except (ContractError, TypeError, ValueError, UnicodeError, RecursionError):
            self._replayable = False
            self._status, self._failure = 'held', 'Unarchivable or oversized response; no retry is authorized'
            return self.snapshot()
        try:
            response = row.get('response')
            if not isinstance(response, dict) or type(response.get('status_code')) is not int:
                raise ContractError('Component response HTTP status must be an exact integer')
            result, provenance = parse_response(row, contract['maximum_result_bytes'])
            response_body = response.get('body', {})
            if provenance['model'] != record['line']['body']['model']:
                raise ContractError('Component response model differs from the exact frozen requested model')
            choices = response_body.get('choices')
            if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
                message = choices[0].get('message')
                if isinstance(message, dict) and 'refusal' in message and message['refusal'] is not None:
                    raise ContractError('Component response contains a refusal; no retry is authorized')
            record.update(result_sha256=json_hash(result), provenance=provenance)
            review_stage = self._stage in ('review1', 'review2')
            expected = {'binding', 'review'} if review_stage else {'binding', 'candidate', 'scripture_selections'}
            if set(result) != expected or not _same(result['binding'], record['binding']):
                raise ContractError('Component response schema or exact input/candidate binding differs')
            if not review_stage:
                proof = check_component_selections(ctx['evidence'], result['candidate'], result['scripture_selections'],
                                source=ctx['source'], contract=ctx['scripture_policy'])
                self._candidate, self._selections, self._proof = (
                    copy.deepcopy(result['candidate']), copy.deepcopy(result['scripture_selections']), proof)
                self._stage = 'review1' if self._stage == 'translate' else 'review2'
                self._status = 'awaiting_request'
            else:
                verdict = result['review']
                validate_review(verdict, 2)
                self._review = copy.deepcopy(verdict)
                if not verdict['findings_complete']:
                    self._status, self._failure = 'held', 'Independent review findings are incomplete'
                elif accepted_review(verdict, contract['quality_threshold'], contract_version=2):
                    self._status = 'review_accepted_offline'
                elif self._stage == 'review1' and verdict['findings']:
                    self._stage, self._status = 'correct', 'awaiting_request'
                else:
                    self._status, self._failure = 'held', 'Independent review failed; no further correction is authorized'
        except (ContractError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
            self._status, self._failure = 'held', str(exc)[:500]
        return self.snapshot()

    def snapshot(self):
        snapshot = {'version': PROCESSING_VERSION, 'context': copy.deepcopy(self._context),
            'context_sha256': self._context_sha, 'records': copy.deepcopy(self._records),
            'stage': self._stage, 'status': self._status, 'failure': self._failure,
            'candidate': copy.deepcopy(self._candidate), 'selections': copy.deepcopy(self._selections),
            'selection_proof': copy.deepcopy(self._proof), 'review': copy.deepcopy(self._review),
            'generation_attempts': sum(r['stage'] in ('translate', 'correct') for r in self._records),
            'review_attempts': sum(r['stage'] in ('review1', 'review2') for r in self._records),
            'replayable': self._replayable, 'offline_only': True, 'funding_authorized': False,
            'publication_authorized': False}
        _bounded_selection_input(snapshot, self._context['processing']['maximum_snapshot_bytes'])
        return snapshot

    @classmethod
    def restore(cls, snapshot):
        """Validate exact replay; restoring old evidence never refreshes its TTL."""
        if (not isinstance(snapshot, dict) or snapshot.get('version') != PROCESSING_VERSION
                or snapshot.get('replayable') is not True):
            raise ContractError('Unsupported or unarchivable component cycle snapshot')
        _bounded_selection_input(snapshot, 16000000)
        ctx = snapshot.get('context')
        if (not isinstance(ctx, dict) or set(ctx) != {'source', 'evidence', 'scripture_policy', 'processing'}
                or json_hash(ctx) != snapshot.get('context_sha256')
                or not isinstance(snapshot.get('records'), list) or len(snapshot['records']) > 4):
            raise ContractError('Component cycle context/history changed')
        cycle = cls(ctx['source'], ctx['evidence'], ctx['scripture_policy'], ctx['processing'])
        for saved in snapshot['records']:
            if cycle._status != 'awaiting_request' or not isinstance(saved, dict):
                raise ContractError('Component cycle contains a reset, extra request or incomplete predecessor')
            expected = cycle._make_request(require_fresh=False)
            header = {key: saved.get(key) for key in expected}
            header['response'] = None
            if not _same(header, expected):
                raise ContractError('Archived component request does not match exact frozen inputs')
            cycle._records.append(expected)
            cycle._status = 'awaiting_response'
            if saved.get('response') is not None:
                cycle.receive(saved['response'])
        if not _same(cycle.snapshot(), snapshot):
            raise ContractError('Component cycle response, outcome or attempts differ from replay')
        return cycle
