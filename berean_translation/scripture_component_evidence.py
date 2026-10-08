"""Explicit v2 component evidence, integrated but disabled at runtime admission.

No source plan is inferred. Plans are frozen against one complete source and
every independently associated quotation. Provider reads are anonymous; replay
uses only the exact archived envelopes. Nothing here submits or publishes work.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime

from .common import canonical, json_hash
from .html import validate_translation
from .scripture_components import (COMPONENT_VERSION, MAX_COMPONENT_BYTES,
    decode_component_reference, validate_components, validate_component_pair)
from .scripture_evidence import (ScriptureAttention, APPROVED_EDITIONS,
    _build_associated_evidence, _structural_scope, _prove_scope, _bounded_selection_input)
from .scripture_provider import GetBibleMCP, ScriptureProviderError, ENDPOINT, SDK_VERSION

EVIDENCE_VERSION = '2'


def _hold(reason, detail):
    raise ScriptureAttention(reason, detail)


def validate_component_policy(contract):
    """An explicit new version; default/historical policies never opt in."""
    if (not isinstance(contract, dict) or contract.get('version') != EVIDENCE_VERSION
            or contract.get('source_association_version') not in ('2', '3')
            or contract.get('selection_normalization_version') != '2'
            or contract.get('api_version') != 'v2'):
        _hold('component_contract', 'Component evidence requires the explicit v2 contract')
    expected = {'version', 'api_version', 'max_evidence_bytes', 'edition_map', 'edition_map_provenance',
                'edition_map_sha256', 'max_selection_audit_bytes', 'selection_normalization_version',
                'source_association_version', 'prompt_addendum', 'authored_components', 'max_component_candidate_bytes'}
    provenance = contract.get('edition_map_provenance')
    if (set(contract) != expected or not isinstance(contract.get('prompt_addendum'), str)
            or not contract['prompt_addendum'].strip() or not isinstance(provenance, dict)
            or any(not isinstance(provenance.get(key), str) or not provenance[key]
                   for key in ('source_repository', 'source_commit', 'source_path', 'source_blob_sha',
                               'source_url', 'file_sha256'))
            or provenance.get('api_version') != 'v2' or provenance.get('mcp_endpoint') != ENDPOINT):
        _hold('component_contract', 'Complete frozen policy and approved-map provenance are required')
    plan = contract.get('authored_components')
    if (not isinstance(plan, dict) or set(plan) != {'version', 'source_sha256', 'plans', 'plans_sha256'}
            or plan['version'] != COMPONENT_VERSION
            or not isinstance(plan['source_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['source_sha256'])
            or not isinstance(plan['plans'], list) or len(plan['plans']) > 128):
        _hold('component_contract', 'A bounded, source-bound component plan is required')
    try:
        plan_size = len(canonical(plan))
        plans_hash = json_hash(plan['plans'])
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ScriptureAttention('component_contract', 'Component plan must be finite UTF-8 JSON') from exc
    if plan_size > MAX_COMPONENT_BYTES:
        _hold('component_size_limit', 'Complete frozen source plan exceeds its bound')
    if plan['plans_sha256'] != plans_hash:
        _hold('component_plan_changed', 'Frozen source plan hash differs')
    for item in plan['plans']:
        if (not isinstance(item, dict) or set(item) != {'scope_sha256', 'operations'}
                or not isinstance(item['scope_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', item['scope_sha256'])
                or not isinstance(item['operations'], list)):
            _hold('component_contract', 'Each plan must bind exact scope and operations')
    for field, maximum in (('max_evidence_bytes', 500000), ('max_selection_audit_bytes', 32768),
                           ('max_component_candidate_bytes', 500000)):
        if type(contract.get(field)) is not int or not 0 < contract[field] <= maximum:
            _hold('component_contract', 'A supported complete evidence/selection bound is required')
    mapping = contract.get('edition_map')
    if (not isinstance(mapping, dict) or not isinstance(mapping.get('locales'), dict)
            or any(not isinstance(value, dict) for value in mapping['locales'].values())
            or {key:value.get('abbreviation') for key,value in mapping['locales'].items()} != APPROVED_EDITIONS
            or json_hash(mapping) != contract.get('edition_map_sha256')):
        _hold('edition_map_changed', 'Component evidence cannot use an alternate approved-edition map')
    return contract


def component_policy(root, source, plans, *, source_association_version='2'):
    """Construct an explicit offline policy from supplied plans, never infer one."""
    from .scripture_evidence import policy
    result = policy(root)
    result['version'] = EVIDENCE_VERSION
    result['source_association_version'] = source_association_version
    result['max_component_candidate_bytes'] = 500000
    result['authored_components'] = {'version': COMPONENT_VERSION,
        'source_sha256': json_hash(source), 'plans': copy.deepcopy(plans), 'plans_sha256': json_hash(plans)}
    validate_component_policy(result)
    return result


def source_plans(source, scopes, contract):
    validate_component_policy(contract)
    plan = contract['authored_components']
    if plan['source_sha256'] != json_hash(source):
        _hold('component_source_changed', 'Plan belongs to a different complete English snapshot')
    if ([item['scope_sha256'] for item in plan['plans']] != [json_hash(scope) for scope in scopes]):
        _hold('component_scope_changed', 'Plans must cover every independently associated scope in order')
    return plan['plans']


def verify_provider_envelope(envelope, edition, book, chapter):
    """Reapply the approved adapter's identity checks without network access."""
    arguments = {'translation': edition, 'book': book, 'chapter': chapter, 'api_version': 'v2'}
    try:
        result_hash = json_hash(envelope.get('result')) if isinstance(envelope, dict) else None
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ScriptureAttention('component_provider_changed', 'Provider envelope must be finite UTF-8 JSON') from exc
    if (not isinstance(envelope, dict) or envelope.get('tool') != 'get_scripture'
            or envelope.get('arguments') != arguments or envelope.get('endpoint') != ENDPOINT
            or envelope.get('client_version') != SDK_VERSION or not isinstance(envelope.get('result'), dict)
            or envelope.get('result_sha256') != result_hash):
        _hold('component_provider_changed', 'Provider envelope identity or exact result hash differs')
    try:
        result = envelope['result']
        expected_url = f'https://api.getbible.net/v2/{edition}/{book}/{chapter}.json'
        if result.get('source', {}).get('url') != expected_url:
            raise ValueError('Unexpected provider source URL')
        # Injected adapter callback is an in-memory replay, not a remote lookup.
        GetBibleMCP(lambda name, args: {'structuredContent': copy.deepcopy(result)}).chapter(edition, book, chapter)
        retrieved = datetime.fromisoformat(envelope['retrieved_at'].replace('Z', '+00:00'))
        cache = result['cache']
        expiry = datetime.fromisoformat(cache['expires_at'].replace('Z', '+00:00'))
        if (retrieved.tzinfo is None or expiry.tzinfo is None or expiry <= retrieved
                or cache.get('cacheable') is not True):
            raise ValueError('Unarchivable timestamps')
    except (ScriptureProviderError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ScriptureAttention('component_provider_changed', 'Archived approved-adapter evidence is incomplete') from exc
    return envelope


def source_component_fields(scope, verse, plan):
    reference = decode_component_reference(scope['printed_reference'], version=COMPONENT_VERSION)
    if len(reference['verses']) != 1:
        _hold('component_multi_verse', 'Component evidence currently supports one verse per quotation')
    side = {'verse': verse, 'quote': scope['source_quote'], 'operations': copy.deepcopy(plan['operations'])}
    proof = validate_components(**side, version=COMPONENT_VERSION)
    complete = (scope['source_quote'] == verse
                and all(item['kind'] == 'canonical' for item in side['operations']))
    return {'component_reference': reference, 'component_source': side,
            'component_source_proof': proof,
            'alignment': {'kind': 'complete' if complete else 'components',
                          'independent_review_required': True}}


def build_component_evidence(source, language_tag, contract, provider=None, *, require_fresh=True):
    validate_component_policy(contract)
    return _build_associated_evidence(source, language_tag, contract, provider,
                                     component_contract=True, component_require_fresh=require_fresh)


def validate_component_evidence(source, evidence, contract, *, require_fresh=False):
    """Recompute source/provider/component proof using only archived chapters."""
    validate_component_policy(contract)
    if (not isinstance(evidence, dict) or evidence.get('version') != EVIDENCE_VERSION
            or evidence.get('component_version') != COMPONENT_VERSION
            or not isinstance(evidence.get('lookups'), dict)
            or evidence.get('component_contract_sha256') != json_hash(contract)):
        _hold('component_evidence_changed', 'Evidence and frozen component contracts disagree')
    _bounded_selection_input(evidence, contract['max_evidence_bytes'])
    class CachedProvider:
        def chapter(self, edition, book, chapter):
            value = evidence['lookups'].get(f'{edition}/{book}/{chapter}')
            if value is None:
                _hold('component_provider_changed', 'Complete archived chapter is missing')
            return copy.deepcopy(value)
    rebuilt = build_component_evidence(source, evidence.get('language_tag'), contract,
                                       CachedProvider(), require_fresh=require_fresh)
    rebuilt['created_at'] = evidence.get('created_at')
    if rebuilt != evidence:
        _hold('component_evidence_changed', 'Frozen source/provider/quotation proof differs from replay')
    return evidence


def check_component_selections(evidence, candidate, selections, *, source, contract,
                               max_selection_bytes=32768):
    """Check actual candidate HTML; return review input, never an audit approval."""
    if source is None or contract is None:
        _hold('component_contract', 'Complete source and frozen policy are mandatory')
    validate_component_evidence(source, evidence, contract)
    _bounded_selection_input(selections, min(max_selection_bytes, contract['max_selection_audit_bytes']))
    if not isinstance(selections, list) or len(selections) != len(evidence['quotes']):
        _hold('missing_selections', 'Every component quotation requires exactly one target claim')
    if not isinstance(candidate, dict) or set(candidate) != {'html', 'title', 'subtitle', 'section'}:
        _hold('selection_shape', 'Complete candidate fields are required')
    _bounded_selection_input(candidate, contract['max_component_candidate_bytes'])
    validate_translation(source, candidate)
    scope = _structural_scope(evidence, candidate, source)
    claims = {}
    for selection in selections:
        if (not isinstance(selection, dict)
                or set(selection) != {'quote_id', 'block', 'start', 'end', 'operations'}
                or not isinstance(selection['quote_id'], str) or selection['quote_id'] in claims):
            _hold('selection_shape', 'Invalid or duplicated target component claim')
        claims[selection['quote_id']] = selection
    proofs, intervals = [], {}
    for quote in evidence['quotes']:
        claim = claims.get(quote['id'])
        if claim is None or claim['block'] != quote['block']:
            _hold('selection_location', 'Component quotation moved to a different block')
        block = scope.target.blocks.get(claim['block'], '')
        start, end = claim['start'], claim['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(block):
            _hold('selection_location', 'Invalid complete candidate quotation offsets')
        if (start, end) != scope.proofs[quote['id']][:2]:
            _hold('quote_scope_changed', 'Components must occupy the complete original quotation scope')
        target = {'verse': quote['target_verses'][0]['text'], 'quote': block[start:end],
                  'operations': claim['operations']}
        proof = validate_component_pair(quote['component_source'], target,
            printed_reference=quote['printed_reference'], version=COMPONENT_VERSION)
        if _prove_scope(scope, quote, target['quote']) != (start, end):
            _hold('quote_scope_changed', 'Candidate scope proof disagrees with target components')
        expected = target['quote']
        claimed = {(item['block'], a, b) for item in evidence['quotes']
                   for a, b, value in (scope.proofs[item['id']],) if value == expected}
        actual = set()
        for path, text in scope.target.blocks.items():
            position = text.find(expected)
            while position >= 0:
                actual.add((path, position, position + len(expected)))
                position = text.find(expected, position + 1)
        if actual != claimed:
            _hold('quote_scope_changed', 'Component quotation has an extra unclaimed article occurrence')
        for left, right in intervals.setdefault(claim['block'], []):
            if start < right and left < end:
                _hold('selection_overlap', 'Different quotations cannot claim the same scope')
        intervals[claim['block']].append((start, end))
        proofs.append({'quote_id': quote['id'], 'proof': proof})
    return {'version': EVIDENCE_VERSION, 'component_version': COMPONENT_VERSION,
            'source_sha256': json_hash(source), 'candidate_sha256': json_hash(candidate),
            'evidence_sha256': json_hash(evidence), 'selections_sha256': json_hash(selections),
            'proofs': proofs, 'independent_review_required': True, 'runtime_admission_supported': False}


def reject_component_runtime(contract):
    if contract and contract.get('version') == EVIDENCE_VERSION:
        _hold('component_runtime_not_enabled',
              'Component evidence is offline-only until independent review and append-only lifecycle integration')
