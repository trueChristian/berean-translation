"""Owner-authorized archive queue with one permanent automatic spending authority.

Selection is resumable pagination, never a completion limit. Each exact pair is
admitted only with its complete remaining stage chain reserved. Historical manual
and refresh envelopes retain their own authority and are never repurposed.
"""
from __future__ import annotations
import copy
import re
from datetime import datetime, timezone
from .common import ContractError, digest, json_hash, now, read_json
from .recovery import money
from .state import TERMINAL
from . import continuation, downstream, stage_budget

POLICY = 'autonomous_translation'
AUTHORITY_PATH = 'state/automatic-budget.json'


class BudgetUnavailable(ContractError):
    """A durable pending request can be reconsidered without changing its identity."""


def policy(config):
    value = config.runtime.get(POLICY, {'enabled': False})
    if value == {'enabled': False}:
        return value
    fields = {'enabled', 'total_budget_usd', 'max_envelope_usd', 'max_active_tasks', 'page_size'}
    if not isinstance(value, dict) or set(value) != fields or type(value['enabled']) is not bool:
        raise ContractError('Invalid autonomous translation policy')
    if not 0 < money(value['max_envelope_usd']) <= money(value['total_budget_usd']) <= money(30):
        raise ContractError('Automatic authority is cumulative and at most $30; renewal requires new authorization')
    if money(value['max_envelope_usd']) > money(10):
        raise ContractError('Automatic complete-stage envelopes are at most $10')
    if any(type(value[k]) is not int or not 1 <= value[k] <= 1000
           for k in ('max_active_tasks', 'page_size')):
        raise ContractError('Automatic concurrency and page size must be positive bounded integers')
    return value


def enabled(config):
    return policy(config)['enabled']


def owns_automatic_work(config, state):
    # Pausing a migrated authority must never reactivate legacy spending paths.
    return enabled(config) or state.read(AUTHORITY_PATH) is not None


def legacy_allocations(state):
    downstream.funding_ledger(state)
    return [{'campaign': c['id'], 'allocation_usd': c['downstream_allocation_usd']}
            for c in sorted(state.campaigns(), key=lambda c: c['id'])
            if c.get('downstream_recovery') and not c.get('dry_run') and not c.get('empty_selection')
            and downstream.campaign_funding_scope(c) == downstream.SHARED_SCOPE]


def initialize(engine):
    state = engine.state
    if state.read(AUTHORITY_PATH) is not None:
        ledger(state)
        return
    baseline = legacy_allocations(state)
    authority = {'version': 1, 'authorization': 'owner-2026-10-04-autonomous-archive',
        'approved_total_usd': float(money(policy(engine.config)['total_budget_usd'])),
        'legacy_recovery_allocations': baseline,
        'separate_legacy_refresh_envelopes': [
            {'campaign': c['id'], 'budget_usd': c['budget_usd']}
            for c in sorted(state.campaigns(), key=lambda c: c['id']) if c.get('source_refresh')],
        'created_at': now()}
    authority['sha256'] = json_hash(authority)
    state.write(AUTHORITY_PATH, authority)
    engine.checkpoint('runtime: record cumulative automatic authority and historical funding boundaries')


def frozen(campaign):
    return {key: campaign[key] for key in ('automatic_request', 'execution', 'initial_task',
        'stage_budget', 'budget_usd', 'automatic_allocation_usd', 'automatic_allocation_index',
        'automatic_allocation_before_usd', 'automatic_approved_total_usd', 'automatic_authority_sha256',
        'automatic_settlement_credits')}


def ledger(state):
    authority = state.read(AUTHORITY_PATH)
    campaigns = []
    for campaign in state.campaigns():
        request = state.read(f'state/queue/{campaign["id"]}.json', {})
        if campaign.get('autonomous') or 'automatic_request' in campaign or request.get('autonomous'):
            if campaign.get('autonomous') is not True:
                raise ContractError('Automatic campaign funding marker cannot be removed')
            campaigns.append(campaign)
    if authority is None:
        if campaigns:
            raise ContractError('Autonomous funding authority is missing')
        return {'allocated_usd': money(0), 'count': 0, 'authority': None}
    if (authority.get('version') != 1 or authority.get('sha256') != json_hash(
            {k: v for k, v in authority.items() if k != 'sha256'})
            or authority['legacy_recovery_allocations'] != legacy_allocations(state)):
        raise ContractError('Automatic authority or historical allocation baseline changed')
    settlements = validated_settlements(state, campaigns)
    total = sum((money(c['allocation_usd']) for c in authority['legacy_recovery_allocations']), money(0))
    gross = total
    by_id = {c['id']: c for c in campaigns}
    for index, campaign in enumerate(sorted(campaigns, key=lambda c: c['automatic_allocation_index']), 1):
        allocation = money(campaign['automatic_allocation_usd'])
        credits = campaign.get('automatic_settlement_credits', {})
        credit_total = money(0)
        for identity, sha256 in credits.items():
            event = settlements.get(identity)
            if (not event or event['sha256'] != sha256
                    or by_id[identity]['automatic_allocation_index'] >= index):
                raise ContractError('Automatic admission uses an invalid or future settlement credit')
            credit_total += money(event['released_usd'])
        total = gross - credit_total
        if (campaign['automatic_allocation_index'] != index
                or money(campaign['automatic_allocation_before_usd']) != total
                or campaign.get('automatic_frozen_sha256') != json_hash(frozen(campaign))
                or campaign['automatic_authority_sha256'] != authority['sha256']
                or money(campaign['automatic_approved_total_usd']) != money(authority['approved_total_usd'])
                or allocation != money(campaign['stage_budget']['total_reserved_usd'])
                or allocation != money(campaign['budget_usd'])):
            raise ContractError('Automatic permanent allocation or complete-stage reservation changed')
        total += allocation
        gross += allocation
        if total > money(authority['approved_total_usd']):
            raise ContractError('Automatic allocations exceed their cumulative authorization')
    released = sum((money(event['released_usd']) for event in settlements.values()), money(0))
    committed = gross - released
    return {'allocated_usd': committed, 'gross_allocated_usd': gross, 'released_usd': released,
            'count': len(campaigns), 'authority': authority, 'settlements': settlements}


def prior_chain(state, previous):
    result, seen = [], set()
    while previous:
        if previous['id'] in seen:
            raise ContractError('Recovery history contains a cycle')
        seen.add(previous['id']); result.append(previous)
        identity = previous.get('automatic_previous_task') or previous.get('downstream_previous_task')
        previous = state.read(f'state/tasks/{identity}/task.json') if identity else None
    return result


def resume_stage(state, previous):
    """Reuse saved generation/review work without rewriting old terminal records."""
    if downstream.refusal(previous):
        return None, 'provider_refusal_requires_owner_attention'
    kind = previous.get('failure_kind')
    if kind == 'invalid_result' and previous.get('stage') in ('review1', 'review2'):
        from .review_contract import MAX_FINDINGS
        archived = state.read(f'state/tasks/{previous["id"]}/results/{previous["stage"]}.json', {})
        value = archived.get('result') if isinstance(archived, dict) else None
        findings = value.get('findings') if isinstance(value, dict) else None
        if isinstance(findings, list) and len(findings) > MAX_FINDINGS:
            return None, 'preserved_review_overflow_requires_attention'
    if not kind and ('Missing, ambiguous, or truncated model response' in previous.get('failure', '')
                    or previous.get('provider_failure', {}).get('code') == 'provider_error'):
        return None, 'legacy_response_outcome_unknown_requires_owner_attention'
    candidate = state.candidate(previous)
    if previous['status'] not in ('not_ready', 'budget_blocked') or previous.get('batch'):
        return None, 'terminal_outcome_requires_attention'
    history = prior_chain(state, previous)
    paid_recoveries = [t for t in history if t.get('autonomous_recovery') or t.get('downstream_recovery')]
    if paid_recoveries:
        try:
            finished = datetime.fromisoformat(previous['finished_at'])
            if finished.tzinfo is None:
                raise ValueError('missing timezone')
        except (KeyError, TypeError, ValueError):
            return None, 'missing_completion_time_requires_attention'
        if (datetime.now(timezone.utc) - finished).total_seconds() < 3600:
            return None, 'continuation_cooldown'
    technical = ('truncated', 'invalid_response', 'invalid_result')
    if kind in technical and sum(t.get('failure_kind') in technical for t in history) >= 2:
        return None, 'repeated_technical_failure_requires_attention'
    if kind == 'scripture_evidence_expired':
        return previous['stage'], None  # A proven never-submitted payload needs newly frozen evidence.
    if not downstream.usable_candidate(candidate) or not previous.get('translation_model_actual'):
        if kind in ('truncated', 'invalid_response', 'invalid_result'):
            return 'translate', None
        return None, 'no_usable_candidate_requires_owner_attention'
    quality = kind == 'quality_rejection' or 'Final review failed' in previous.get('failure', '')
    if quality:
        if paid_recoveries:
            current_anchors = continuation.anchors(previous, state.source(previous))
            for older in history[1:]:
                if older['translation_key'] != previous['translation_key']:
                    continue
                if json_hash(candidate) == json_hash(state.candidate(older)):
                    return None, 'continuation_no_candidate_progress_requires_attention'
                older_anchors = continuation.anchors(older, state.source(previous))
                if current_anchors is None or older_anchors is None:
                    return None, 'continuation_progress_uncertain_requires_attention'
                if any(a in b or b in a for a in current_anchors for b in older_anchors):
                    return None, 'continuation_repeated_findings_requires_attention'
        return 'correct', None
    if previous['status'] == 'budget_blocked':
        return previous['stage'], None
    if previous['stage'] in ('review1', 'review2') and kind in ('truncated', 'invalid_response', 'invalid_result'):
        if sum(t['stage'].startswith('review') and t.get('failure_kind') in
               ('truncated', 'invalid_response', 'invalid_result') for t in history) >= 2:
            return None, 'repeated_review_failure_requires_attention'
        return previous['stage'], None
    if kind == 'invalid_result' and previous['stage'] in ('translate', 'correct'):
        return 'correct', None
    return None, 'technical_failure_requires_attention'


def selection(engine, language, article, previous=None):
    if engine.human_protected(language, article['id']):
        return None, 'human_reviewed_or_edited_protected'
    record = engine.state.record(language, article['id'])
    if previous and previous['status'] not in TERMINAL:
        return None, 'active'
    pub = record.get('published')
    if pub and pub['translation_key'] == article['translation_key']:
        return None, 'already_translated'
    if previous and downstream.refusal(previous):
        return None, 'provider_refusal_requires_owner_attention'
    stage, reason = ('translate', None)
    if previous and previous['translation_key'] == article['translation_key']:
        stage, reason = resume_stage(engine.state, previous)
    if reason:
        return None, reason
    spec = {'language': language, 'article_id': article['id'], 'translation_key': article['translation_key'],
            'issue_id': article['issue_id'], 'stage': stage,
            'previous_task_id': previous['id'] if previous else None,
            'previous_task_sha256': json_hash(previous) if previous else None,
            'candidate_sha256': json_hash(engine.state.candidate(previous)) if previous else None,
            'recovery': bool(previous and previous['translation_key'] == article['translation_key'])}
    return spec, None


def enqueue(engine):
    if not enabled(engine.config):
        return []
    initialize(engine)
    state, config = engine.state, engine.config
    settings = policy(config)
    tasks = state.tasks()
    by_id = {t['id']: t for t in tasks}
    source = state.read('state/source.json', {})
    pending, pending_manual, pending_slots = [], [], 0
    for path in sorted((config.root / 'state/queue').glob('*.json')):
        request = read_json(path)
        if request.get('autonomous') and not state.read(f'state/campaigns/{path.stem}.json') and not state.read(f'state/queue-errors/{path.stem}.json'):
            pending.append((request['selection']['language'], request['selection']['article_id']))
            if state.read(f'state/automatic-holds/{path.stem}.json') is None:
                pending_slots += 1
        elif (not request.get('autonomous') and not request.get('source_refresh')
              and not state.read(f'state/campaigns/{path.stem}.json')
              and not state.read(f'state/queue-errors/{path.stem}.json')):
            pending_manual.append(request)
    room = settings['max_active_tasks'] - sum(t['status'] not in TERMINAL for t in tasks) - pending_slots
    queued, held = [], []
    pairs = []
    for article in sorted(source.get('articles', {}).values(), key=lambda a: (a['issue_id'], a['sequence'], a['id'])):
        for language in config.languages:
            record = state.record(language, article['id'])
            previous = by_id.get(record.get('latest_task'))
            # Finish saved work first, then fill missing archive/language pairs.
            priority = 0 if previous and previous['translation_key'] == article['translation_key'] else 1
            pairs.append((priority, article, language, previous))
    pairs.sort(key=lambda p: p[0])
    for _, article, language, previous in pairs:
        if (language, article['id']) in pending:
            continue
        from .refresh import _pending_covers
        if any(_pending_covers(config, source, request, language, article)
               or previous and previous['id'] in request.get('previous_task_ids', [])
               for request in pending_manual):
            continue
        spec, reason = selection(engine, language, article, previous)
        if reason:
            if reason not in ('active', 'already_translated', 'human_reviewed_or_edited_protected'):
                held.append({'article_id': article['id'], 'language': language, 'reason': reason})
            continue
        if len(queued) >= min(room, settings['page_size']):
            continue
        identity = 'auto-' + json_hash(spec)
        request = {'id': identity, 'operation': 'automatic', 'autonomous': True,
                   'requested_by': 'owner-authorized-autonomous-archive', 'selection': spec}
        path = f'state/queue/{identity}.json'
        existing = state.read(path)
        if existing is not None:
            if existing != request:
                raise ContractError('Automatic queue identity changed')
            continue
        state.write(path, request); queued.append(identity)
    state.write('state/automatic-status.json', {'attention': held,
        'allocated_usd': float(ledger(state)['allocated_usd']),
        'approved_total_usd': state.read(AUTHORITY_PATH)['approved_total_usd'],
        'pending_requests': len(pending) + len(queued)})
    if queued:
        engine.checkpoint('runtime: enqueue exact archive work before admission and paid requests')
    return queued


def template(config, identity, recovery):
    from .review_contract import frozen_fields
    if recovery:
        model, review_model = (config.runtime['automatic_downstream_recovery'][k] for k in ('model', 'review_model'))
    else:
        model, review_model = config.runtime['default_model'], config.runtime['default_review_model']
    return {'id': identity, 'model': model, 'review_model': review_model,
        'models': copy.deepcopy(config.models), 'prompt_version': config.runtime['prompt_version'],
        'structural_feedback_version': config.runtime.get('structural_feedback_version'),
        'prompts': {name: config.prompt(name) for name in ('translation', 'review', 'repair')},
        'language_settings': copy.deepcopy(config.languages),
        'glossaries': read_json(config.root / 'config/glossaries.json')['languages'],
        'max_output_tokens': config.runtime['automatic_downstream_recovery']['max_output_tokens'] if recovery else config.runtime['max_output_tokens'],
        'review_output_tokens': config.runtime['automatic_downstream_recovery']['review_output_tokens'] if recovery else config.review_output_limit(review_model),
        'quality_threshold': config.runtime['quality_threshold'], **frozen_fields(config),
        'automatic_continuation_policy': {'version': 3, 'cooldown_seconds': 3600, 'progress_required': True, 'funding': 'cumulative_authority'}}


def accept(engine, request):
    state, config = engine.state, engine.config
    identity = request.get('id', '')
    if (set(request) != {'id', 'operation', 'autonomous', 'requested_by', 'selection'}
            or request['autonomous'] is not True or request['operation'] != 'automatic'
            or identity != 'auto-' + json_hash(request['selection'])):
        raise ContractError('Invalid immutable automatic request')
    existing = state.read(f'state/campaigns/{identity}.json')
    if existing:
        if existing.get('automatic_request') != request:
            raise ContractError('Automatic request changed after acceptance')
        ledger(state)
        return existing
    if not enabled(config):
        raise BudgetUnavailable('Automatic work is paused')
    initialize(engine)
    active = sum(task['status'] not in TERMINAL for task in state.tasks())
    if active >= policy(config)['max_active_tasks']:
        state.write(f'state/automatic-holds/{identity}.json', {
            'reason': 'automatic_capacity_wait', 'active_tasks': active,
            'maximum_active_tasks': policy(config)['max_active_tasks'], 'last_checked_at': now()})
        raise BudgetUnavailable('Automatic work awaits a free active-task slot')
    spec = request['selection']
    article = state.read('state/source.json')['articles'].get(spec['article_id'])
    previous = state.read(f'state/tasks/{spec["previous_task_id"]}/task.json') if spec['previous_task_id'] else None
    record = state.record(spec['language'], spec['article_id'])
    current, reason = selection(engine, spec['language'], article, previous) if article else (None, 'source_removed')
    if current != spec or record.get('latest_task') != spec['previous_task_id']:
        raise ContractError('Automatic exact selection is no longer eligible: ' + str(reason))
    source = state.source(previous) if spec['recovery'] else engine.source_client.snapshot(article['id'])
    if source['translation_key'] != spec['translation_key']:
        raise ContractError('English source changed before automatic admission')
    snapshot_path = f'state/sources/{json_hash(source)}.json'
    state.write(snapshot_path, source)
    candidate = state.candidate(previous) if spec['recovery'] else None
    execution = template(config, identity, spec['recovery'])
    execution['language_settings'] = {spec['language']: execution['language_settings'][spec['language']]}
    pub = record.get('published')
    task = {'id': digest(identity + ':' + spec['language'] + ':' + spec['article_id'])[:32],
        'campaign': identity, 'article_id': spec['article_id'], 'issue_id': spec['issue_id'],
        'language': spec['language'], 'translation_key': spec['translation_key'], 'source_snapshot': snapshot_path,
        'created_at': now(), 'status': 'queued', 'stage': spec['stage'], 'autonomous': True,
        'autonomous_recovery': spec['recovery'], 'automatic_previous_task': spec['previous_task_id'],
        'model': execution['model'], 'review_model': execution['review_model'], 'models': execution['models'],
        'protected': False, 'base_html_sha256': pub['html_sha256'] if pub else None,
        'base_metadata_sha256': pub['metadata_sha256'] if pub else None,
        'translation_model_actual': previous.get('translation_model_actual') if candidate is not None else None,
        'translation_attempts': 0, 'review_attempts': 0, 'events': [],
        'findings': copy.deepcopy(previous.get('findings', [])) if spec['recovery'] else [],
        'rejection_reason': 'Audit the full candidate against the authoritative English and substantiate each finding'}
    if config.runtime.get('scripture_quotes_enabled', False):
        from .scripture_evidence import policy as scripture_policy, freeze_scripture_evidence, adopt_scripture_selection_audit
        execution['scripture_quotes'] = scripture_policy(config.root)
        task.update(freeze_scripture_evidence(engine, source, task['language'], frozen_policy=execution['scripture_quotes']))
        if task['stage'].startswith('review'):
            view = stage_budget.PlanningState(state, execution, source, candidate)
            adopted = adopt_scripture_selection_audit(view, task, previous)
            if adopted:
                task['initial_scripture_audit'] = view.writes[f'state/tasks/{task["id"]}/scripture-selections.json']
            else:
                task.update(stage='correct', scripture_resume_reason='saved_candidate_requires_quotation_audit')
    budget = stage_budget.plan(config, state, task, execution, source, candidate)
    task['stage_budget'] = budget
    stage_budget.enforce(task, candidate)
    funding = ledger(state)
    allocation = money(budget['total_reserved_usd'])
    cap = min(money(policy(config)['total_budget_usd']), money(funding['authority']['approved_total_usd']))
    if allocation <= money(0):
        raise ContractError('Automatic work requires a positive finite stage reservation')
    if allocation > money(policy(config)['max_envelope_usd']) or funding['allocated_usd'] + allocation > cap:
        state.write(f'state/automatic-holds/{identity}.json', {'reason': 'automatic_budget_blocked',
            'required_usd': float(allocation), 'remaining_usd': float(max(money(0), cap - funding['allocated_usd'])), 'last_checked_at': now()})
        raise BudgetUnavailable('Complete remaining stage chain awaits automatic budget')
    campaign = {**execution, 'autonomous': True, 'operation': 'automatic', 'created_at': task['created_at'],
        'requested_by': request['requested_by'], 'automatic_request': copy.deepcopy(request),
        'request_sha256': json_hash(request), 'source_revision': source['revision'],
        'budget_usd': float(allocation), 'reserved_usd': 0.0, 'reported_usage_usd': 0.0,
        'automatic_allocation_usd': float(allocation), 'automatic_allocation_index': funding['count'] + 1,
        'automatic_allocation_before_usd': float(funding['allocated_usd']),
        'automatic_approved_total_usd': funding['authority']['approved_total_usd'],
        'automatic_authority_sha256': funding['authority']['sha256'], 'stage_budget': budget,
        'automatic_settlement_credits': {identity: event['sha256'] for identity, event in funding['settlements'].items()},
        'execution': copy.deepcopy(execution), 'initial_task': copy.deepcopy(task),
        'tasks': [], 'selection': [spec], 'skipped': [], 'dry_run': False,
        'status': 'acceptance_incomplete', 'automatic_acceptance_complete': False,
        'languages': [spec['language']], 'issues': [spec['issue_id']]}
    campaign['automatic_frozen_sha256'] = json_hash(frozen(campaign))
    state.save_campaign(campaign)
    engine.checkpoint('runtime: allocate full automatic stage chain before materializing work')
    return stage_accepted(engine, campaign, candidate)


def stage_accepted(engine, campaign, candidate=None):
    """Resume a proven unsubmitted acceptance checkpoint without reallocating."""
    state = engine.state
    task = copy.deepcopy(campaign['initial_task'])
    record = state.record(task['language'], task['article_id'])
    previous_id = task['automatic_previous_task']
    if record.get('latest_task') not in (previous_id, task['id']):
        raise ContractError('Automatic acceptance predecessor changed')
    if not state.read(f'state/tasks/{task["id"]}/task.json'):
        if task['autonomous_recovery']:
            previous = state.read(f'state/tasks/{previous_id}/task.json')
            candidate = state.candidate(previous)
            if candidate is not None:
                state.save_candidate(task, candidate)
        if task.get('initial_scripture_audit'):
            state.write(f'state/tasks/{task["id"]}/scripture-selections.json', task['initial_scripture_audit'])
        state.save_task(task)
    if record.get('latest_task') != task['id']:
        record['latest_task'] = task['id']
        record['history'].append({'event': 'automatic_requested', 'task': task['id'],
                                 'previous_task': previous_id, 'campaign': campaign['id'], 'at': task['created_at']})
        state.save_record(record)
    campaign.update(tasks=[task['id']], automatic_acceptance_complete=True, status='active')
    state.save_campaign(campaign)
    return campaign


def validate_history(config, state):
    ledger(state)
    for campaign in state.campaigns():
        if not campaign.get('autonomous'):
            continue
        if state.read(f'state/queue/{campaign["id"]}.json') != campaign['automatic_request']:
            raise ContractError('Automatic immutable queue authorization changed')
        if any(campaign.get(k) != v for k, v in campaign['execution'].items()):
            raise ContractError('Automatic frozen execution contract changed')
        original = campaign['initial_task']
        spec = campaign['automatic_request']['selection']
        previous = state.read(f'state/tasks/{spec["previous_task_id"]}/task.json') if spec['previous_task_id'] else None
        candidate = state.candidate(previous) if spec['recovery'] else None
        if previous and (json_hash(previous) != spec['previous_task_sha256']
                         or json_hash(state.candidate(previous)) != spec['candidate_sha256']):
            raise ContractError('Automatic recovery predecessor evidence changed')
        if stage_budget.plan(config, state, original, campaign, state.source(original), candidate) != campaign['stage_budget']:
            raise ContractError('Automatic complete-stage reservation changed')
        task = state.read(f'state/tasks/{original["id"]}/task.json')
        if campaign.get('automatic_acceptance_complete') and (task is None or campaign['tasks'] != [original['id']]):
            raise ContractError('Automatic accepted task inventory changed')
        if task:
            for key in ('id', 'campaign', 'article_id', 'issue_id', 'language', 'translation_key', 'source_snapshot',
                        'model', 'review_model', 'models', 'stage_budget', 'autonomous', 'autonomous_recovery',
                        'automatic_previous_task', 'base_html_sha256', 'base_metadata_sha256',
                        'scripture_evidence_path', 'scripture_evidence_sha256', 'initial_scripture_audit', 'scripture_resume_reason'):
                if task.get(key) != original.get(key):
                    raise ContractError('Automatic task provenance changed')
            if task['stage'] not in campaign['stage_budget']['stages_usd']:
                raise ContractError('Automatic task stage is outside its funded chain')


def settlement_proof(state, campaign, batches=None):
    """Prove every potentially billable request ended with complete usage.

    Missing rows, missing usage, uncertain submissions and collection failures
    keep the entire envelope. Prices come only from the accepted campaign.
    """
    from decimal import Decimal, ROUND_CEILING
    from .requests import usage_cost
    if not campaign.get('automatic_acceptance_complete') or not campaign['tasks']:
        return None
    tasks = [state.read(f'state/tasks/{identity}/task.json') for identity in campaign['tasks']]
    if any(not t or t['status'] not in TERMINAL for t in tasks):
        return None
    proof, usage = [], money(0)
    batches = ([b for b in state.batches() if b['campaign'] == campaign['id']]
               if batches is None else batches)
    accounted_stages = {(task['id'], kind): set() for task in tasks for kind in ('generation', 'review')}
    for batch in batches:
        for identity in batch['tasks']:
            kind = 'review' if batch['stage'].startswith('review') else 'generation'
            if (identity, kind) not in accounted_stages:
                raise ContractError('Automatic settlement batch is outside its task inventory')
            accounted_stages[(identity, kind)].add(batch['stage'])
    for task in tasks:
        if (len(accounted_stages[(task['id'], 'generation')]) != task['translation_attempts']
                or len(accounted_stages[(task['id'], 'review')]) != task['review_attempts']):
            return None  # A missing batch cannot be treated as proof of no charge.
    if sum((money(b['reserved_usd']) for b in batches if not b.get('reservation_reused_from')), money(0)) != money(campaign['reserved_usd']):
        raise ContractError('Automatic settlement reservation inventory changed')
    for batch in batches:
        if batch['campaign'] != campaign['id']:
            continue
        if not batch.get('remote_id'):
            if (batch.get('submission_started_at') and not batch.get('create_not_called')) or batch['status'] not in ('cancelled_before_submission', 'upload_failed'):
                return None
            proof.append({'batch': batch['id'], 'never_submitted': True,
                          'payload_sha256': batch['payload_sha256']})
            continue
        if batch.get('remote_status') not in ('completed', 'failed', 'expired', 'cancelled') or batch['status'] != 'collected':
            return None
        attempts = []
        for identity in batch['tasks']:
            path = f'state/tasks/{identity}/attempts/{batch["stage"]}.json'
            attempt = state.read(path)
            if not attempt:
                return None
            price = campaign['models'][batch['model']]
            # Both aggregate counters must exist; zero is a real report, not a default.
            value = usage_cost(price, attempt.get('response', {}).get('usage'))
            if value is None:
                return None
            usage += Decimal(str(value))
            attempts.append({'task': identity, 'stage': batch['stage'], 'evidence_sha256': json_hash(attempt),
                             'priced_usage_usd': value})
        proof.append({'batch': batch['id'], 'remote_id': batch['remote_id'],
                      'remote_status': batch['remote_status'], 'payload_sha256': batch['payload_sha256'],
                      'attempts': attempts})
    usage = usage.quantize(Decimal('0.000001'), rounding=ROUND_CEILING)
    allocation = money(campaign['automatic_allocation_usd'])
    if usage > allocation:
        raise ContractError('Provider-reported usage exceeds the full-stage reservation; automatic admission must stop')
    return {'version': 1, 'campaign': campaign['id'], 'authority_sha256': campaign['automatic_authority_sha256'],
            'allocation_usd': float(allocation), 'provider_reported_usage_priced_usd': float(usage),
            'released_usd': float(allocation - usage),
            'tasks': {task['id']: json_hash(task) for task in tasks}, 'batches': proof}


def validated_settlements(state, campaigns, batches=None):
    by_id = {c['id']: c for c in campaigns}
    by_campaign = {}
    for batch in state.batches() if batches is None else batches:
        by_campaign.setdefault(batch['campaign'], []).append(batch)
    result = {}
    for path in sorted((state.root / 'state/automatic-settlements').glob('*.json')):
        event = read_json(path)
        campaign = by_id.get(path.stem)
        if campaign is None or event.get('campaign') != path.stem:
            raise ContractError('Settlement has no matching autonomous campaign')
        proof = settlement_proof(state, campaign, by_campaign.get(campaign['id'], []))
        expected = {**proof, 'sha256': json_hash(proof)} if proof is not None else None
        if event != expected:
            raise ContractError('Automatic settlement proof, usage or immutable event changed')
        result[path.stem] = event
    return result


def settle(engine):
    """Append once; cancellation, failure or cheap actual use cannot erase history."""
    state = engine.state
    campaigns, batches = state.campaigns(), state.batches()
    existing = validated_settlements(state, [c for c in campaigns if c.get('autonomous')], batches)
    by_campaign = {}
    for batch in batches:
        by_campaign.setdefault(batch['campaign'], []).append(batch)
    changed = False
    for campaign in campaigns:
        if not campaign.get('autonomous') or campaign['id'] in existing:
            continue
        proof = settlement_proof(state, campaign, by_campaign.get(campaign['id'], []))
        if proof is None:
            continue
        event = {**proof, 'sha256': json_hash(proof)}
        state.write(f'state/automatic-settlements/{campaign["id"]}.json', event)
        changed = True
    if changed:
        engine.checkpoint('runtime: settle proven completed automatic requests at frozen rates')


def frontier(config, state, tasks):
    funding = ledger(state)
    by_id = {task['id']: task for task in tasks}
    articles = state.read('state/source.json', {}).get('articles', {})
    rows, counts = [], {}
    for record in state.records():
        task = by_id.get(record.get('latest_task'))
        if not task or task['status'] == 'complete':
            continue
        pub = record.get('published') or {}
        article = articles.get(task['article_id'])
        if pub.get('human_reviewed') or pub.get('edit_issue'):
            reason = 'human_reviewed_or_edited_protected'
        elif task['status'] not in TERMINAL:
            reason = 'active'
        elif article is None:
            reason = 'source_removed_requires_attention'
        elif pub and pub['translation_key'] == article['translation_key']:
            reason = 'already_translated'
        elif downstream.refusal(task):
            reason = 'provider_refusal_requires_owner_attention'
        elif task['translation_key'] != article['translation_key']:
            reason = 'eligible_source_refresh' if enabled(config) else 'automatic_paused'
        else:
            _, reason = resume_stage(state, task)
            reason = reason or ('eligible_automatic' if enabled(config) else 'automatic_paused')
        rows.append({'language': task['language'], 'article_id': task['article_id'], 'latest_task': task['id'],
                     'processing_status': task['status'], 'reason': reason,
                     'accepted_cycles': sum(bool(t.get('autonomous_recovery') or t.get('downstream_recovery'))
                                            for t in prior_chain(state, task))})
        counts[reason] = counts.get(reason, 0) + 1
    by_pair = {(row['language'], row['article_id']):row for row in rows}
    for path in sorted((config.root / 'state/queue').glob('auto-*.json')):
        if state.read(f'state/campaigns/{path.stem}.json') or state.read(f'state/queue-errors/{path.stem}.json'):
            continue
        request = read_json(path)
        spec = request['selection']
        hold = state.read(f'state/automatic-holds/{path.stem}.json', {})
        key = (spec['language'], spec['article_id'])
        by_pair[key] = {**by_pair.get(key, {}), 'language':key[0], 'article_id':key[1],
            'latest_task':spec['previous_task_id'], 'processing_status':'pending', 'request_id':path.stem,
            'reason':hold.get('reason', 'pending_automatic_admission'),
            **{k:hold[k] for k in ('detail','required_usd','remaining_usd') if k in hold}}
    rows, counts = list(by_pair.values()), {}
    for row in rows:
        counts[row['reason']] = counts.get(row['reason'], 0) + 1
    authority = funding['authority']
    if authority is None:
        funding['allocated_usd'] = sum((money(item['allocation_usd']) for item in legacy_allocations(state)), money(0))
        funding['gross_allocated_usd'] = funding['allocated_usd']
    cap = authority['approved_total_usd'] if authority else policy(config).get('total_budget_usd', 0)
    return {'format_version': '2', 'derived': True, 'automatic_policy_version': 3,
            'source_revision': state.read('state/source.json', {}).get('revision'),
            'automatic_enabled': enabled(config), 'approved_total_usd': cap,
            'committed_usd': float(funding['allocated_usd']),
            'gross_allocated_usd': float(funding.get('gross_allocated_usd', 0)),
            'settled_unused_usd': float(funding.get('released_usd', 0)),
            'usage_description': 'Provider-reported usage priced at frozen rates; not invoice reconciliation',
            'counts': dict(sorted(counts.items())), 'items': sorted(rows, key=lambda r: (r['language'],r['article_id']))}
