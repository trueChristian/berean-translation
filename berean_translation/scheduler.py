"""Continue validated collection windows without relying solely on cron delivery.

This module reads durable state and dispatches an existing trusted-main workflow.
It never changes translation history or calls the model provider.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.error import HTTPError

from . import autonomous, downstream, manual_admission
from .common import ContractError, json_hash, read_json
from .engine import Engine
from .queue import github_request
from .state import TERMINAL

WORKER = 'ai-worker.yml'
DISCOVERY = 'ai-discover.yml'
WRITERS = (WORKER, DISCOVERY)


def _submittable(config, campaign):
    if campaign.get('cancel_requested') or campaign.get('dry_run'):
        return False
    if campaign.get('autonomous'):
        return autonomous.enabled(config) and campaign.get('automatic_acceptance_complete') is True
    if campaign.get('downstream_recovery'):
        return (campaign.get('downstream_acceptance_complete') is True
                and downstream.submission_enabled(config, campaign))
    return True


def work_reason(engine):
    """Find work the normal collector can advance; counters alone are insufficient."""
    config, state = engine.config, engine.state
    campaigns = {c['id']: c for c in state.campaigns()}
    tasks = state.tasks()
    by_id = {t['id']: t for t in tasks}
    batches = state.batches()
    for batch in batches:
        # Cancellation still needs collection and usage accounting. An uncertain
        # creation alone never authorizes another create or a perpetual loop.
        if batch['status'] in ('submitted', 'cancelling') and batch.get('remote_id'):
            return 'submitted_batches'
        campaign = campaigns.get(batch['campaign'], {})
        if batch['status'] == 'prepared' and _submittable(config, campaign):
            if any(not engine.human_protected(by_id[i]['language'], by_id[i]['article_id'])
                   for i in batch['tasks'] if i in by_id):
                return 'prepared_batches'
    for task in tasks:
        if (task['status'] == 'queued' and not task.get('batch')
                and _submittable(config, campaigns[task['campaign']])
                and manual_admission.admitted(state, task)
                and not engine.human_protected(task['language'], task['article_id'])):
            return 'queued_tasks'
    for campaign in campaigns.values():
        if campaign.get('cancel_requested') or campaign.get('dry_run'):
            continue
        if (campaign.get('autonomous') and autonomous.enabled(config)
                and not campaign.get('automatic_acceptance_complete')):
            return 'incomplete_automatic_admission'
        if (state.read(f'state/queue/{campaign["id"]}.json') is None
                or state.read(f'state/queue-errors/{campaign["id"]}.json') is not None):
            continue  # The collector only visits surviving, non-rejected requests.
        ledger = state.read(manual_admission.path(campaign['id']))
        if ledger:
            for entry in ledger['entries'].values():
                if (manual_admission._can_resume(entry)
                        and (entry['task_id'] not in by_id or 'resume_fields' in entry)
                        and not any(entry['task_id'] in b['tasks'] for b in batches)):
                    return 'manual_admission'
        elif manual_admission.legacy_targets(campaign):
            return 'manual_admission'
    automatic_room = autonomous.enabled(config) and sum(
        t['status'] not in TERMINAL for t in tasks if t.get('autonomous')) < autonomous.policy(config).get('max_active_tasks', 0)
    articles = state.read('state/source.json', {}).get('articles', {})
    claims = manual_admission.claims(state, include_attention=True)
    for path in sorted((state.root / 'state/queue').glob('*.json')):
        if path.stem in campaigns or state.read(f'state/queue-errors/{path.stem}.json') is not None:
            continue
        request = read_json(path)
        if request.get('dry_run') or request.get('source_refresh'):
            continue
        if request.get('operation') == 'repair' and not request.get('manual_authorization'):
            continue
        if not request.get('autonomous'):
            return 'manual_request'
        if not automatic_room:
            continue
        selection = request['selection']
        article = articles.get(selection['article_id'])
        if article:
            spec, reason = autonomous.selection(engine, selection['language'], article,
                by_id.get(selection['previous_task_id']), manual_claims=claims)
            if spec is not None and reason is None:
                return 'pending_automatic_admission'
    if automatic_room:
        records = {(r['language'], r['article_id']): r for r in state.records()}
        for article in articles.values():
            for language in config.languages:
                previous = by_id.get(records.get((language, article['id']), {}).get('latest_task'))
                spec, reason = autonomous.selection(engine, language, article, previous, manual_claims=claims)
                if spec is not None and reason is None:
                    identity = 'auto-' + json_hash(spec)
                    if state.read(f'state/queue-errors/{identity}.json') is None:
                        return 'eligible_automatic_work'
    return None


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        raise ContractError('GitHub returned an invalid workflow timestamp') from exc


def continue_collection(config, environment, transport=github_request, *, now=None):
    """Dispatch at most one successor after a successful, serialized worker job."""
    result = {'dispatched': False}
    if not config.runtime.get('automatic_collection_continuation', True):
        return {**result, 'reason': 'continuation_disabled'}
    repository = environment.get('GITHUB_REPOSITORY', '')
    run_id = environment.get('GITHUB_RUN_ID', '')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not re.fullmatch(r'[1-9][0-9]*', run_id)
            or environment.get('GITHUB_REF') != 'refs/heads/main'
            or environment.get('GITHUB_WORKFLOW_REF') != f'{repository}/.github/workflows/{WORKER}@refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in ('push', 'schedule', 'workflow_run', 'workflow_dispatch')
            or environment.get('OPERATION', 'collect') != 'collect'):
        raise ContractError('Collection continuation requires the trusted main collector run')
    token = environment.get('GH_TOKEN', '')
    if not token:
        raise ContractError('GITHUB_TOKEN is required for collection continuation')
    engine = Engine(config, None, None, None)
    last = engine.state.read('state/last-collection.json', {})
    if (last.get('operation') != 'collect' or not last.get('completed_at')
            or last.get('collection', {}).get('stop_reason') == 'provider_unavailable'):
        return {**result, 'reason': 'no_provider_collection'}
    reason = work_reason(engine)
    if reason is None:
        return {**result, 'reason': 'no_actionable_work'}
    current_time = now or datetime.now(timezone.utc)
    base = f'https://api.github.com/repos/{repository}'

    def request(method, path, data=None):
        try:
            return transport(method, base + path, token, data)
        except HTTPError as exc:
            # Never blindly retry a dispatch: a lost acknowledgment can mean it
            # succeeded. A visible failed handoff leaves cron/manual recovery.
            raise ContractError(f'Collection continuation rejected by GitHub ({exc.code})') from exc

    def current_run_ready():
        run = request('GET', f'/actions/runs/{run_id}')
        if (run.get('head_branch') != 'main' or run.get('path') != f'.github/workflows/{WORKER}'
                or run.get('status') != 'in_progress'):
            return False
        jobs = request('GET', f'/actions/runs/{run_id}/jobs?per_page=100')['jobs']
        return any(j['name'] == 'worker' and j['status'] == 'completed'
                   and j.get('conclusion') == 'success' for j in jobs)

    if request('GET', '')['default_branch'] != 'main' or not current_run_ready():
        return {**result, 'reason': 'collector_not_successful_or_cancelled'}
    histories = {}
    for workflow in WRITERS:
        definition = request('GET', f'/actions/workflows/{workflow}')
        if definition['state'] != 'active':
            if workflow == WORKER:
                return {**result, 'reason': 'collector_disabled'}
            continue
        runs = request('GET', f'/actions/workflows/{workflow}/runs?branch=main&per_page=100')['workflow_runs']
        histories[workflow] = runs
        for run in runs:
            if str(run['id']) == run_id or run['status'] == 'completed':
                continue
            if workflow == DISCOVERY:
                return {**result, 'reason': 'discovery_already_pending'}
            if run['status'] in ('queued', 'pending', 'requested', 'waiting'):
                return {**result, 'reason': 'collector_already_pending'}
            # Another run may only be finishing its handoff. It no longer owns
            # the writer lock, so inspect the worker job rather than run totals.
            jobs = request('GET', f'/actions/runs/{run["id"]}/jobs?per_page=100')['jobs']
            if any(j['name'] == 'worker' and j['status'] != 'completed' for j in jobs):
                return {**result, 'reason': 'collector_already_pending'}
    target = WORKER
    if config.runtime.get('automatic_discovery') and DISCOVERY in histories:
        # Retry discovery no more than hourly, including failures. Continuous
        # collection must not depend on a separate cron to notice new English.
        attempts = histories[DISCOVERY]
        if not attempts or current_time - max(_stamp(r['created_at']) for r in attempts) >= timedelta(hours=1):
            target = DISCOVERY
    if (target == WORKER and reason in ('manual_request', 'manual_admission',
            'pending_automatic_admission', 'eligible_automatic_work')
            and engine.state.read('state/discovery-status.json', {}).get('status') == 'held'):
        return {**result, 'reason': 'source_discovery_requires_attention'}
    if not current_run_ready():
        return {**result, 'reason': 'collector_cancelled_before_dispatch'}
    payload = {'ref': 'main', 'inputs': {'operation': 'collect'}} if target == WORKER else {'ref': 'main'}
    request('POST', f'/actions/workflows/{target}/dispatches', payload)
    return {'dispatched': True, 'workflow': target, 'ref': 'main', 'reason': reason}
