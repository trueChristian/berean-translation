"""Owner-invoked, never-billable abort of interrupted exact recovery staging.

Aborting does not resume acceptance, create missing tasks, reset original history,
or release any allocation. Audit the complete deterministic child set before any
write. A durable abort journal makes repeated explicit cancellation idempotent.
"""
from __future__ import annotations
import copy
from datetime import datetime
from .common import ContractError, json_hash, now
from .recovery import allocation_evidence, money, recovery_history, selection_evidence

ABORT_REASON = 'Incomplete recovery acceptance cancelled by owner'
JOURNAL_KEYS = {'at', 'materialized_task_ids', 'candidate_only_task_ids', 'unstaged_task_ids'}


def requested_event(campaign, task):
    return {'event': 'requested', 'task': task['id'], 'campaign': campaign['id'], 'at': task['created_at']}


def cancelled_event(task, at):
    return {'event': 'cancelled', 'task': task['id'], 'at': at, 'reason': ABORT_REASON}


def _timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except (ValueError, TypeError):
        return False


def audit_abort(state, campaign, maximum, *, terminal=False):
    """Read-only evidence audit for every interrupted staging boundary."""
    parent, _ = allocation_evidence(state, campaign, maximum)
    if (campaign.get('dry_run') is not False or campaign.get('recovery_acceptance_complete') is not False
            or money(campaign.get('reserved_usd')) != 0 or money(campaign.get('reported_usage_usd')) != 0):
        raise ContractError('Only never-submitted incomplete recovery acceptance can be aborted')
    allowed_statuses = {'acceptance_aborted'} if terminal else {'acceptance_incomplete', 'acceptance_aborting'}
    if campaign.get('status') not in allowed_statuses:
        raise ContractError('Unexpected incomplete recovery abort state')
    all_tasks = state.tasks()
    tasks = {task['id']: task for task in all_tasks}
    if len(tasks) != len(all_tasks):
        raise ContractError('Duplicate task evidence prevents recovery abort')
    originals = selection_evidence(state, campaign, parent, tasks)
    expected = set(originals)
    if any(task.get('campaign') == campaign['id'] and task['id'] not in expected for task in all_tasks):
        raise ContractError('Unexpected recovery child prevents abort')
    for batch in state.batches():
        if (batch.get('campaign') == campaign['id'] or expected.intersection(batch.get('tasks', []))
                or any(isinstance(key, str) and key.split(':')[0] in expected
                       for key in batch.get('custom_ids', []))):
            raise ContractError('Batch evidence prevents never-submitted recovery abort')
    allocated, _ = recovery_history(state, parent['id'])
    if money(parent['reserved_usd']) + allocated > money(parent['budget_usd']):
        raise ContractError('Recovery abort cannot conceal an overallocated parent ledger')
    journal = campaign.get('recovery_abort')
    if journal is not None:
        if (not isinstance(journal, dict) or set(journal) != JOURNAL_KEYS
                or not _timestamp(journal.get('at')) or campaign.get('cancel_requested') is not True
                or campaign['status'] not in ('acceptance_aborting', 'acceptance_aborted')):
            raise ContractError('Invalid durable recovery abort journal')
    elif terminal or campaign['status'] == 'acceptance_aborting':
        raise ContractError('Recovery abort requires its durable owner-cancellation journal')
    partition = {'materialized_task_ids': [], 'candidate_only_task_ids': [], 'unstaged_task_ids': []}
    children = {}
    for child_id, (item, previous) in originals.items():
        folder = state.path(f'state/tasks/{child_id}')
        names = set()
        if folder.exists():
            if not folder.is_dir():
                raise ContractError('Unexpected child artifact prevents recovery abort')
            allowed_files = {'candidate.json', 'task.json'}
            scripture = campaign.get('recovery_scripture', {}).get(previous['id'])
            if scripture and scripture.get('audit') is not None:
                allowed_files.add('scripture-selections.json')
            for path in folder.iterdir():
                if path.is_symlink() or not path.is_file() or path.name not in allowed_files:
                    raise ContractError('Unexpected child result/artifact prevents recovery abort')
                names.add(path.name)
        task = tasks.get(child_id)
        if task is None and 'task.json' in names:
            raise ContractError('Recovery child path and identity disagree')
        if task is not None and ('task.json' not in names or state.read(f'state/tasks/{child_id}/task.json') != task):
            raise ContractError('Recovery child path and identity disagree')
        if 'candidate.json' in names:
            if json_hash(state.read(f'state/tasks/{child_id}/candidate.json')) != item['candidate_sha256']:
                raise ContractError('Changed child candidate prevents recovery abort')
        elif task is not None:
            raise ContractError('Materialized child has no candidate')
        scripture = campaign.get('recovery_scripture', {}).get(previous['id'])
        if 'scripture-selections.json' in names and state.read(f'state/tasks/{child_id}/scripture-selections.json') != scripture['audit']:
            raise ContractError('Changed initial Scripture audit prevents recovery abort')
        before = item['record_before']
        record = state.record(previous['language'], previous['article_id'])
        expected_records = [before]
        suffix = []
        if task is not None:
            wanted = {'id': child_id, 'campaign': campaign['id'], 'article_id': previous['article_id'],
                      'issue_id': previous['issue_id'], 'language': previous['language'],
                      'translation_key': previous['translation_key'], 'source_snapshot': previous['source_snapshot'],
                      'stage': 'review1', 'model': campaign['model'], 'review_model': campaign['review_model'],
                      'models': campaign['models'], 'protected': False,
                      'base_html_sha256': None, 'base_metadata_sha256': None,
                      'translation_model_actual': previous['translation_model_actual'],
                      'translation_attempts': 0, 'review_attempts': 0, 'events': [],
                      'recovery_of_task': previous['id'], 'recovery_candidate_sha256': item['candidate_sha256'],
                      'recovery_previous_task_sha256': item['previous_task_sha256']}
            if scripture:
                wanted.update(scripture['fields'], stage=scripture['stage'])
            if (any(task.get(key) != value for key, value in wanted.items())
                    or not _timestamp(task.get('created_at'))
                    or task.get('status') not in ('queued', 'cancelled')):
                raise ContractError('Unexpected task/paid evidence prevents recovery abort')
            allowed = set(wanted) | {'created_at', 'status'}
            if task['status'] == 'cancelled':
                allowed |= {'finished_at', 'failure'}
                if (journal is None or task.get('finished_at') != journal['at']
                        or task.get('failure') != ABORT_REASON):
                    raise ContractError('Unjournalled child cancellation prevents recovery abort')
            elif terminal:
                raise ContractError('Terminal recovery abort retains a queued child')
            if set(task) != allowed:
                raise ContractError('Unexpected task fields prevent never-submitted recovery abort')
            staged_record = copy.deepcopy(before)
            staged_record['latest_task'] = child_id
            suffix.append(requested_event(campaign, task))
            staged_record['history'] += suffix
            expected_records.append(staged_record)
            if task['status'] == 'cancelled':
                suffix.append(cancelled_event(task, journal['at']))
                cancelled_record = copy.deepcopy(before)
                cancelled_record['latest_task'] = child_id
                cancelled_record['history'] += suffix
                expected_records.append(cancelled_record)
            partition['materialized_task_ids'].append(child_id)
            children[child_id] = (task, before)
        else:
            partition['candidate_only_task_ids' if names else 'unstaged_task_ids'].append(child_id)
        if terminal:
            # Later ordinary work can legitimately extend this record or publish.
            # Preserve the original prefix and this abort's exact cancellation audit.
            prefix = before['history'] + suffix
            if (record.get('language') != before['language'] or record.get('article_id') != before['article_id']
                    or not isinstance(record.get('history'), list) or record['history'][:len(prefix)] != prefix
                    or any(event.get('task') in expected or event.get('campaign') == campaign['id']
                           for event in record['history'][len(prefix):])):
                raise ContractError('Aborted recovery record audit was changed')
        elif record not in expected_records:
            raise ContractError('Unexpected record/history evidence prevents recovery abort')
    materialized = partition['materialized_task_ids']
    listed = campaign.get('tasks')
    if (not isinstance(listed, list) or listed != [identity for identity in materialized if identity in listed]
            or (terminal and listed != materialized)):
        raise ContractError('Unexpected campaign child list prevents recovery abort')
    if journal is not None and any(journal.get(key) != value for key, value in partition.items()):
        raise ContractError('Recovery abort artifact partition changed after its durable intent')
    if terminal and campaign.get('acceptance_aborted_at') != journal['at']:
        raise ContractError('Recovery abort completion timestamp disagrees with its journal')
    # No child/campaign history may be hidden on an unrelated article record.
    pairs = {(previous['language'], previous['article_id']) for _, previous in originals.values()}
    for record in state.records():
        if (record.get('language'), record.get('article_id')) in pairs:
            continue
        if (record.get('latest_task') in expected or any(event.get('task') in expected
                or event.get('campaign') == campaign['id'] for event in record.get('history', []))):
            raise ContractError('Unexpected foreign child history prevents recovery abort')
    return partition, children


def checkpoint_abort(engine, message):
    engine.checkpoint(message)
    if getattr(engine.gitstore, 'publish', False):
        engine.gitstore.require_published_checkpoint()


def abort_incomplete_recovery(engine, campaign):
    """Explicit cancel path; retries only finish this never-billable local abort."""
    state = engine.state
    terminal = campaign.get('status') == 'acceptance_aborted'
    partition, children = audit_abort(state, campaign, engine.config.runtime['max_tasks_per_request'], terminal=terminal)
    if terminal:
        checkpoint_abort(engine, 'runtime: reconcile explicit recovery abort checkpoint')
        return campaign
    journal = campaign.get('recovery_abort')
    if journal is None:
        journal = {'at': now(), **partition}
        campaign.update(cancel_requested=True, status='acceptance_aborting', recovery_abort=journal)
        state.save_campaign(campaign)
        engine.checkpoint('runtime: record explicit incomplete recovery abort intent without releasing allocation')
    for task, before in children.values():
        if task['status'] != 'cancelled':
            task.update(status='cancelled', finished_at=journal['at'], failure=ABORT_REASON)
            state.save_task(task)
        record = copy.deepcopy(before)
        record['latest_task'] = task['id']
        record['history'] += [requested_event(campaign, task), cancelled_event(task, journal['at'])]
        if state.record(task['language'], task['article_id']) != record:
            state.save_record(record)
    campaign.update(status='acceptance_aborted', acceptance_aborted_at=journal['at'],
                    tasks=partition['materialized_task_ids'])
    state.save_campaign(campaign)
    checkpoint_abort(engine, 'runtime: finish incomplete recovery abort; retain full allocation and all artifacts')
    return campaign
