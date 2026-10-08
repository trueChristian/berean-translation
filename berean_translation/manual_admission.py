"""Resume only never-paid manual Scripture prefetch, under the original envelope.

Campaign selection and skips remain audit history. This separate v1 ledger owns
admission progress; it is not a new request, retry allowance or funding source.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime
from .common import ContractError, canonical, digest, json_hash, now, read_json
from .scripture_evidence import (ScriptureAttention, adopt_scripture_selection_audit,
                                  freeze_scripture_evidence)
from .state import TERMINAL
from . import plain_policy

VERSION = 1
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 6
MAX_ATTENTION_DETAIL_BYTES = 2048
TEMPORARY = 'prefetch_wait_budget'
MUTABLE = {'tasks', 'skipped', 'status', 'task_counts', 'reserved_usd',
           'reported_usage_usd', 'accounted_responses', 'cancel_requested', 'admission_counts'}
UNFINISHED = {'pending', 'ready', 'attention'}
SCRIPTURE_HOLDS = {
    'prefetch_wait_budget', 'awaiting_prefetch', 'ambiguous_source_quote',
    'ambiguous_quote_reference', 'unresolved_quote_reference', 'unmarked_quote_scope',
    'unassociated_source_quote', 'printed_reference_mismatch', 'source_quote_annotation',
    'source_association_limit', 'missing_edition', 'missing_verse', 'unsupported_reference',
    'unverified_versification', 'fetch_failed', 'evidence_size_limit', 'unarchivable_response',
    'invalid_expiry', 'evidence_expired', 'edition_map_changed',
}


def path(identity):
    return f'state/manual-admissions/{identity}.json'


def supported(campaign):
    return (campaign.get('operation') in ('translate', 'review')
            and campaign.get('scripture_quotes') and not campaign.get('dry_run')
            and not any(campaign.get(k) for k in
                        ('autonomous', 'source_refresh', 'recovery_of_campaign', 'downstream_recovery')))


def legacy_targets(campaign):
    if not supported(campaign):
        return []
    return [row for row in campaign.get('skipped', []) if row.get('reason') == TEMPORARY]


def needs_resume(state, campaign, *, config=None):
    ledger = state.read(path(campaign['id']))
    if ledger is not None:
        validate(state, campaign, ledger)
    if ledger:
        from .scripture_component_runtime import materialized
        if any('component_revision' in e and not materialized(state, campaign, e)
               for e in ledger['entries'].values()):
            return True
        settings = config if config is not None else state.read('config/runtime.json', {})
        if plain_policy.enabled(settings) and any(_can_migrate(e) for e in ledger['entries'].values()):
            return True
    return (any(e['status'] in ('pending', 'ready') for e in ledger['entries'].values())
            if ledger else bool(legacy_targets(campaign)))


def contract(campaign):
    return {k: v for k, v in campaign.items() if k not in MUTABLE}


def task_id(campaign, item):
    return digest(campaign['id'] + ':' + item['language'] + ':' + item['article_id'])[:32]


def transition(entry, status, reason):
    if entry.get('status') == status and entry.get('reason') == reason:
        return
    entry.update(status=status, reason=reason)
    entry.setdefault('events', []).append({'status': status, 'reason': reason, 'at': now()})


def _attention_text(message):
    """Bound untrusted diagnostic data; retain visible Unicode, escape controls/markup."""
    marker = ' [truncated]'
    parts, size = [], 0
    for char in message:
        part = (f'\\u{ord(char):04x}' if char in '<>&' else
                char if char.isprintable() else ascii(char)[1:-1])
        size += len(part.encode('utf-8'))
        if size > MAX_ATTENTION_DETAIL_BYTES - len(marker):
            return ''.join(parts) + marker
        parts.append(part)
    return ''.join(parts)


def record_attention_detail(entry, message):
    """Append once at observation time, without modifying existing audit history."""
    if entry['status'] != 'attention' or 'attention_detail' in entry:
        return
    detail = {'text': _attention_text(message),
              'provenance_sha256': entry['provenance_sha256'],
              'event_index': len(entry['events']) - 1,
              'event_sha256': json_hash(entry['events'][-1])}
    entry['attention_detail'] = {**detail, 'sha256': json_hash(detail)}


def _validate_attention_detail(entry):
    if 'attention_detail' not in entry:
        return  # Existing v1 history has no reconstructable diagnostic obligation.
    detail = entry['attention_detail']
    if (not isinstance(detail, dict)
            or set(detail) != {'text', 'provenance_sha256', 'event_index', 'event_sha256', 'sha256'}
            or not isinstance(detail['text'], str) or not detail['text']
            or len(detail['text'].encode('utf-8')) > MAX_ATTENTION_DETAIL_BYTES
            or any(not char.isprintable() or char in '<>&' for char in detail['text'])
            or type(detail['event_index']) is not int
            or not 0 <= detail['event_index'] < len(entry['events'])
            or detail['provenance_sha256'] != entry['provenance_sha256']
            or detail['sha256'] != json_hash({k: v for k, v in detail.items() if k != 'sha256'})):
        raise ContractError('Malformed manual admission attention detail')
    event = entry['events'][detail['event_index']]
    if (entry['provenance'] is None
            or (entry['status'] not in ('attention', 'cancelled') and 'plain_migration' not in entry)
            or event['status'] != 'attention' or event['reason'] == TEMPORARY
            or detail['event_sha256'] != json_hash(event)
            or not detail['text'].startswith(f'Scripture attention: {event["reason"]}: ')):
        raise ContractError('Manual admission attention detail provenance changed')


def save(state, ledger):
    if len(canonical(ledger)) > MAX_LEDGER_BYTES:
        raise ContractError('Manual admission ledger exceeds its bounded size')
    state.write(path(ledger['campaign']), ledger)


def counts(state, campaign):
    ledger = state.read(path(campaign['id']))
    if ledger is None:
        return {'pending': len({(i['language'], i['article_id']) for i in legacy_targets(campaign)})}
    validate(state, campaign, ledger)
    result = {}
    for entry in ledger['entries'].values():
        status = entry['status']
        if 'component_revision' in entry:
            from .scripture_component_runtime import materialized
            status = 'admitted' if materialized(state, campaign, entry) else 'ready'
        result[status] = result.get(status, 0) + 1
    return result


def claims(state, *, include_attention=False):
    """Build once per discovery/selection pass; never retain a cross-tick cache."""
    result = set()
    for campaign in state.campaigns():
        if campaign.get('cancel_requested') or not supported(campaign):
            continue
        ledger = state.read(path(campaign['id']))
        if ledger is None:
            sources = {}
            for item in legacy_targets(campaign):
                article_id = item['article_id']
                if article_id not in sources:
                    try:
                        _, source = _legacy_source(state, campaign, item)
                        sources[article_id] = source['translation_key']
                    except (ContractError, OSError, UnicodeError):
                        sources[article_id] = None
                result.add((item['language'], article_id, sources[article_id]))
            continue
        validate(state, campaign, ledger)
        for entry in ledger['entries'].values():
            if entry['status'] in ({'pending', 'ready', 'attention'} if include_attention else {'pending', 'ready'}):
                proof = entry['provenance']
                if entry['status'] == 'attention' and attention_superseded(state, campaign, entry):
                    continue
                result.add((entry['item']['language'], entry['item']['article_id'],
                            proof['task']['translation_key'] if proof else None))
    return result


def covered(state, language, article_id, translation_key, *, claim_index=None, include_attention=False):
    index = claims(state, include_attention=include_attention) if claim_index is None else claim_index
    return (language, article_id, translation_key) in index or (language, article_id, None) in index


def _template(campaign, item, source_path, source, record, previous):
    pub = record.get('published')
    return {'id': task_id(campaign, item), 'campaign': campaign['id'],
            'article_id': item['article_id'], 'issue_id': source['article']['issue_id'],
            'language': item['language'], 'translation_key': source['translation_key'],
            'source_snapshot': source_path, 'created_at': campaign['created_at'],
            'status': 'queued', 'stage': 'review1' if campaign['operation'] == 'review' else 'translate',
            'model': campaign['model'], 'review_model': campaign['review_model'],
            'protected': bool(pub and pub['human_reviewed']),
            'base_html_sha256': pub['html_sha256'] if pub else None,
            'base_metadata_sha256': pub['metadata_sha256'] if pub else None,
            'translation_model_actual': pub['model'] if pub else (previous or {}).get('translation_model_actual'),
            'translation_attempts': 0, 'review_attempts': 0, 'events': [],
            'manual_admission_version': VERSION}


def _legacy_source(state, campaign, item):
    matches = []
    for candidate in sorted((state.root / 'state/sources').glob('*.json')):
        source = read_json(candidate)
        if (source.get('revision') == campaign['source_revision']
                and source.get('article', {}).get('id') == item['article_id']):
            relative = candidate.relative_to(state.root).as_posix()
            # A corrupted matching snapshot invalidates the proof, not just the
            # one file. Never silently choose a different candidate.
            state.source({'source_snapshot': relative})
            matches.append((relative, source))
    if len(matches) != 1:
        raise ContractError('legacy_source_snapshot_missing_or_ambiguous')
    return matches[0]


def _legacy_baseline(campaign, record, previous):
    try:
        cutoff = datetime.fromisoformat(campaign['created_at'])
        if cutoff.tzinfo is None:
            raise ValueError('naive time')
    except (ValueError, TypeError):
        raise ContractError('legacy_record_provenance_unproven')
    if record.get('latest_task') and previous is None:
        raise ContractError('legacy_latest_task_missing')
    events = record.get('history', [])
    dates = [event.get('at') for event in events]
    if previous:
        dates.extend([previous.get('created_at'), previous.get('finished_at')])
        if previous.get('status') not in TERMINAL:
            raise ContractError('legacy_latest_task_changed_or_active')
    pub = record.get('published')
    if pub:
        dates.append(pub.get('published_at'))
    try:
        parsed = [datetime.fromisoformat(date) for date in dates]
        if any(date.tzinfo is None or date >= cutoff for date in parsed):
            raise ValueError('ambiguous or later record time')
    except (ValueError, TypeError):
        raise ContractError('legacy_record_provenance_unproven')


def initialize(engine, campaign, request, *, legacy=False):
    state = engine.state
    if campaign.get('request_sha256') != json_hash(request):
        raise ContractError('Manual admission requires the original immutable request hash')
    ledger = {'version': VERSION, 'campaign': campaign['id'], 'request': copy.deepcopy(request),
              'request_sha256': campaign['request_sha256'],
              'campaign_sha256': json_hash(contract(campaign)),
              'initial_campaign': copy.deepcopy(campaign), 'legacy': legacy, 'entries': {}}
    items = legacy_targets(campaign) if legacy else campaign['selection']
    selected = {(i['language'], i['article_id']) for i in campaign['selection']}
    sources = {}
    legacy_contract_error = None
    if legacy:
        try:
            original_contract(campaign, request)
        except ContractError as exc:
            legacy_contract_error = str(exc)
    for item in items:
        identity = task_id(campaign, item)
        if identity in ledger['entries']:
            continue
        entry = {'task_id': identity, 'item': {'language': item['language'], 'article_id': item['article_id']},
                 'provenance': None, 'provenance_sha256': json_hash(None), 'events': []}
        ledger['entries'][identity] = entry
        try:
            if legacy_contract_error:
                raise ContractError(legacy_contract_error)
            if legacy and campaign['operation'] != 'translate':
                raise ContractError('legacy_review_candidate_provenance_unproven')
            if (item['language'], item['article_id']) not in selected:
                raise ContractError('legacy_target_not_in_original_selection')
            if legacy and any(i['language'] == item['language'] and i['article_id'] == item['article_id']
                              and i['reason'] != TEMPORARY for i in campaign['skipped']):
                raise ContractError('legacy_target_has_non_temporary_hold')
            if legacy:
                if item['article_id'] not in sources:
                    sources[item['article_id']] = _legacy_source(state, campaign, item)
                source_path, source = sources[item['article_id']]
            else:
                source = engine.source_client.snapshot(item['article_id'])
                source_path = f'state/sources/{json_hash(source)}.json'
                state.write(source_path, source)
            if source['revision'] != campaign['source_revision'] or source['article']['id'] != item['article_id']:
                raise ContractError('original_source_identity_changed')
            record = state.record(item['language'], item['article_id'])
            previous = state.read(f'state/tasks/{record["latest_task"]}/task.json') if record.get('latest_task') else None
            if legacy:
                _legacy_baseline(campaign, record, previous)
            candidate = None
            if campaign['operation'] == 'review':
                pub = record.get('published')
                candidate = state.publication_candidate(pub)[0] if pub else state.candidate(previous)
                if candidate is None:
                    raise ContractError('original_review_candidate_missing')
            candidate_path = None
            if candidate is not None:
                candidate_path = f'state/manual-admission-candidates/{json_hash(candidate)}.json'
                state.write(candidate_path, candidate)
            provenance = {'task': _template(campaign, item, source_path, source, record, previous),
                          'record_sha256': json_hash(record),
                          'previous_task_id': previous['id'] if previous else None,
                          'previous_task_sha256': json_hash(previous),
                          'candidate_path': candidate_path, 'candidate_sha256': json_hash(candidate)}
            entry.update(provenance=provenance, provenance_sha256=json_hash(provenance))
            transition(entry, 'pending', TEMPORARY if legacy else
                       'awaiting_translation' if plain_policy.is_plain(campaign) else 'awaiting_prefetch')
        except (ContractError, UnicodeError) as exc:
            transition(entry, 'attention', str(exc))
    save(state, ledger)
    state.save_campaign(campaign)
    engine.checkpoint('runtime: freeze manual admission provenance before Scripture prefetch')
    return ledger


def _validate(state, campaign, ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('entries'), dict)
            or len(canonical(ledger)) > MAX_LEDGER_BYTES):
        raise ContractError('Malformed manual admission ledger')
    if (ledger.get('version') != VERSION or ledger.get('campaign') != campaign['id']
            or ledger.get('request_sha256') != campaign.get('request_sha256')
            or ledger.get('request_sha256') != json_hash(ledger.get('request'))
            or ledger.get('campaign_sha256') != json_hash(contract(campaign))
            or not isinstance(ledger.get('initial_campaign'), dict)
            or ledger.get('campaign_sha256') != json_hash(contract(ledger['initial_campaign']))):
        raise ContractError('Manual admission frozen request or campaign changed')
    queued = state.read(f'state/queue/{campaign["id"]}.json')
    if queued is not None and json_hash(queued) != ledger['request_sha256']:
        raise ContractError('Manual admission queue request changed')
    targets = legacy_targets(campaign) if ledger.get('legacy') is True else campaign['selection']
    expected = {task_id(campaign, item) for item in targets}
    target_pairs = {(i['language'], i['article_id']) for i in targets}
    if type(ledger.get('legacy')) is not bool or set(ledger['entries']) != expected:
        raise ContractError('Manual admission changed its original target set')
    for identity, entry in ledger['entries'].items():
        if (not isinstance(entry, dict) or not isinstance(entry.get('item'), dict)
                or set(entry['item']) != {'language', 'article_id'}
                or (entry['item']['language'], entry['item']['article_id']) not in target_pairs
                or not isinstance(entry.get('events'), list)
                or not 1 <= len(entry['events']) <= MAX_EVENTS + (3 if 'plain_migration' in entry else 0)
                or any(not isinstance(event, dict) or set(event) != {'status', 'reason', 'at'}
                       or not all(isinstance(v, str) and len(v) <= 4096 for v in event.values())
                       for event in entry['events'])
                or not isinstance(entry.get('reason'), str) or len(entry['reason']) > 4096
                or entry['events'][-1].get('status') != entry.get('status')
                or entry['events'][-1].get('reason') != entry.get('reason')
                or not {'provenance', 'provenance_sha256', 'task_id', 'status'} <= entry.keys()):
            raise ContractError('Malformed manual admission target')
        if (identity != entry['task_id'] or identity != task_id(campaign, entry['item'])
                or entry['provenance_sha256'] != json_hash(entry['provenance'])
                or entry['status'] not in UNFINISHED | {'admitted', 'cancelled'}):
            raise ContractError('Manual admission target provenance changed')
        _validate_attention_detail(entry)
        if 'plain_migration' in entry:
            _validate_plain_migration(campaign, entry)
        if 'component_revision' in entry:
            from .scripture_component_runtime import validate_revision
            validate_revision(state, campaign, entry)
        proof = entry['provenance']
        if proof is None:
            if entry['status'] not in ('attention', 'cancelled'):
                raise ContractError('Manual admission lacks original provenance')
        else:
            if (not isinstance(proof, dict) or set(proof) != {'task', 'record_sha256',
                    'previous_task_id', 'previous_task_sha256', 'candidate_path', 'candidate_sha256'}
                    or not isinstance(proof['task'], dict)):
                raise ContractError('Malformed manual admission provenance')
            task = proof['task']
            fixed = {'id':identity, 'campaign':campaign['id'], 'article_id':entry['item']['article_id'],
                     'language':entry['item']['language'], 'model':campaign['model'],
                     'review_model':campaign['review_model'], 'created_at':campaign['created_at'],
                     'status':'queued', 'stage':'review1' if campaign['operation'] == 'review' else 'translate',
                     'translation_attempts':0, 'review_attempts':0, 'events':[], 'manual_admission_version':VERSION}
            if (any(task.get(k) != v for k,v in fixed.items()) or 'models' in task
                    or not re.fullmatch(r'state/sources/[a-f0-9]{64}\.json', task.get('source_snapshot', ''))
                    or not re.fullmatch(r'[a-f0-9]{64}', task.get('translation_key', ''))
                    or any(not isinstance(proof[k], str) or not re.fullmatch(r'[a-f0-9]{64}', proof[k])
                           for k in ('record_sha256', 'previous_task_sha256', 'candidate_sha256'))
                    or (proof['candidate_path'] is not None and proof['candidate_path'] !=
                        f'state/manual-admission-candidates/{proof["candidate_sha256"]}.json')):
                raise ContractError('Manual admission task changed its frozen contract')
        fields = entry.get('ready_fields')
        if fields is not None and plain_policy.is_plain(fields):
            allowed = {plain_policy.FIELD}
            baseline = (entry.get('plain_migration') or {}).get('accepted_baseline')
            if baseline is not None:
                allowed.update({'accepted_baseline', 'baseline_quality_score'})
            if 'plain_migration' in entry:
                allowed.add('plain_policy_migration_sha256')
            if (set(fields) != allowed or entry.get('ready_sha256') != json_hash(fields)
                    or ('plain_migration' in entry and fields.get('plain_policy_migration_sha256') !=
                        entry['plain_migration']['sha256'])
                    or ('plain_migration' not in entry and not plain_policy.is_plain(campaign))):
                raise ContractError('Plain manual admission prepared task changed')
            if baseline is not None and (fields['accepted_baseline'] != baseline['candidate']
                    or fields['baseline_quality_score'] != baseline['quality_score']):
                raise ContractError('Plain manual admission changed its accepted baseline')
        elif fields is not None and (not isinstance(fields, dict)
                or set(fields) - {'scripture_evidence_path', 'scripture_evidence_sha256', 'stage', 'scripture_resume_reason'}
                or entry.get('ready_sha256') != json_hash(fields)
                or fields.get('scripture_evidence_path') != f'state/scripture/{fields.get("scripture_evidence_sha256")}.json'
                or not isinstance(fields.get('scripture_evidence_sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', fields['scripture_evidence_sha256'])
                or ('stage' in fields and (campaign['operation'] != 'review' or fields['stage'] != 'correct'))):
            raise ContractError('Manual admission prepared task changed')
        if entry['status'] in ('ready', 'admitted') and fields is None:
            raise ContractError('Manual admission lacks prepared evidence')
        if entry['status'] == 'admitted':
            task = state.read(f'state/tasks/{identity}/task.json')
            if not task or identity not in campaign['tasks'] or task['campaign'] != campaign['id']:
                raise ContractError('Manual admission references missing accepted task')
            expected_task = {**proof['task'], **fields, 'models':campaign['models']}
            mutable_task = {'stage', 'status', 'translation_attempts', 'review_attempts', 'events', 'translation_model_actual'}
            if any(task.get(k) != v for k,v in expected_task.items() if k not in mutable_task):
                raise ContractError('Accepted manual task changed its frozen provenance')


def _guard(engine, campaign, entry):
    state, proof = engine.state, entry['provenance']
    task = proof['task']
    source = state.source(task)
    current = state.read('state/source.json', {}).get('articles', {}).get(task['article_id'])
    if (source['revision'] != campaign['source_revision'] or source['article']['id'] != task['article_id']
            or source['translation_key'] != task['translation_key']):
        raise ContractError('original_source_provenance_changed')
    if current is None or current['translation_key'] != task['translation_key']:
        raise ContractError('source_changed_since_manual_selection')
    if engine.human_protected(task['language'], task['article_id']):
        raise ContractError('human_reviewed_or_edited_protected')
    record = state.record(task['language'], task['article_id'])
    # Reconcile only our own exact, never-paid materialization after a crash.
    expected = copy.deepcopy(record)
    if record.get('latest_task') == task['id']:
        expected['history'] = [event for event in record['history'] if event != requested_event(task)]
        previous_id = proof['previous_task_id']
        if previous_id:
            expected['latest_task'] = previous_id
        else:
            expected.pop('latest_task', None)
    if json_hash(expected) != proof['record_sha256']:
        raise ContractError('publication_or_latest_task_changed_since_manual_selection')
    previous_id = proof['previous_task_id']
    previous = state.read(f'state/tasks/{previous_id}/task.json') if previous_id else None
    if json_hash(previous) != proof['previous_task_sha256']:
        raise ContractError('previous_task_changed_since_manual_selection')
    candidate = state.read(proof['candidate_path']) if proof['candidate_path'] else None
    if json_hash(candidate) != proof['candidate_sha256']:
        raise ContractError('original_review_candidate_changed')
    if any(t['language'] == task['language'] and t['article_id'] == task['article_id']
           and t['id'] != task['id'] and t['status'] not in TERMINAL for t in state.tasks()):
        raise ContractError('another_task_is_active')
    return source, record


def requested_event(task):
    return {'event': 'requested', 'task': task['id'], 'campaign': task['campaign'], 'at': task['created_at']}


def _can_migrate(entry):
    return (entry.get('status') in UNFINISHED and entry.get('provenance') is not None
            and 'component_revision' not in entry and 'plain_migration' not in entry
            and (entry['status'] in ('pending', 'ready') or entry.get('reason') in SCRIPTURE_HOLDS))


def _validate_plain_migration(campaign, entry):
    """The overlay is additive; original admission proof and events remain frozen."""
    migration = entry['plain_migration']
    keys = {'version', 'at', 'original_campaign_sha256', 'provenance_sha256',
            'policy', 'previous_ready_fields', 'original_events_sha256',
            'original_events_count', 'accepted_baseline', 'sha256'}
    if (not isinstance(migration, dict) or set(migration) != keys
            or type(migration['version']) is not int or migration['version'] != 1
            or not isinstance(migration['at'], str)
            or migration['original_campaign_sha256'] != json_hash(contract(campaign))
            or migration['provenance_sha256'] != entry['provenance_sha256']
            or migration['sha256'] != json_hash({k: v for k, v in migration.items() if k != 'sha256'})
            or type(migration['original_events_count']) is not int
            or not 1 <= migration['original_events_count'] < len(entry['events'])
            or migration['original_events_sha256'] !=
                json_hash(entry['events'][:migration['original_events_count']])
            or entry['events'][migration['original_events_count']] !=
                {'status':'ready', 'reason':'plain_translation_policy_migration', 'at':migration['at']}):
        raise ContractError('Plain manual migration changed its original history')
    policy = migration['policy']
    allowed = {plain_policy.FIELD, 'prompts', 'prompt_version', 'review_contract_version', 'upgrade_quality_threshold'}
    if (not isinstance(policy, dict) or not plain_policy.is_plain(policy)
            or set(policy) - allowed or not {plain_policy.FIELD, 'prompts', 'prompt_version'} <= policy.keys()
            or not isinstance(policy['prompt_version'], str) or not policy['prompt_version']
            or not isinstance(policy['prompts'], dict) or set(policy['prompts']) != {'translation', 'review'}
            or any(not isinstance(value, str) or not value.strip() for value in policy['prompts'].values())):
        raise ContractError('Malformed frozen plain manual policy')
    from .review_contract import frozen_version
    frozen_version(policy)
    if type(policy.get('upgrade_quality_threshold')) is not int or policy['upgrade_quality_threshold'] != 98:
        raise ContractError('Plain manual replacement review must retain its 98-point threshold')
    baseline = migration['accepted_baseline']
    if baseline is not None and (not isinstance(baseline, dict)
            or set(baseline) != {'candidate', 'quality_score'} or campaign['operation'] != 'review'
            or entry['provenance']['task'].get('base_html_sha256') is None
            or json_hash(baseline['candidate']) != entry['provenance']['candidate_sha256']
            or type(baseline['quality_score']) is not int or not 0 <= baseline['quality_score'] <= 100):
        raise ContractError('Plain manual migration changed its accepted baseline')
    original = entry['events'][migration['original_events_count'] - 1]
    if original['status'] not in ('pending', 'ready', 'attention') or (
            original['status'] == 'attention' and original['reason'] not in SCRIPTURE_HOLDS):
        raise ContractError('Plain migration cannot reopen an unrelated admission hold')
    previous_fields = migration['previous_ready_fields']
    if previous_fields is not None and (not isinstance(previous_fields, dict)
            or set(previous_fields) - {'scripture_evidence_path', 'scripture_evidence_sha256',
                                      'stage', 'scripture_resume_reason'}):
        raise ContractError('Plain migration changed its previous evidence fields')


def migrate_manual_admissions(engine, campaign, ledger=None):
    """Resume never-paid Scripture admission holds inside their original authority.

    Accepted tasks, candidate/provider history, requests, source snapshots,
    campaign contracts and spending allocations are never rewritten. The same
    admission identity receives new frozen prompts only before its first task.
    """
    if (not plain_policy.enabled(engine.config) or plain_policy.is_plain(campaign)
            or campaign.get('cancel_requested')):
        return ledger
    state = engine.state
    ledger = ledger if ledger is not None else state.read(path(campaign['id']))
    if ledger is None:
        return None
    validate(state, campaign, ledger)
    batch_tasks = {custom_id.split(':', 1)[0] for batch in state.batches()
                   for custom_id in batch.get('custom_ids', [])}
    for entry in ledger['entries'].values():
        if not _can_migrate(entry):
            continue
        if state.read(f'state/tasks/{entry["task_id"]}/task.json') is not None or entry['task_id'] in batch_tasks:
            continue  # Existing/reserved task bytes keep their historical policy.
        boundary = getattr(engine, 'continue_work', None)
        if boundary is not None and not boundary():
            break
        try:
            _, record = _guard(engine, campaign, entry)
        except (ContractError, UnicodeError):
            continue  # Source/human/lineage guards are not Scripture holds.
        from .review_contract import frozen_fields as review_fields
        policy = {**plain_policy.frozen_fields(engine.config), **review_fields(engine.config),
                  'prompt_version':engine.config.runtime['prompt_version'],
                  'upgrade_quality_threshold':engine.config.runtime.get('upgrade_quality_threshold', 98),
                  'prompts':{name:engine.config.prompt(name) for name in ('translation', 'review')}}
        baseline = None
        if campaign['operation'] == 'review' and record.get('published'):
            baseline = {'candidate':state.read(entry['provenance']['candidate_path']),
                        'quality_score':record['published']['quality_score']}
        stamp = now()
        migration = {'version':1, 'at':stamp,
                     'original_campaign_sha256':json_hash(contract(campaign)),
                     'provenance_sha256':entry['provenance_sha256'], 'policy':policy,
                     'previous_ready_fields':copy.deepcopy(entry.get('ready_fields')),
                     'original_events_sha256':json_hash(entry['events']),
                     'original_events_count':len(entry['events']), 'accepted_baseline':baseline}
        migration['sha256'] = json_hash(migration)
        entry['plain_migration'] = migration
        fields = {plain_policy.FIELD:1, 'plain_policy_migration_sha256':migration['sha256']}
        if baseline is not None:
            fields.update(accepted_baseline=copy.deepcopy(baseline['candidate']),
                          baseline_quality_score=baseline['quality_score'])
        entry.update(ready_fields=fields, ready_sha256=json_hash(fields),
                     status='ready', reason='plain_translation_policy_migration')
        entry['events'].append({'status':'ready', 'reason':entry['reason'], 'at':stamp})
        save(state, ledger)
    return ledger


def resume(engine, campaign, request, *, new=False):
    state = engine.state
    ledger = state.read(path(campaign['id']))
    if ledger is None:
        ledger = initialize(engine, campaign, request, legacy=not new)
    validate(state, campaign, ledger)
    ledger = migrate_manual_admissions(engine, campaign, ledger)
    for entry in ledger['entries'].values():
        if 'component_revision' in entry:
            from .scripture_component_runtime import enabled, resume as resume_component
            if campaign.get('cancel_requested'):
                from .scripture_component_runtime import cancel_materialized as cancel_component
                cancel_component(engine, campaign, entry)
            elif enabled(engine.config):
                resume_component(engine, campaign, entry)
            continue
        if entry['status'] not in UNFINISHED:
            continue
        if campaign.get('cancel_requested'):
            cancel_materialized(engine, campaign, entry)
            transition(entry, 'cancelled', 'campaign_cancelled_by_owner')
            save(state, ledger)
            continue
        if entry['status'] == 'attention':
            continue
        boundary = getattr(engine, 'continue_work', None)
        if boundary is not None and not boundary():
            break
        try:
            source, record = _guard(engine, campaign, entry)
            task = copy.deepcopy(entry['provenance']['task'])
            task['models'] = copy.deepcopy(campaign['models'])
            if entry['status'] == 'pending':
                if plain_policy.is_plain(campaign):
                    task.update({plain_policy.FIELD:1})
                else:
                    task.update(freeze_scripture_evidence(engine, source, task['language'],
                        frozen_policy=campaign['scripture_quotes'],
                        language_tag=campaign['language_settings'][task['language']]['tag']))
                if task['stage'].startswith('review') and not plain_policy.is_plain(campaign):
                    # The candidate and predecessor were frozen before the first
                    # deadline, and guard rechecks that predecessor on replay.
                    proof = entry['provenance']
                    state.save_candidate(task, state.read(proof['candidate_path']))
                    previous = state.read(f'state/tasks/{proof["previous_task_id"]}/task.json') if proof['previous_task_id'] else None
                    if not adopt_scripture_selection_audit(state, task, previous):
                        task.update(stage='correct', scripture_resume_reason='saved_candidate_requires_quotation_audit')
                fields = {k: v for k, v in task.items() if k != 'models' and entry['provenance']['task'].get(k) != v}
                entry.update(ready_fields=fields, ready_sha256=json_hash(fields))
                transition(entry, 'ready', 'plain_translation_ready' if plain_policy.is_plain(campaign)
                           else 'evidence_frozen')
                save(state, ledger)
                source, record = _guard(engine, campaign, entry)
            else:
                task.update(copy.deepcopy(entry['ready_fields']))
            existing = state.read(f'state/tasks/{task["id"]}/task.json')
            if existing is not None and existing != task:
                raise ContractError('original_task_identity_already_used')
            if entry['provenance']['candidate_path'] is not None:
                state.save_candidate(task, state.read(entry['provenance']['candidate_path']))
            state.save_task(task)
            event = requested_event(task)
            if event not in record['history']:
                record['history'].append(event)
            record['latest_task'] = task['id']
            state.save_record(record)
            if task['id'] not in campaign['tasks']:
                campaign['tasks'].append(task['id'])
            state.save_campaign(campaign)
            transition(entry, 'admitted', 'original_manual_envelope')
            save(state, ledger)
        except ScriptureAttention as exc:
            transition(entry, 'pending' if exc.reason == TEMPORARY else 'attention', exc.reason)
            record_attention_detail(entry, str(exc))
            if not any(i['language'] == entry['item']['language'] and i['article_id'] == entry['item']['article_id']
                       for i in campaign['skipped']):
                campaign['skipped'].append({**entry['item'], 'reason': exc.reason, 'detail': _attention_text(str(exc))})
            save(state, ledger)
            state.save_campaign(campaign)
        except (ContractError, UnicodeError) as exc:
            transition(entry, 'attention', str(exc))
            save(state, ledger)
    campaign['admission_counts'] = counts(state, campaign)
    if not campaign.get('cancel_requested'):
        campaign['status'] = ('admission_attention' if campaign['admission_counts'].get('attention') else
                              'admission_pending' if any(campaign['admission_counts'].get(s) for s in ('pending', 'ready')) else
                              'finished' if all(state.read(f'state/tasks/{identity}/task.json')['status'] in TERMINAL
                                                for identity in campaign['tasks']) else 'active')
    state.save_campaign(campaign)
    return campaign


def admitted(state, task):
    if not task.get('manual_admission_version'):
        return True
    ledger = state.read(path(task['campaign']), {})
    entry = ledger.get('entries', {}).get(task['id'], {})
    if 'component_revision' in entry:
        from .scripture_component_runtime import materialized
        return materialized(state, state.read(f'state/campaigns/{task["campaign"]}.json'), entry)
    return entry.get('status') == 'admitted'


def validate_history(state):
    for ledger_path in sorted((state.root / 'state/manual-admissions').glob('*.json')):
        campaign = state.read(f'state/campaigns/{ledger_path.stem}.json')
        if campaign is None:
            raise ContractError('Manual admission ledger has no campaign')
        validate(state, campaign, read_json(ledger_path))


def validate_campaign(state, identity):
    ledger = state.read(path(identity))
    if ledger is not None:
        validate(state, state.read(f'state/campaigns/{identity}.json'), ledger)


def original_contract(campaign, request):
    """Legacy state cannot enlarge the explicit, immutable manual authority."""
    if (any(request.get(k) != campaign.get(k) for k in
            ('id', 'operation', 'model', 'review_model', 'dry_run', 'requested_by'))
            or 'budget_usd' not in request or float(request['budget_usd']) != campaign['budget_usd']):
        raise ContractError('legacy_request_contract_unproven')
    languages = request.get('languages')
    if languages != 'all' and (not isinstance(languages, str)
            or set(languages.split(',')) != set(campaign['languages'])):
        raise ContractError('legacy_request_languages_unproven')


def restore_orphan(state, request):
    ledger = state.read(path(request['id']))
    if ledger is None:
        return None
    campaign = ledger.get('initial_campaign')
    if (ledger.get('legacy') is not False or not isinstance(campaign, dict)
            or json_hash(request) != ledger.get('request_sha256')
            or any(e.get('status') not in ('pending', 'attention') or
                   state.read(f'state/tasks/{identity}/task.json') is not None
                   for identity,e in ledger.get('entries', {}).items())):
        raise ContractError('Orphan manual admission cannot reconstruct its original campaign')
    validate(state, campaign, ledger)
    state.save_campaign(campaign)
    return campaign


def cancel_materialized(engine, campaign, entry):
    """Retain an exact never-paid child even if its campaign write was interrupted."""
    state = engine.state
    task = state.read(f'state/tasks/{entry["task_id"]}/task.json')
    if task is None:
        return
    proof, fields = entry['provenance'], entry.get('ready_fields')
    if proof is None or fields is None:
        raise ContractError('Cannot cancel an unproven manual materialization')
    expected = {**proof['task'], **fields, 'models':campaign['models']}
    if (task.get('translation_attempts') != 0 or task.get('review_attempts') != 0
            or task.get('batch') or any(task.get(k) != v for k,v in expected.items()
                                     if k not in ('status', 'events'))
            or task.get('status') not in ('queued', 'cancelled')):
        raise ContractError('Manual cancellation requires an exact never-paid child')
    record = state.record(task['language'], task['article_id'])
    event = requested_event(task)
    if event not in record['history']:
        record['history'].append(event)
    if record.get('latest_task') in (None, proof['previous_task_id'], task['id']):
        record['latest_task'] = task['id']
    state.save_record(record)
    if task['id'] not in campaign['tasks']:
        campaign['tasks'].append(task['id'])
    state.save_campaign(campaign)
    engine.finish(task, 'cancelled', 'Campaign cancelled by owner during admission')
    # finish() deliberately leaves terminal tasks unchanged. Reconcile the
    # remaining audit writes if cancellation crashed after saving the task.
    from .attempts import decision
    if not isinstance(task.get('finished_at'), str):
        raise ContractError('Cancelled manual child lacks its terminal timestamp')
    reason = task.get('failure')
    decision(state, task, 'terminal', 'cancelled', reason, task.get('findings', []))
    record = state.record(task['language'], task['article_id'])
    event = {'event':'cancelled', 'task':task['id'], 'at':task['finished_at'], 'reason':reason}
    if event not in record['history']:
        if any(e.get('event') == 'cancelled' and e.get('task') == task['id'] for e in record['history']):
            raise ContractError('Cancelled manual child has conflicting terminal history')
        record['history'].append(event)
        state.save_record(record)


def validate(state, campaign, ledger):
    try:
        _validate(state, campaign, ledger)
    except ContractError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ContractError('Malformed manual admission history') from exc


def restore_orphans(state):
    for ledger_path in sorted((state.root / 'state/manual-admissions').glob('*.json')):
        if state.read(f'state/campaigns/{ledger_path.stem}.json') is None:
            request = state.read(f'state/queue/{ledger_path.stem}.json')
            if not isinstance(request, dict) or request.get('id') != ledger_path.stem:
                raise ContractError('Orphan manual admission has no original queue request')
            restore_orphan(state, request)


def attention_superseded(state, campaign, entry):
    """An explicit later manual task restores the ordinary saved-stage policy."""
    proof = entry['provenance']
    record = state.record(entry['item']['language'], entry['item']['article_id'])
    latest = record.get('latest_task')
    if not proof or not latest or latest == proof['previous_task_id']:
        return False
    task = state.read(f'state/tasks/{latest}/task.json', {})
    later = state.read(f'state/campaigns/{task.get("campaign")}.json', {})
    request = state.read(f'state/queue/{task.get("campaign")}.json')
    return bool(later and later['id'] != campaign['id'] and later['created_at'] >= campaign['created_at']
        and later.get('operation') in ('translate', 'review') and not later.get('dry_run')
        and not any(later.get(k) for k in ('autonomous', 'source_refresh', 'recovery_of_campaign', 'downstream_recovery'))
        and task.get('translation_key') == proof['task']['translation_key']
        and latest in later.get('tasks', []) and request is not None
        and json_hash(request) == later.get('request_sha256'))
