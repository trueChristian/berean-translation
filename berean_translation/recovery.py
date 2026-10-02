"""Explicit candidate recovery selectors and non-recyclable parent budget allocations.

Only the existing serialized collector accepts these requests. Queue writers do
not allocate budgets; every acceptance observes all previously accepted envelopes.
"""
from __future__ import annotations
import copy
import re
from decimal import Decimal, InvalidOperation
from .common import ContractError, digest, json_hash
from .state import TERMINAL

FIELDS = {'recovery_of_campaign', 'previous_task_ids'}


def recovery_selector(request, maximum):
    """Validate selector syntax before defaults can broaden an exact request."""
    if not FIELDS.intersection(request):
        return None
    parent, identities = request.get('recovery_of_campaign'), request.get('previous_task_ids')
    if 'budget_usd' not in request or type(request.get('dry_run')) is not bool:
        raise ContractError('Exact recovery requires an explicit budget and dry_run boolean')
    if not money(request['budget_usd']):
        raise ContractError('Recovery budget must be positive')
    if (request.get('operation') != 'review' or request.get('retry_failed', False) is not False
            or any(key in request for key in ('languages', 'issues', 'article_ids',
                                              'source_translation_keys', 'source_refresh'))):
        raise ContractError('Exact recovery is review-only and cannot include other selection fields')
    if not isinstance(parent, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', parent):
        raise ContractError('Exact recovery requires one original campaign identity')
    if (not isinstance(identities, list) or not identities or len(identities) > maximum
            or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value)
                   for value in identities) or len(set(identities)) != len(identities)):
        raise ContractError('Exact recovery requires unique, nonempty previous task IDs within the task limit')
    if parent == request.get('id'):
        raise ContractError('A recovery request cannot be its own original campaign')
    return parent, identities


def money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError('Invalid recovery budget ledger') from exc
    if not result.is_finite() or result < 0 or result > 1000000000:
        raise ContractError('Invalid recovery budget ledger')
    # Inspect digits directly: Decimal arithmetic/normalize can round very long
    # inputs under its context and accidentally hide unsupported precision.
    _, digits, exponent = result.as_tuple()
    while digits and digits[-1] == 0:
        digits, exponent = digits[:-1], exponent + 1
    if digits and exponent < -6:
        raise ContractError('Recovery budgets and ledgers require at most six decimal places in USD')
    return result


def recorded_request(state, campaign):
    """Check accepted envelope/selector against its immutable request provenance."""
    request = campaign.get('recovery_request')
    if not isinstance(request, dict) or json_hash(request) != campaign.get('request_sha256'):
        raise ContractError('Recovery immutable request snapshot/hash is missing or changed')
    queued = state.read(f'state/queue/{campaign["id"]}.json')
    if queued is not None and queued != request:
        raise ContractError('Recovery campaign disagrees with its immutable queue request')
    if (request.get('id') != campaign['id'] or request.get('operation') != 'review'
            or request.get('operation') != campaign.get('operation')
            or request.get('recovery_of_campaign') != campaign.get('recovery_of_campaign')
            or request.get('previous_task_ids') != campaign.get('previous_task_ids')
            or request.get('dry_run') is not campaign.get('dry_run')
            or money(request.get('budget_usd')) != money(campaign.get('budget_usd'))
            or any(request.get(key) and request[key] != campaign.get(key)
                   for key in ('model', 'review_model'))):
        raise ContractError('Recovery allocation/selector disagrees with its immutable request')
    return request


def is_recovery_campaign(state, campaign):
    queued = state.read(f'state/queue/{campaign["id"]}.json', {})
    return bool(FIELDS.intersection(campaign) or 'recovery_request' in campaign
                or 'recovery_allocation_usd' in campaign
                or (isinstance(queued, dict) and FIELDS.intersection(queued)))


def recovery_history(state, parent):
    """All accepted allocations count forever, including failed/cancelled work."""
    total, recovered = Decimal(0), set()
    for campaign in state.campaigns():
        if not is_recovery_campaign(state, campaign):
            continue
        request = recorded_request(state, campaign)
        if request['recovery_of_campaign'] != parent or request['dry_run'] is True:
            continue
        ids = campaign.get('previous_task_ids')
        if (not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or recovered.intersection(ids)):
            raise ContractError('Invalid or duplicate accepted recovery history')
        allocation = money(campaign.get('recovery_allocation_usd'))
        if not allocation or allocation != money(campaign.get('budget_usd')):
            raise ContractError('Accepted recovery must retain its full original budget envelope')
        total += allocation
        recovered.update(ids)
    return total, recovered


def plan_recovery(engine, request, budget, selector):
    """Read-only, all-or-nothing preflight. Never silently skip a requested target."""
    state, config = engine.state, engine.config
    parent_id, ids = selector
    parent = state.read(f'state/campaigns/{parent_id}.json')
    if (not parent or parent.get('id') != parent_id or parent.get('status') != 'finished'
            or parent.get('dry_run') is not False or parent.get('recovery_of_campaign')):
        raise ContractError('Recovery requires a finished, non-dry original campaign')
    tasks = state.tasks()
    parent_tasks = parent.get('tasks', [])
    by_id = {}
    for task in tasks:
        by_id.setdefault(task.get('id'), []).append(task)
    if (not parent_tasks or len(parent_tasks) != len(set(parent_tasks))
            or any(len(by_id.get(identity, [])) != 1
                   or by_id[identity][0].get('campaign') != parent_id
                   or by_id[identity][0].get('status') not in TERMINAL for identity in parent_tasks)
            or any(t.get('campaign') == parent_id and t.get('status') not in TERMINAL for t in tasks)
            or any(b.get('campaign') == parent_id and b.get('status') not in
                   {'collected', 'results_invalid', 'upload_failed', 'cancelled_before_submission'}
                   for b in state.batches())):
        raise ContractError('Original campaign has missing, duplicate or unfinished work')
    allocated, recovered = recovery_history(state, parent_id)
    cap, reserved, envelope = money(parent.get('budget_usd')), money(parent.get('reserved_usd')), money(budget)
    remaining = cap - reserved - allocated
    if remaining < 0 or envelope > remaining:
        raise ContractError('Recovery envelope exceeds original cap minus original reservations and all accepted recovery allocations')
    ledger = {key: float(value) for key, value in {
        'original_cap_usd': cap, 'original_reserved_usd': reserved,
        'previously_allocated_usd': allocated, 'requested_usd': envelope,
        'remaining_before_usd': remaining, 'remaining_after_usd': remaining - envelope}.items()}
    source_index = state.read('state/source.json')
    planned, frozen, pairs = [], {}, set()
    for identity in ids:
        if identity in recovered:
            raise ContractError(f'Task {identity} already has an accepted recovery; allocations and attempts are never recycled')
        matches = by_id.get(identity, [])
        if len(matches) != 1 or parent_tasks.count(identity) != 1:
            raise ContractError(f'Recovery task {identity} must exist exactly once in the original campaign')
        previous = matches[0]
        # Verify the canonical task path as well as the inventory identity.
        if state.read(f'state/tasks/{identity}/task.json') != previous:
            raise ContractError('Recovery task path and identity disagree')
        article_id, language = previous.get('article_id'), previous.get('language')
        if (previous.get('campaign') != parent_id or previous.get('status') != 'not_ready'
                or previous.get('recovery_of_task') or previous.get('batch')
                or language not in config.languages):
            raise ContractError(f'Recovery task {identity} must be a terminal not_ready original task')
        article = source_index['articles'].get(article_id)
        if (not article or article['translation_key'] != previous.get('translation_key')
                or article['issue_id'] != previous.get('issue_id')):
            raise ContractError(f'Recovery task {identity} is not current-source compatible')
        record = state.record(language, article_id)
        pair = language, article_id
        if (record.get('language') != language or record.get('article_id') != article_id
                or record.get('latest_task') != identity or pair in pairs
                or any(t.get('language') == language and t.get('article_id') == article_id
                       and t.get('status') not in TERMINAL for t in tasks)):
            raise ContractError(f'Recovery task {identity} is not the unique latest terminal task')
        if (record.get('published') or previous.get('protected')
                or any(state.path(f'content/{language}/articles/{article_id}.{ext}').exists()
                       for ext in ('html', 'json'))):
            raise ContractError(f'Recovery task {identity} has a protected or public replacement')
        new_id = digest(request['id'] + ':' + language + ':' + article_id)[:32]
        if by_id.get(new_id) or state.path(f'state/tasks/{new_id}').exists():
            raise ContractError('Recovery would overwrite an existing task identity')
        candidate = state.candidate(previous)
        source = state.source(previous)
        if (not isinstance(candidate, dict) or not candidate
                or set(candidate) != {'html', 'title', 'subtitle', 'section'}
                or not isinstance(candidate.get('html'), str) or not candidate['html'].strip()
                or not all(candidate.get(k) is None or isinstance(candidate.get(k), str)
                           for k in ('title', 'subtitle', 'section'))
                or source.get('translation_key') != previous['translation_key']
                or source.get('article', {}).get('id') != article_id
                or not previous.get('translation_model_actual')):
            raise ContractError(f'Recovery task {identity} lacks a candidate or valid source/model provenance')
        item = {'previous_task_id': identity, 'article_id': article_id, 'language': language,
                'reason': 'exact_candidate_recovery', 'candidate_sha256': json_hash(candidate),
                'previous_task_sha256': json_hash(previous), 'source_snapshot': previous['source_snapshot'],
                'record_before': copy.deepcopy(record), 'record_before_sha256': json_hash(record)}
        planned.append(item)
        frozen[identity] = {'previous': previous, 'candidate': candidate, 'source': source}
        pairs.add(pair)
    return planned, frozen, ledger


def allocation_evidence(state, campaign, maximum):
    """Audit immutable authorization and frozen parent-cap math, including aborts."""
    request = recorded_request(state, campaign)
    parent_id, ids = recovery_selector(request, maximum)
    parent = state.read(f'state/campaigns/{parent_id}.json')
    if (not parent or parent.get('id') != parent_id or parent.get('status') != 'finished'
            or parent.get('dry_run') is not False or parent.get('recovery_of_campaign')):
        raise ContractError('Recovery ledger has no finished original campaign')
    ledger = campaign.get('recovery_budget', {})
    cap, reserved = money(parent.get('budget_usd')), money(parent.get('reserved_usd'))
    allocated = money(ledger.get('previously_allocated_usd'))
    envelope = money(campaign.get('budget_usd'))
    if (money(ledger.get('original_cap_usd')) != cap
            or money(ledger.get('original_reserved_usd')) != reserved
            or money(ledger.get('requested_usd')) != envelope
            or money(ledger.get('remaining_before_usd')) != cap - reserved - allocated
            or money(ledger.get('remaining_after_usd')) != cap - reserved - allocated - envelope):
        raise ContractError('Frozen recovery budget ledger disagrees with the original allocation')
    return parent, ids


def selection_evidence(state, campaign, parent, tasks):
    """Every planned original remains auditable even when no child was staged."""
    selections = campaign.get('selection', [])
    if ([item.get('previous_task_id') for item in selections] != campaign['previous_task_ids']
            or not re.fullmatch(r'[a-f0-9]{64}', campaign.get('request_sha256', ''))):
        raise ContractError('Accepted recovery selection provenance is incomplete')
    result = {}
    for item in selections:
        previous = tasks.get(item['previous_task_id'])
        before = item.get('record_before')
        if (not previous or previous.get('campaign') != parent['id']
                or previous.get('status') != 'not_ready'
                or parent.get('tasks', []).count(previous['id']) != 1
                or json_hash(previous) != item.get('previous_task_sha256')
                or json_hash(state.candidate(previous)) != item.get('candidate_sha256')
                or any(item.get(key) != previous.get(key) for key in
                       ('article_id', 'language', 'source_snapshot'))
                or not isinstance(before, dict) or json_hash(before) != item.get('record_before_sha256')
                or before.get('latest_task') != previous['id'] or before.get('published')
                or before.get('language') != previous['language']
                or before.get('article_id') != previous['article_id']
                or not isinstance(before.get('history'), list)):
            raise ContractError('Recovery must preserve original task/candidate/source/record provenance')
        source = state.source(previous)
        if (source.get('translation_key') != previous['translation_key']
                or source.get('article', {}).get('id') != previous['article_id']):
            raise ContractError('Recovery original source identity mismatch')
        child_id = digest(campaign['id'] + ':' + previous['language'] + ':' + previous['article_id'])[:32]
        if child_id in result:
            raise ContractError('Duplicate deterministic recovery child identity')
        result[child_id] = (item, previous)
    return result


def validate_recoveries(state, tasks, campaigns, maximum):
    """Audit the additive ledger and immutable links without reselecting old work."""
    by_id = {campaign['id']: campaign for campaign in campaigns}
    parents = set()
    for campaign in campaigns:
        if not is_recovery_campaign(state, campaign):
            continue
        parent, ids = allocation_evidence(state, campaign, maximum)
        originals = selection_evidence(state, campaign, parent, tasks)
        if campaign.get('dry_run') is True:
            if campaign.get('tasks') or campaign.get('reserved_usd') or campaign.get('recovery_allocation_usd') != 0:
                raise ContractError('Recovery dry run must not create tasks, reservations or allocations')
            continue
        parents.add(parent['id'])
        if campaign.get('status') == 'acceptance_aborted':
            from .recovery_abort import audit_abort
            audit_abort(state, campaign, maximum, terminal=True)
            continue
        if (not campaign.get('recovery_acceptance_complete')
                or campaign.get('tasks') != list(originals)):
            raise ContractError('Accepted recovery selection/task provenance is incomplete')
        for task_id, (item, previous) in originals.items():
            task = tasks.get(task_id)
            if (not task or task.get('campaign') != campaign['id']
                    or task.get('recovery_of_task') != previous['id']
                    or task.get('recovery_previous_task_sha256') != item.get('previous_task_sha256')
                    or task.get('recovery_candidate_sha256') != item.get('candidate_sha256')
                    or any(task.get(key) != previous.get(key) for key in
                           ('article_id', 'language', 'issue_id', 'source_snapshot', 'translation_key'))
                    or task.get('stage') not in ('review1', 'correct', 'review2')
                    or task.get('translation_attempts', 0) > 1):
                raise ContractError('Recovery must preserve original task/candidate/source provenance and stage limits')
    for parent_id in parents:
        allocated, _ = recovery_history(state, parent_id)
        parent = by_id[parent_id]
        if money(parent['reserved_usd']) + allocated > money(parent['budget_usd']):
            raise ContractError('Cumulative recovery allocations exceed the original campaign cap')
