"""Bounded Jacques inspection and immutable selection through the ordinary queue.

Inspection only reads repository State. Provider chapters and plan artifacts may
be cached outside the repository. Neither a plan nor its cost projection admits
work; the existing gated collector is the sole admission and funding authority.
"""
from __future__ import annotations

import copy
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import manual_admission, scripture_component_runtime as runtime
from .common import ContractError, canonical, json_hash, loads, write_json
from .scripture_admission_revisions import inspect_never_paid_hold
from .scripture_association import associate
from .scripture_component_evidence import (component_policy, build_component_evidence,
    validate_component_evidence, verify_provider_envelope, source_component_fields)
from .scripture_component_processing import processing_contract
from .scripture_evidence import ScriptureAttention, APPROVED_EDITIONS
from .scripture_provider import GetBibleMCP
from .state import State

VERSION = '1'
OPERATION = 'scripture-components'
CAMPAIGN_ID = 'gh-37439892859'
ARTICLE_ID = '069fc797-01e4-44d3-8b71-1c07f0821965'
SOURCE_SHA256 = 'ad31a29abcaa9fc4133d05837032668a19ddc92690fa0c75fc335b4adf6d6cb3'
LANGUAGES = ('afr', 'deu', 'ell', 'fra', 'heb', 'ita', 'kor', 'nld', 'nob', 'por', 'rus', 'swe')
MAX_PACKAGE_BYTES = 8_000_000
MAX_CHAPTER_BYTES = 500_000
_MISSING = object()
WORKFLOW = '.github/workflows/ai-scripture-components.yml'
CHAPTER_LENGTHS = {(40, 5): 48, (44, 24): 27, (60, 3): 22,
                   (45, 7): 25, (41, 16): 20, (45, 2): 29}


def _hash(value):
    return {**value, 'sha256': json_hash(value)}


def _check_hash(value, fields, label):
    try:
        valid = (isinstance(value, dict) and set(value) == set(fields) | {'sha256'}
                 and value['sha256'] == json_hash({k:v for k,v in value.items() if k != 'sha256'}))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        valid = False
    if not valid:
        raise ContractError(label + ' is malformed or changed')


def external_path(root, value):
    path = Path(value).resolve()
    root = Path(root).resolve()
    if path == root or path.is_relative_to(root) or root.is_relative_to(path):
        raise ContractError('Inspection artifacts and provider caches must be outside the repository')
    return path


def _complete_chapter(value, edition, book, chapter):
    value = verify_provider_envelope(value, edition, book, chapter)
    if ((book, chapter) not in CHAPTER_LENGTHS
            or (edition != 'kjv' and (book, chapter) != (45, 2))
            or [verse['verse'] for verse in value['result']['data']['verses']] !=
               list(range(1, CHAPTER_LENGTHS[book, chapter] + 1))):
        raise ScriptureAttention('incomplete_chapter', 'Complete ordered Jacques chapter inventory is required')
    retrieved = datetime.fromisoformat(value['retrieved_at'].replace('Z', '+00:00'))
    if retrieved > datetime.now(timezone.utc):
        raise ScriptureAttention('component_provider_changed', 'Provider retrieval timestamp is in the future')
    try:
        result = value['result']
        fetched = datetime.fromisoformat(result['source']['fetched_at'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(result['cache']['expires_at'].replace('Z', '+00:00'))
        retention = result['cache'].get('max_retention_seconds', 30 * 24 * 60 * 60)
        # The adapter records retrieval to whole seconds; provider timestamps
        # can retain fractions of that same second. This does not renew TTL.
        if (fetched.tzinfo is None or fetched > retrieved + timedelta(seconds=1)
                or type(retention) is not int or not 0 < retention <= 30 * 24 * 60 * 60
                or expires > fetched + timedelta(seconds=retention)):
            raise ValueError('Invalid original provider cache lifetime')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ScriptureAttention('component_provider_changed',
            'Chapter expiry must honor its original provider fetch and bounded retention') from exc
    return value


def _cached_chapter(path):
    """Never read an unbounded or special file from an external evidence cache."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return _MISSING
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ContractError('Archived chapter must be a bounded regular file')
        raw = stream.read(MAX_CHAPTER_BYTES + 1)
    if len(raw) > MAX_CHAPTER_BYTES:
        raise ScriptureAttention('evidence_size_limit', 'Complete archived chapter exceeds its byte bound')
    return loads(raw)


class _CompleteProvider:
    """Freeze and check every whole chapter before any per-language plan exists."""
    def __init__(self, provider):
        self.provider, self.memory = provider, {}

    def chapter(self, edition, book, chapter):
        key = (edition, book, chapter)
        if key not in self.memory:
            self.memory[key] = copy.deepcopy(_complete_chapter(
                self.provider.chapter(edition, book, chapter), edition, book, chapter))
        return copy.deepcopy(self.memory[key])


class ChapterProvider:
    """Reuse full approved envelopes; fetching is explicit and never uses a model."""
    def __init__(self, root, directory=None, *, fetch=False):
        self.directory = external_path(root, directory) if directory is not None else None
        self.fetch = fetch
        self.memory = {}

    def chapter(self, edition, book, chapter):
        allowed = ((edition == 'kjv' and (book, chapter) in
                    {(40, 5), (44, 24), (60, 3), (45, 7), (41, 16), (45, 2)})
                   or (edition in set(APPROVED_EDITIONS.values()) - {None} and (book, chapter) == (45, 2)))
        if not allowed:
            raise ScriptureAttention('unsupported_inspection_lookup', 'Lookup is outside the bounded Jacques chapters')
        key = f'{edition}-{book}-{chapter}'
        if key not in self.memory:
            path = self.directory / (key + '.json') if self.directory else None
            value = _cached_chapter(path) if path else _MISSING
            if value is _MISSING:
                if not self.fetch:
                    raise ScriptureAttention('evidence_missing', 'Complete approved chapter is missing: ' + key)
                value = GetBibleMCP().chapter(edition, book, chapter)
                _complete_chapter(value, edition, book, chapter)
                if path:
                    write_json(path, value)
            self.memory[key] = _complete_chapter(value, edition, book, chapter)
        return copy.deepcopy(self.memory[key])


class _ReadOnlyState(State):
    def __init__(self, state, inventory):
        super().__init__(state.root)
        self.inventory = inventory
        if hasattr(state, 'component_gitstore'):
            self.component_gitstore = state.component_gitstore
        if hasattr(state, 'gitstore'):
            self.gitstore = state.gitstore

    def read(self, path, default=None):
        return copy.deepcopy(self.inventory) if path == 'state/source.json' else super().read(path, default)

    def write(self, path, value):
        raise ContractError('Read-only component inspection cannot mutate State')


def _current_source(engine, source, inventory=None):
    inventory = inventory if inventory is not None else engine.source_client.discover()
    current = inventory.get('articles', {}).get(ARTICLE_ID)
    if not current or current.get('translation_key') != source['translation_key']:
        raise ContractError('Current English source changed or is no longer eligible')
    snapshot = engine.source_client.snapshot(ARTICLE_ID)
    # Repository revisions may change without changing this article. Everything
    # else in the complete, index-derived snapshot must remain exact.
    if ({k:v for k,v in snapshot.items() if k != 'revision'} !=
            {k:v for k,v in source.items() if k != 'revision'}):
        raise ContractError('Complete current English snapshot differs from the inspected original')
    if snapshot.get('revision') != inventory.get('revision'):
        raise ContractError('Current source discovery is partial or inconsistent')
    return inventory


def plan_jacques(source, verse):
    """One unique literal insertion/excerpt proof; no normalized/semantic matching."""
    if source.get('article', {}).get('id') != ARTICLE_ID or json_hash(source) != SOURCE_SHA256:
        raise ContractError('Only the original complete Jacques source is supported')
    scopes = associate(source['html'], ARTICLE_ID)[2]
    if len(scopes) != 1 or scopes[0]['reference'] != 'Romans 2:4':
        raise ScriptureAttention('unsupported_component_scope', 'Exactly one Romans 2:4 quotation is required')
    quote = scopes[0]['source_quote']
    if (not isinstance(verse, str) or not quote.startswith('…') or quote.count('…') != 1
            or quote.count('[that]') != 1 or re.search(r'[\[\]{}]', quote.replace('[that]', ''))):
        raise ScriptureAttention('unsupported_component_primitive', 'Only the printed leading ellipsis and literal [that] insertion are supported')
    left, right = quote[1:].split('[that]')
    # The one intervening space belongs to the bracketed insertion. Every
    # canonical code point remains literal; no punctuation or whitespace repair.
    if not left or not right.startswith(' ') or not right[1:]:
        raise ScriptureAttention('ambiguous_component_alignment', 'Insertion must have two nonempty exact canonical flanks')
    retained = left + right[1:]
    positions = [m.start() for m in re.finditer('(?=' + re.escape(retained) + ')', verse)]
    if len(positions) != 1:
        raise ScriptureAttention('ambiguous_component_alignment', 'Printed canonical extent has no unique exact verse match')
    start, end = positions[0], positions[0] + len(retained)
    at = start + len(left)
    def part(kind, a, b):
        result = {'kind': kind, 'start': a, 'end': b, 'text': verse[a:b]}
        if kind == 'omit': result['reason'] = 'excerpt_boundary'
        return result
    operations = ([part('omit', 0, start)] if start else [])
    operations += [{'kind': 'punctuation', 'id': 'a1', 'at': start, 'authored_text': '…'},
                   part('canonical', start, at),
                   {'kind': 'insert', 'id': 'a2', 'at': at, 'authored_text': '[that] '},
                   part('canonical', at, end)]
    if end < len(verse): operations.append(part('omit', end, len(verse)))
    plan = {'scope_sha256': json_hash(scopes[0]), 'operations': operations}
    source_component_fields(scopes[0], verse, plan)
    return [plan]


def _processing(config, campaign, entry, policy):
    projected = copy.deepcopy(campaign)
    projected['scripture_quotes'] = copy.deepcopy(policy)
    task = {**copy.deepcopy(entry['provenance']['task']), 'models': copy.deepcopy(campaign['models'])}
    value = processing_contract(config.root, projected, task,
                               maximum_result_bytes=config.runtime['max_result_bytes'])
    for key in ('offline_only', 'funding_authorized', 'publication_authorized'):
        value.pop(key)
    value['runtime_protocol_version'] = runtime.VERSION
    # Mutable sibling progress is deliberately not a new model/prompt contract.
    projection = {k:v for k,v in value.items() if k != 'origin_campaign_sha256'}
    return runtime._stage_budget(value), json_hash(projection)


def _policy(root, source, verse):
    return component_policy(root, source, plan_jacques(source, verse),
                            source_association_version='3')


def _entry_contract(config, campaign, source, planned):
    """Reject changed target/policy projections before any admission write."""
    language = campaign['language_settings'].get(planned['language'])
    evidence = planned['evidence']
    if not language or evidence['language_tag'] != language['tag']:
        raise ContractError('Component evidence language differs from the original manual target')
    verse = evidence['quotes'][0]['english_verses'][0]['text']
    if planned['scripture_policy'] != _policy(config.root, source, verse):
        raise ContractError('Component policy differs from the bounded Jacques inspection contract')


def inspect_jacques(engine, provider=None):
    """Return a no-funding, no-admission package and exact per-entry holds."""
    state = engine.state
    campaign = state.read(f'state/campaigns/{CAMPAIGN_ID}.json')
    ledger = state.read(manual_admission.path(CAMPAIGN_ID))
    if not campaign or not ledger:
        raise ContractError('Original Jacques manual campaign and admission ledger are required')
    source = state.read(f'state/sources/{SOURCE_SHA256}.json')
    if not source or json_hash(source) != SOURCE_SHA256:
        raise ContractError('Original complete Jacques source snapshot is missing or changed')
    inventory = _current_source(engine, source)
    reader = copy.copy(engine)
    reader.state = _ReadOnlyState(state, inventory)
    provider = _CompleteProvider(provider or ChapterProvider(engine.config.root))
    entries, holds = [], []
    candidates = sorted((identity, entry) for identity, entry in ledger.get('entries', {}).items()
                        if entry.get('item', {}).get('article_id') == ARTICLE_ID
                        and entry['item'].get('language') in LANGUAGES)
    if len(candidates) > len(LANGUAGES) or len({e['item']['language'] for _,e in candidates}) != len(candidates):
        raise ContractError('Duplicate Jacques language admission entries')
    for entry_id, entry in candidates:
        language = entry['item']['language']
        try:
            inspected = inspect_never_paid_hold(reader, CAMPAIGN_ID, entry_id)
            runtime.human_ownership_guard(reader.state, entry['provenance']['task'])
            if json_hash(inspected['source']) != SOURCE_SHA256:
                raise ContractError('Entry is not bound to the original Jacques snapshot')
            english = _complete_chapter(provider.chapter('kjv', 45, 2), 'kjv', 45, 2)
            verses = [v['text'] for v in english['result']['data']['verses'] if v['verse'] == 4]
            if len(verses) != 1:
                raise ScriptureAttention('evidence_missing', 'Complete Romans 2:4 is absent from the approved English chapter')
            policy = _policy(engine.config.root, source, verses[0])
            evidence = build_component_evidence(source, campaign['language_settings'][language]['tag'], policy, provider)
            validate_component_evidence(source, evidence, policy, require_fresh=True)
            budget, processing_sha = _processing(engine.config, campaign, entry, policy)
            if runtime.committed_total(state, campaign) + Decimal(str(budget['total_reserved_usd'])) > Decimal(str(campaign['budget_usd'])):
                raise ContractError('Complete component cycle does not fit the original manual envelope')
            entries.append(_hash({'entry_id': entry_id, 'language': language,
                'origin': inspected['origin'], 'scripture_policy': policy, 'evidence': evidence,
                'stage_budget': budget, 'processing_sha256': processing_sha}))
        except (ContractError, OSError, KeyError, TypeError) as exc:
            holds.append({'entry_id': entry_id, 'language': language,
                          'reason': getattr(exc, 'reason', 'inspection_guard'), 'detail': str(exc)})
    package = _hash({'version': VERSION, 'kind': 'jacques-component-inspection',
        'campaign_id': CAMPAIGN_ID, 'article_id': ARTICLE_ID, 'source_sha256': SOURCE_SHA256,
        'source': source, 'current_source_revision': inventory['revision'],
        'entries': entries, 'holds': holds, 'funding_allocated': False,
        'publication_ready': False, 'selected_entry_ids': []})
    validate_package(package)
    return package


def validate_package(package, *, require_fresh=False):
    try:
        size = len(canonical(package))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError('Component package must be finite bounded UTF-8 JSON') from exc
    if size > MAX_PACKAGE_BYTES:
        raise ContractError('Complete component package exceeds its bounded size')
    _check_hash(package, {'version', 'kind', 'campaign_id', 'article_id', 'source_sha256', 'source',
        'current_source_revision', 'entries', 'holds', 'funding_allocated', 'publication_ready',
        'selected_entry_ids'}, 'Component inspection package')
    if (package['version'] != VERSION or package['kind'] != 'jacques-component-inspection'
            or package['campaign_id'] != CAMPAIGN_ID or package['article_id'] != ARTICLE_ID
            or package['source_sha256'] != SOURCE_SHA256 or json_hash(package['source']) != SOURCE_SHA256
            or package['funding_allocated'] is not False or package['publication_ready'] is not False
            or package['selected_entry_ids'] != [] or not isinstance(package['entries'], list)
            or len(package['entries']) > len(LANGUAGES) or not isinstance(package['holds'], list)
            or len(package['holds']) > len(LANGUAGES)
            or not isinstance(package['current_source_revision'], str)
            or not re.fullmatch('[0-9a-f]{40}', package['current_source_revision'])):
        raise ContractError('Unsupported or unbounded component inspection package')
    ids, languages = set(), set()
    for row in [*package['entries'], *package['holds']]:
        if (not isinstance(row, dict) or not isinstance(row.get('entry_id'), str)
                or not re.fullmatch('[0-9a-f]{32}', row['entry_id']) or row.get('language') not in LANGUAGES
                or row['entry_id'] in ids or row['language'] in languages):
            raise ContractError('Component package has duplicate or unsupported entries')
        ids.add(row['entry_id']); languages.add(row['language'])
    for entry in package['entries']:
        _check_hash(entry, {'entry_id', 'language', 'origin', 'scripture_policy', 'evidence',
                           'stage_budget', 'processing_sha256'}, 'Component plan entry')
        origin = entry['origin']
        origin_fields = {'campaign_id', 'entry_id', 'request_sha256', 'campaign_contract_sha256',
            'entry_sha256', 'attention_event_sha256', 'source_sha256', 'record_sha256',
            'task_template_sha256', 'models_sha256', 'budget_usd'}
        if (not isinstance(origin, dict) or set(origin) != origin_fields
                or origin.get('campaign_id') != CAMPAIGN_ID or origin.get('entry_id') != entry['entry_id']
                or origin.get('source_sha256') != SOURCE_SHA256
                or any(not isinstance(origin[key], str) or not re.fullmatch('[0-9a-f]{64}', origin[key])
                       for key in origin_fields if key.endswith('_sha256'))
                or not isinstance(entry['processing_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', entry['processing_sha256'])
                or not isinstance(entry['stage_budget'], dict)):
            raise ContractError('Component entry origin differs from the bounded original')
        evidence, policy = entry['evidence'], entry['scripture_policy']
        if (not isinstance(evidence, dict) or not isinstance(evidence.get('lookups'), dict)
                or not isinstance(policy, dict) or policy.get('source_association_version') != '3'):
            raise ContractError('Complete explicitly versioned component evidence is required')
        for identity, envelope in evidence.get('lookups', {}).items():
            if not isinstance(envelope, dict) or not isinstance(envelope.get('arguments'), dict):
                raise ContractError('Archived chapter envelope is malformed')
            arguments = envelope.get('arguments', {})
            if identity != f'{arguments.get("translation")}/{arguments.get("book")}/{arguments.get("chapter")}':
                raise ContractError('Archived chapter lookup identity changed')
            _complete_chapter(envelope, arguments.get('translation'), arguments.get('book'), arguments.get('chapter'))
        validate_component_evidence(package['source'], evidence, policy, require_fresh=require_fresh)
        if len(evidence['quotes']) != 1 or evidence['quotes'][0]['reference'] != 'Romans 2:4':
            raise ContractError('Only the original Jacques quotation is supported')
        plans = plan_jacques(package['source'], evidence['quotes'][0]['english_verses'][0]['text'])
        if policy['authored_components']['plans'] != plans:
            raise ContractError('Component plan differs from deterministic Jacques inspection')
    for hold in package['holds']:
        if set(hold) != {'entry_id', 'language', 'reason', 'detail'} or any(
                not isinstance(hold[k], str) or not hold[k] for k in ('reason', 'detail')):
            raise ContractError('Malformed component hold diagnostic')
    return package


def build_request(package, selected_entry_ids=(), *, request_id, authorization):
    validate_package(package, require_fresh=True)
    request = {'id': request_id, 'operation': OPERATION, 'version': VERSION,
        'package': copy.deepcopy(package), 'package_sha256': package['sha256'],
        'selected_entry_ids': list(selected_entry_ids), 'authorization': copy.deepcopy(authorization)}
    validate_request(None, request)
    return request


def validate_request(config, request):
    if (not isinstance(request, dict) or set(request) != {'id', 'operation', 'version', 'package',
            'package_sha256', 'selected_entry_ids', 'authorization'}
            or request['operation'] != OPERATION or request['version'] != VERSION):
        raise ContractError('Unsupported component queue request')
    validate_package(request['package'])
    selected = request['selected_entry_ids']
    entries = {e['entry_id']: e for e in request['package']['entries']}
    if (not isinstance(selected, list) or not 1 <= len(selected) <= len(LANGUAGES)
            or any(not isinstance(identity, str) for identity in selected)
            or len(set(selected)) != len(selected) or any(identity not in entries for identity in selected)
            or request['package_sha256'] != request['package']['sha256']):
        raise ContractError('Explicit nonempty exact evidence-backed entry selection is required')
    auth = request['authorization']
    if (not isinstance(auth, dict) or set(auth) != {'kind', 'repository', 'workflow_ref', 'run_id', 'actor'}
            or auth['kind'] != 'github_workflow_dispatch'
            or not isinstance(auth['repository'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', auth['repository'])
            or auth['workflow_ref'] != auth['repository'] + '/' + WORKFLOW + '@refs/heads/main'
            or not isinstance(auth['run_id'], str) or not re.fullmatch(r'[0-9]{1,30}', auth['run_id'])
            or not isinstance(auth['actor'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', auth['actor'])
            or request['id'] != 'component-gh-' + auth['run_id']):
        raise ContractError('Component selection requires exact trusted main workflow_dispatch provenance')
    return request


def _matches_revision(revision, planned):
    return (revision['origin'] == planned['origin']
        and revision['scripture_policy'] == planned['scripture_policy']
        and revision['evidence_sha256'] == json_hash(planned['evidence'])
        and revision['stage_budget'] == planned['stage_budget']
        and json_hash({k:v for k,v in revision['processing'].items()
                      if k != 'origin_campaign_sha256'}) == planned['processing_sha256'])


def request_materialized(state, request):
    """The original entry/task histories are the receipt; never add a ledger."""
    validate_request(None, request)
    campaign = state.read(f'state/campaigns/{CAMPAIGN_ID}.json')
    ledger = state.read(manual_admission.path(CAMPAIGN_ID))
    if not campaign or not ledger:
        return False
    planned = {entry['entry_id']: entry for entry in request['package']['entries']}
    for identity in request['selected_entry_ids']:
        entry = ledger.get('entries', {}).get(identity)
        if not entry or 'component_revision' not in entry:
            return False
        revision = runtime.validate_revision(state, campaign, entry)
        if not _matches_revision(revision, planned[identity]):
            raise ContractError('Existing component revision conflicts with selected immutable package')
        if not runtime.materialized(state, campaign, entry):
            return False
    return True


def accept_request(engine, request):
    """Recheck the immutable selected package, then use existing Engine admission."""
    validate_request(engine.config, request)
    runtime.require_enabled(engine.config)
    recorded = engine.state.read(f'state/queue/{request["id"]}.json')
    if recorded != request:
        raise ContractError('Component selection must be persisted unchanged in the ordinary immutable queue')
    package = request['package']
    _current_source(engine, package['source'])
    campaign = engine.state.read(f'state/campaigns/{CAMPAIGN_ID}.json')
    ledger = engine.state.read(manual_admission.path(CAMPAIGN_ID))
    if not campaign or not ledger or campaign.get('cancel_requested'):
        raise ContractError('Original manual campaign is missing or cancelled')
    manual_admission.validate(engine.state, campaign, ledger)
    entries = {e['entry_id']: e for e in package['entries']}
    selected, additional = [], Decimal(0)
    # Validate every selected item before the first write. An interrupted commit
    # can resume through its exact existing component revision, never a new ID.
    for identity in request['selected_entry_ids']:
        planned = entries[identity]
        entry = ledger['entries'].get(identity)
        if not entry or entry['item']['language'] != planned['language'] or entry['item']['article_id'] != ARTICLE_ID:
            raise ContractError('Selected original admission entry changed')
        _entry_contract(engine.config, campaign, package['source'], planned)
        runtime.human_ownership_guard(engine.state, entry['provenance']['task'])
        if engine.human_protected(planned['language'], ARTICLE_ID):
            raise ContractError('Human-reviewed or edited work excludes component admission')
        revision = entry.get('component_revision')
        if revision:
            runtime.validate_revision(engine.state, campaign, entry)
            if not _matches_revision(revision, planned):
                raise ContractError('Existing component revision conflicts with selected immutable package')
        else:
            inspected = inspect_never_paid_hold(engine, CAMPAIGN_ID, identity)
            if inspected['origin'] != planned['origin']:
                raise ContractError('Selected original entry/source/funding provenance changed')
            validate_component_evidence(package['source'], planned['evidence'], planned['scripture_policy'], require_fresh=True)
            budget, processing_sha = _processing(engine.config, campaign, entry, planned['scripture_policy'])
            if budget != planned['stage_budget'] or processing_sha != planned['processing_sha256']:
                raise ContractError('Selected frozen model, prompt or complete-cycle projection changed')
            additional += Decimal(str(budget['total_reserved_usd']))
        selected.append(planned)
    if runtime.committed_total(engine.state, campaign) + additional > Decimal(str(campaign['budget_usd'])):
        raise ScriptureAttention('component_original_budget', 'Selected complete cycles do not fit the original remaining envelope')
    for planned in selected:
        engine.accept_component_revision(CAMPAIGN_ID, planned['entry_id'], planned['scripture_policy'], planned['evidence'])
    return {'request': request['id'], 'campaign': CAMPAIGN_ID,
            'selected_entry_ids': list(request['selected_entry_ids']), 'publication_ready': False}
