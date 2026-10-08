"""Resume frozen, never-paid manual admissions under their original authority."""
from __future__ import annotations

import copy
import re
from datetime import datetime
from .common import ContractError, canonical, digest, json_hash, now, read_json
from .state import TERMINAL

VERSION = 1
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 6
MAX_ATTENTION_DETAIL_BYTES = 2048
TEMPORARY = 'prefetch_wait_budget'
MUTABLE = {'tasks', 'skipped', 'status', 'task_counts', 'reserved_usd',
           'reported_usage_usd', 'accounted_responses', 'cancel_requested', 'admission_counts'}
UNFINISHED = {'pending', 'ready', 'attention'}
RETIRED_HOLDS = {
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
            and not campaign.get('dry_run')
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
    return (any(_can_resume(e) for e in ledger['entries'].values())
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
            or (entry['status'] not in ('attention', 'cancelled') and 'resume_fields' not in entry)
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
                       'awaiting_translation')
        except (ContractError, UnicodeError) as exc:
            transition(entry, 'attention', str(exc))
    save(state, ledger)
    state.save_campaign(campaign)
    engine.checkpoint('runtime: freeze original manual admission provenance')
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
                or not 1 <= len(entry['events']) <= MAX_EVENTS + (2 if 'resume_fields' in entry else 0)
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
        historical = entry.get('ready_fields')
        if historical is not None and (not isinstance(historical, dict)
                or entry.get('ready_sha256') != json_hash(historical)):
            raise ContractError('Manual admission prepared history changed')
        fields = entry.get('resume_fields', historical)
        if 'resume_fields' in entry:
            allowed = {'plain_policy_resumed', 'stage'}
            if 'accepted_baseline' in fields:
                allowed.update({'accepted_baseline', 'baseline_quality_score'})
                if (campaign['operation'] != 'review' or proof['task'].get('base_html_sha256') is None
                        or json_hash(fields['accepted_baseline']) != proof['candidate_sha256']
                        or type(fields.get('baseline_quality_score')) is not int
                        or not 0 <= fields['baseline_quality_score'] <= 100):
                    raise ContractError('Manual admission changed its accepted baseline')
            count = entry.get('resume_event_count')
            if (not isinstance(fields, dict) or set(fields) != allowed
                    or fields['plain_policy_resumed'] is not True
                    or fields['stage'] != proof['task']['stage']
                    or entry.get('resume_sha256') != json_hash(fields)
                    or type(count) is not int or not 1 <= count < len(entry['events'])
                    or entry.get('resume_events_sha256') != json_hash(entry['events'][:count])
                    or entry['events'][count]['reason'] != 'plain_translation_resumed'
                    or entry['events'][count]['status'] != 'ready'):
                raise ContractError('Manual admission policy resumption changed')
        if entry['status'] in ('ready', 'admitted') and fields is None:
            raise ContractError('Manual admission lacks prepared task fields')
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


def _can_resume(entry):
    return (entry.get('status') in UNFINISHED and entry.get('provenance') is not None
            and (entry['status'] in ('pending', 'ready') or entry.get('reason') in RETIRED_HOLDS))


def resume(engine, campaign, request, *, new=False):
    """Admit the original selected work, with current policy and no new funding."""
    state = engine.state
    ledger = state.read(path(campaign['id']))
    if ledger is None:
        ledger = initialize(engine, campaign, request, legacy=not new)
    validate(state, campaign, ledger)
    batch_tasks = {custom_id.split(':', 1)[0] for batch in state.batches()
                   for custom_id in batch.get('custom_ids', [])}
    for entry in ledger['entries'].values():
        if entry['status'] not in UNFINISHED:
            continue
        if campaign.get('cancel_requested'):
            cancel_materialized(engine, campaign, entry)
            transition(entry, 'cancelled', 'campaign_cancelled_by_owner')
            save(state, ledger)
            continue
        if not _can_resume(entry) or entry['task_id'] in batch_tasks:
            continue
        boundary = getattr(engine, 'continue_work', None)
        if boundary is not None and not boundary():
            break
        try:
            proof = entry['provenance']
            existing = state.read(f'state/tasks/{entry["task_id"]}/task.json')
            if existing is not None and 'resume_fields' not in entry:
                continue  # Materialized historical tasks retain their exact bytes.
            _, record = _guard(engine, campaign, entry)
            if 'resume_fields' not in entry:
                fields = {'plain_policy_resumed':True,
                          'stage':proof['task']['stage']}
                if campaign['operation'] == 'review' and record.get('published'):
                    fields.update(accepted_baseline=state.read(proof['candidate_path']),
                                  baseline_quality_score=record['published']['quality_score'])
                entry.update(resume_fields=fields, resume_sha256=json_hash(fields),
                             resume_event_count=len(entry['events']),
                             resume_events_sha256=json_hash(entry['events']))
                transition(entry, 'ready', 'plain_translation_resumed')
                save(state, ledger)
                _, record = _guard(engine, campaign, entry)
            task = {**copy.deepcopy(proof['task']), **copy.deepcopy(entry['resume_fields']),
                    'models':copy.deepcopy(campaign['models'])}
            if existing is not None and existing != task:
                raise ContractError('original_task_identity_already_used')
            if proof['candidate_path'] is not None:
                state.save_candidate(task, state.read(proof['candidate_path']))
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
        except (ContractError, UnicodeError) as exc:
            if entry['status'] != 'attention':
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
    proof, fields = entry['provenance'], entry.get('resume_fields', entry.get('ready_fields'))
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
