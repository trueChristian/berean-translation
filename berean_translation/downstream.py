"""Bounded repair-first recovery with permanent, separately funded envelopes.

The serialized collector owns selection, allocation and task creation. Historical
campaigns and terminal tasks are read-only inputs, not resettable retry counters.
"""
from __future__ import annotations
import copy
import re
from datetime import datetime, timezone
from .common import ContractError, digest, json_hash, now, read_json
from .recovery import money
from .state import TERMINAL
from . import continuation

POLICY = 'automatic_downstream_recovery'
MANUAL_SCOPE = 'manual_workflow'
SHARED_SCOPE = 'shared_policy'


def validate_policy(config):
    policy = config.runtime.get(POLICY, {'enabled': False})
    if policy == {'enabled': False}:
        return policy
    fields = {'enabled', 'total_budget_usd', 'campaign_budget_usd', 'max_articles',
              'model', 'review_model', 'max_output_tokens', 'review_output_tokens'}
    if (not isinstance(policy, dict) or set(policy) != fields
            or type(policy.get('enabled')) is not bool):
        raise ContractError('Invalid downstream recovery policy')
    cap, budget = money(policy['total_budget_usd']), money(policy['campaign_budget_usd'])
    if not budget or budget > money(config.runtime['max_campaign_usd']):
        raise ContractError('Downstream campaign budget must be positive and within the runtime cap')
    if policy['enabled'] and (not cap or budget > cap):
        raise ContractError('Enable downstream recovery only with a positive approved total cap')
    if type(policy['max_articles']) is not int or not 1 <= policy['max_articles'] <= 5:
        raise ContractError('Downstream recovery selects at most five articles per request')
    for key in ('max_output_tokens', 'review_output_tokens'):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise ContractError('Downstream output limits must be positive integers')
    config.model(policy['model']); config.model(policy['review_model'])
    return policy


def validate_manual_authorization(request):
    """Validate provenance recorded by the trusted workflow CLI, never an input flag.

    The queue is durable trusted repository state. This is a consistency check,
    not an authentication mechanism for arbitrary externally supplied JSON.
    """
    if 'manual_authorization' not in request:
        return False
    authorization = request['manual_authorization']
    fields = {'kind', 'repository', 'workflow_ref', 'run_id', 'actor'}
    if (not isinstance(authorization, dict) or set(authorization) != fields
            or any(type(authorization[key]) is not str for key in fields)):
        raise ContractError('Manual recovery requires immutable workflow-dispatch authorization')
    repository, run_id, actor = (authorization[key] for key in ('repository', 'run_id', 'actor'))
    if (authorization['kind'] != 'github_workflow_dispatch'
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository)
            or authorization['workflow_ref'] != repository + '/.github/workflows/ai-repair.yml@refs/heads/main'
            or not re.fullmatch(r'[1-9][0-9]*', run_id)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?', actor)
            or request.get('id') != 'gh-' + run_id
            or request.get('requested_by') != actor
            or 'scheduled_hour' in request):
        raise ContractError('Manual recovery authorization must match its run, actor and main-branch workflow')
    return True


def validate_request(config, request):
    fields = {'id', 'operation', 'model', 'review_model', 'budget_usd', 'dry_run',
              'requested_by', 'max_articles', 'scheduled_hour', 'manual_authorization', 'continuation_policy'}
    if (not isinstance(request, dict) or set(request) - fields
            or request.get('operation') != 'repair'
            or not isinstance(request.get('id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request.get('id', ''))
            or type(request.get('dry_run')) is not bool):
        raise ContractError('Downstream repair requires an immutable bounded request without other selectors')
    count = request.get('max_articles')
    if type(count) is not int or not 1 <= count <= min(5, config.runtime['max_tasks_per_request']):
        raise ContractError('Downstream max_articles must be an integer from 1 to 5')
    budget = money(request.get('budget_usd'))
    if not budget or budget > money(config.runtime['max_campaign_usd']):
        raise ContractError('Downstream request requires an explicit positive campaign budget')
    config.model(request.get('model')); config.model(request.get('review_model'))
    continuation.validate_policy(request.get('continuation_policy'))
    manual = validate_manual_authorization(request)
    policy = validate_policy(config)
    if not request['dry_run'] and not manual and not policy['enabled']:
        raise ContractError('Paid downstream recovery is disabled pending an approved total budget')
    hour = request.get('scheduled_hour')
    if hour is not None:
        if (not isinstance(hour, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}', hour)
                or request['id'] != 'downstream-' + hour.replace('-', '').replace('T', '')
                or request['dry_run'] or count != policy.get('max_articles')
                or any(request[key] != policy.get(key) for key in ('model', 'review_model'))
                or budget != money(policy.get('campaign_budget_usd'))
                or ('continuation_policy' in request and request['continuation_policy'] != continuation.policy())):
            raise ContractError('Scheduled recovery must match the configured hourly policy')
    return request


def recovery_key(task):
    return digest(':'.join(task[k] for k in ('language', 'article_id', 'translation_key')))


def funding_settings(campaign):
    return {key: campaign.get(key) for key in ('funding_scope', 'request_sha256',
        'budget_usd', 'downstream_allocation_usd', 'allocation_index',
        'allocation_before_usd', 'approved_total_usd')}


def campaign_funding_scope(campaign):
    request = campaign.get('downstream_request')
    if not isinstance(request, dict):
        raise ContractError('Downstream immutable authorization is missing or changed')
    manual = validate_manual_authorization(request)
    scope = campaign.get('funding_scope', SHARED_SCOPE)
    if (scope not in (MANUAL_SCOPE, SHARED_SCOPE)
            or (scope == MANUAL_SCOPE) != manual):
        raise ContractError('Downstream funding scope must match its immutable authorization')
    # Old shared-policy campaigns predate this fingerprint. All newly accepted
    # envelopes freeze it, including previews and requests with no eligible work.
    if ('funding_scope' in campaign or 'funding_sha256' in campaign) and (
            json_hash(funding_settings(campaign)) != campaign.get('funding_sha256')):
        raise ContractError('Downstream frozen funding authorization changed')
    if manual and (type(campaign.get('allocation_index')) is not int
            or campaign['allocation_index'] != 1
            or money(campaign.get('allocation_before_usd')) != money(0)
            or money(campaign.get('approved_total_usd')) != money(request.get('budget_usd'))):
        raise ContractError('Manual recovery must retain its own explicit, one-run spending ceiling')
    return scope


def submission_enabled(config, campaign):
    """Hourly policy changes cannot revoke or enlarge a manual run's own cap."""
    return (campaign_funding_scope(campaign) == MANUAL_SCOPE
            or validate_policy(config)['enabled'])


def funding_ledger(state):
    total, manual_total, entries, allocations, manual_count = money(0), money(0), [], [], 0
    for campaign in state.campaigns():
        queued = state.read(f'state/queue/{campaign["id"]}.json')
        if not (campaign.get('downstream_recovery') or campaign.get('operation') == 'repair'
                or 'downstream_request' in campaign or isinstance(queued, dict) and queued.get('operation') == 'repair'):
            continue
        if campaign.get('downstream_recovery') is not True:
            raise ContractError('Downstream campaign marker cannot be removed')
        request = campaign.get('downstream_request')
        if (not isinstance(request, dict) or json_hash(request) != campaign.get('request_sha256')
                or request.get('id') != campaign.get('id')
                or request.get('operation') != 'repair'
                or request.get('dry_run') is not campaign.get('dry_run')
                or request.get('requested_by') != campaign.get('requested_by')
                or money(request.get('budget_usd')) != money(campaign.get('budget_usd'))
                or any(request.get(k) != campaign.get(k) for k in ('model', 'review_model'))):
            raise ContractError('Downstream immutable authorization is missing or changed')
        queued = state.read(f'state/queue/{campaign["id"]}.json')
        if queued is not None and queued != request:
            raise ContractError('Downstream queue request changed after acceptance')
        scope = campaign_funding_scope(campaign)
        allocation = money(campaign.get('downstream_allocation_usd'))
        selections = campaign.get('selection', [])
        if campaign['dry_run']:
            if allocation or campaign.get('tasks') or campaign.get('reserved_usd'):
                raise ContractError('Downstream preview cannot spend or allocate work')
            continue
        if campaign.get('empty_selection'):
            if allocation or selections or campaign.get('tasks') or campaign.get('reserved_usd'):
                raise ContractError('Empty downstream selection cannot allocate work or funds')
            continue
        if allocation != money(campaign['budget_usd']):
            raise ContractError('Accepted downstream envelope is permanent, including cancelled work')
        if scope == MANUAL_SCOPE:
            manual_total += allocation
            manual_count += 1
        else:
            allocations.append(campaign)
        entries.extend((campaign, item) for item in selections)
    for index, campaign in enumerate(sorted(allocations, key=lambda c:c.get('allocation_index', -1)), 1):
        if (campaign.get('allocation_index') != index
                or money(campaign.get('allocation_before_usd')) != total):
            raise ContractError('Downstream cumulative allocation sequence changed')
        total += money(campaign['downstream_allocation_usd'])
        if total > money(campaign['approved_total_usd']):
            raise ContractError('Downstream allocations exceeded their frozen authorization')
    lineages = continuation.lineages(entries)
    return {'shared_policy_usd': total, 'manual_workflow_usd': manual_total,
            'shared_policy_count': len(allocations), 'manual_workflow_count': manual_count,
            'recovery_keys': set(lineages), 'lineages': lineages}


def ledger(state):
    """Retain the shared-policy total API, with once-only keys across both scopes."""
    funding = funding_ledger(state)
    return funding['shared_policy_usd'], funding['recovery_keys']


def refusal(task):
    if task.get('failure_kind') in ('provider_refusal', 'content_filter'):
        return True
    # Older campaigns predate typed outcomes. Conservatively exclude legacy
    # provider refusals/filters; never send them through a workaround prompt.
    text = str(task.get('failure', '')).lower()
    code = str(task.get('provider_failure', {}).get('code', '')).lower()
    return any(word in text or word in code for word in ('refus', 'content_filter', 'content filter', 'safety'))


def usable_candidate(candidate):
    return (isinstance(candidate, dict) and set(candidate) == {'html','title','subtitle','section'}
            and isinstance(candidate['html'], str) and bool(candidate['html'].strip())
            and all(candidate[k] is None or isinstance(candidate[k], str)
                    for k in ('title','subtitle','section')))


def eligible(state, config, task, used, tasks, *, continuation_policy=None, history=(),
             next_strategy=None, automatic=False, at=None):
    if task.get('status') not in ('not_ready', 'budget_blocked') or task.get('batch'):
        return 'not_held'
    if continuation_policy is None and (task.get('downstream_recovery') or recovery_key(task) in used):
        return 'downstream_attempt_exhausted'
    if refusal(task):
        return 'provider_refusal_requires_owner_attention'
    if task.get('failure_kind') == 'incomplete_review':
        return 'review_incomplete_requires_owner_attention'
    if (not task.get('failure_kind') and
            ('Missing, ambiguous, or truncated model response' in task.get('failure', '')
             or task.get('provider_failure', {}).get('code') == 'provider_error')):
        return 'legacy_response_outcome_unknown_requires_owner_attention'
    article = state.read('state/source.json', {}).get('articles', {}).get(task['article_id'])
    if (not article or article['translation_key'] != task['translation_key']
            or article['issue_id'] != task['issue_id']):
        return 'source_changed'
    record = state.record(task['language'], task['article_id'])
    if (record.get('latest_task') != task['id'] or record.get('published') or task.get('protected')
            or any(state.path(f'content/{task["language"]}/articles/{task["article_id"]}.{ext}').exists()
                   for ext in ('html', 'json'))):
        return 'public_or_human_replacement'
    if any(t['language'] == task['language'] and t['article_id'] == task['article_id']
           and t['status'] not in TERMINAL for t in tasks):
        return 'already_processing'
    candidate = state.candidate(task)
    if (not isinstance(candidate, dict) or set(candidate) != {'html','title','subtitle','section'}
            or not isinstance(candidate['html'], str) or not candidate['html'].strip()
            or any(candidate[k] is not None and not isinstance(candidate[k], str)
                   for k in ('title', 'subtitle', 'section'))
            or not task.get('translation_model_actual')):
        if task.get('failure_kind') not in ('truncated', 'invalid_response', 'invalid_result'):
            return 'no_usable_candidate_requires_owner_attention'
    source = state.source(task)
    if source['translation_key'] != task['translation_key'] or source['article']['id'] != task['article_id']:
        raise ContractError('Held task source provenance disagrees with its identity')
    if continuation_policy is not None:
        return continuation.next_reason(state, task, history, continuation_policy, next_strategy,
                                        automatic=automatic, at=at)
    return None


def execution_template(config, request):
    policy = validate_policy(config)
    from .review_contract import frozen_fields
    result = {'model': request['model'], 'review_model': request['review_model'],
        'models': copy.deepcopy(config.models), 'prompt_version': config.runtime['prompt_version'],
        'prompts': {key: config.prompt(key) for key in ('translation', 'review', 'repair')},
        'language_settings': copy.deepcopy(config.languages),
        'glossaries': read_json(config.root/'config/glossaries.json')['languages'],
        'max_output_tokens': policy.get('max_output_tokens', config.runtime['max_output_tokens']),
        'review_output_tokens': policy.get('review_output_tokens', config.review_output_limit(request['review_model'])),
        'quality_threshold': config.runtime['quality_threshold'], **frozen_fields(config)}
    if config.runtime.get('scripture_quotes_enabled', False):
        from .scripture_evidence import policy as scripture_policy
        result['scripture_quotes'] = scripture_policy(config.root)
    return result


def new_task(previous, campaign, item):
    task = {key: copy.deepcopy(previous[key]) for key in
            ('article_id', 'issue_id', 'language', 'translation_key', 'source_snapshot')}
    task.update(id=continuation.task_id(campaign, item), campaign=campaign['id'], created_at=now(),
        status='queued', stage='correct' if item['mode'] == 'repair' else 'translate',
        model=campaign['model'], review_model=campaign['review_model'], models=copy.deepcopy(campaign['models']),
        protected=False, base_html_sha256=None, base_metadata_sha256=None,
        translation_model_actual=previous.get('translation_model_actual'),
        translation_attempts=0, review_attempts=0, events=[], downstream_recovery=True,
        downstream_previous_task=previous['id'], downstream_previous_sha256=item['previous_task_sha256'],
        downstream_candidate_sha256=item['candidate_sha256'], downstream_key=item['recovery_key'],
        findings=copy.deepcopy(previous.get('findings', [])),
        rejection_reason=('Candidate did not pass fidelity review; substantiate findings against the English'
            if previous.get('failure_kind') == 'quality_rejection' or 'Final review failed' in previous.get('failure', '')
            else previous.get('failure', 'Candidate did not pass a quality or structural gate')))
    task.update(copy.deepcopy(item.get('scripture_evidence', {})))
    if 'continuation' in item:
        task['continuation'] = copy.deepcopy(item['continuation'])
        if 'cycle_budget' in item:
            task['cycle_budget'] = copy.deepcopy(item['cycle_budget'])
    return task


def accept(engine, request):
    config, state = engine.config, engine.state
    validate_request(config, request)
    existing = state.read(f'state/campaigns/{request["id"]}.json')
    if existing:
        if existing.get('request_sha256') != json_hash(request) or not existing.get('downstream_recovery'):
            raise ContractError('Campaign identity already exists with different inputs')
        ledger(state)
        return existing
    funding = funding_ledger(state)
    allocated, used = funding['shared_policy_usd'], funding['recovery_keys']
    manual = validate_manual_authorization(request)
    policy = validate_policy(config)
    budget = money(request['budget_usd'])
    if not request['dry_run'] and not manual and allocated + budget > money(policy['total_budget_usd']):
        raise ContractError('Downstream lifetime spending envelope exhausted; prior allocations are never recycled')
    tasks = state.tasks()
    accepted_at = now()
    settings = request.get('continuation_policy')
    template = execution_template(config, request)
    template['id'] = request['id']
    selections, skipped, planned = [], [], money(0)
    records = state.records()
    if settings is not None:
        # Oldest terminal work first, across languages. A just-failed pair cannot
        # monopolize every hourly slot ahead of other waiting articles.
        task_by_id = {task['id']: task for task in tasks}
        records.sort(key=lambda r: (task_by_id.get(r.get('latest_task'), {}).get('finished_at', ''),
                                    r['language'], r['article_id']))
    else:
        records.sort(key=lambda r: (r['language'], r['article_id']))
    for record in records:
        if not record.get('latest_task'):
            continue
        previous = state.read(f'state/tasks/{record["latest_task"]}/task.json')
        if not previous:
            raise ContractError('Latest translation task is missing')
        if previous['status'] not in ('not_ready', 'budget_blocked'):
            continue
        history = funding['lineages'].get(recovery_key(previous), [])
        chosen_strategy = continuation.strategy(template, previous['language'])
        reason = eligible(state, config, previous, used, tasks, continuation_policy=settings,
            history=history, next_strategy=chosen_strategy, automatic='scheduled_hour' in request,
            at=datetime.fromisoformat(accepted_at))
        if reason:
            skipped.append({'previous_task_id': previous['id'], 'reason': reason})
            continue
        candidate = state.candidate(previous)
        item = {'previous_task_id': previous['id'], 'previous_task_sha256': json_hash(previous),
            'article_id': previous['article_id'], 'language': previous['language'],
            'recovery_key': recovery_key(previous), 'candidate_sha256': json_hash(candidate),
            'mode': 'repair' if usable_candidate(candidate) else 'fresh',
            'source_snapshot': previous['source_snapshot'], 'record_before': copy.deepcopy(record),
            'record_before_sha256': json_hash(record)}
        if template.get('scripture_quotes'):
            from .scripture_evidence import freeze_scripture_evidence, ScriptureAttention
            try:
                item['scripture_evidence'] = freeze_scripture_evidence(engine, state.source(previous),
                    previous['language'], frozen_policy=template['scripture_quotes'])
            except ScriptureAttention as exc:
                skipped.append({'previous_task_id': previous['id'], 'reason': exc.reason, 'detail': str(exc)})
                continue
        if settings is not None:
            from .cycle_budget import plan_cycle
            item['continuation'] = {'cycle': len(history) + 1, 'strategy_sha256': chosen_strategy}
            try:
                item['cycle_budget'] = plan_cycle(config, state, new_task(previous, template, item),
                    template, settings['max_candidate_bytes'])
            except ContractError as exc:
                skipped.append({'previous_task_id': previous['id'], 'reason': 'continuation_cycle_context_blocked',
                                'detail': str(exc)})
                continue
            ceiling = money(item['cycle_budget']['total_reserved_usd'])
            if planned + ceiling > budget:
                skipped.append({'previous_task_id': previous['id'], 'reason': 'continuation_cycle_budget_blocked',
                                'required_usd': float(ceiling)})
                continue
            planned += ceiling
        selections.append(item)
        if len(selections) >= request['max_articles']:
            break
    languages = list(dict.fromkeys(item['language'] for item in selections))
    campaign = {'id': request['id'], 'operation': 'repair', 'created_at': accepted_at,
        'requested_by': request.get('requested_by'), 'downstream_recovery': True,
        'downstream_request': copy.deepcopy(request), 'request_sha256': json_hash(request),
        'source_revision': state.read('state/source.json')['revision'],
        'budget_usd': float(budget), 'reserved_usd': 0.0, 'reported_usage_usd': 0.0,
        'downstream_allocation_usd': 0.0 if request['dry_run'] or not selections else float(budget),
        'funding_scope': MANUAL_SCOPE if manual else SHARED_SCOPE,
        'allocation_index': 1 if manual else 1 + funding['shared_policy_count'],
        'allocation_before_usd': 0.0 if manual else float(allocated),
        'approved_total_usd': float(budget if manual else money(policy.get('total_budget_usd', 0))),
        'tasks': [], 'selection': selections, 'skipped': skipped, 'dry_run': request['dry_run'],
        'status': 'planned' if request['dry_run'] else 'acceptance_incomplete',
        'downstream_acceptance_complete': False,
        **template}
    campaign['language_settings'] = {lang: template['language_settings'][lang] for lang in languages}
    if settings is not None:
        campaign['continuation_policy'] = copy.deepcopy(settings)
        campaign['planned_cycle_ceiling_usd'] = float(planned)
    if not selections and not request['dry_run']:
        # No-op requests do not allocate funds. Record them as previews, while
        # retaining the actual request's dry_run value via explicit empty flag.
        campaign['empty_selection'] = True
        campaign['status'] = 'finished'
        campaign['downstream_acceptance_complete'] = True
    campaign['execution_settings_sha256'] = json_hash(execution_settings(campaign))
    campaign['funding_sha256'] = json_hash(funding_settings(campaign))
    state.save_campaign(campaign)
    if request['dry_run'] or not selections:
        return campaign
    engine.checkpoint('runtime: allocate permanent downstream envelope before staging recovery')
    for item in selections:
        previous = state.read(f'state/tasks/{item["previous_task_id"]}/task.json')
        task_id = digest(request['id'] + ':' + item['language'] + ':' + item['article_id'])[:32]
        if state.path(f'state/tasks/{task_id}').exists():
            raise ContractError('Downstream recovery cannot overwrite an existing task')
        task = new_task(previous, campaign, item)
        if item['mode'] == 'repair':
            state.save_candidate(task, state.candidate(previous))
        state.save_task(task)
        record = state.record(task['language'], task['article_id'])
        record['latest_task'] = task_id
        record['history'].append({'event': 'downstream_repair_requested', 'task': task_id,
                                 'previous_task': previous['id'], 'campaign': campaign['id'], 'at': task['created_at']})
        state.save_record(record)
        campaign['tasks'].append(task_id)
    campaign.update(status='active', downstream_acceptance_complete=True)
    state.save_campaign(campaign)
    return campaign


def enqueue_hour(engine):
    policy = validate_policy(engine.config)
    if not policy['enabled']:
        return
    hour = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')
    identity = 'downstream-' + hour.replace('-', '').replace('T', '')
    path = f'state/queue/{identity}.json'
    if engine.state.read(path) is not None:
        return
    allocated, _ = ledger(engine.state)
    if allocated + money(policy['campaign_budget_usd']) > money(policy['total_budget_usd']):
        return
    request = {'id': identity, 'operation': 'repair', 'model': policy['model'],
        'review_model': policy['review_model'], 'budget_usd': policy['campaign_budget_usd'],
        'dry_run': False, 'max_articles': policy['max_articles'], 'scheduled_hour': hour,
        'requested_by': 'owner-authorized-hourly-downstream-policy',
        'continuation_policy': continuation.policy()}
    validate_request(engine.config, request)
    engine.state.write(path, request)
    engine.checkpoint('runtime: persist bounded hourly recovery request before acceptance')


def current(engine, task):
    article = engine.state.read('state/source.json', {}).get('articles', {}).get(task['article_id'])
    return bool(article and article['translation_key'] == task['translation_key']
                and article['issue_id'] == task['issue_id'])


def execution_settings(campaign):
    result = {key: campaign[key] for key in ('model','review_model','models','prompts','prompt_version',
        'language_settings','glossaries','max_output_tokens','review_output_tokens','quality_threshold')}
    if 'review_contract_version' in campaign:
        from .review_contract import frozen_version
        result['review_contract_version'] = frozen_version(campaign)
    if 'scripture_quotes' in campaign:
        result['scripture_quotes'] = campaign['scripture_quotes']
    if 'continuation_policy' in campaign:
        result['continuation_policy'] = campaign['continuation_policy']
        result['planned_cycle_ceiling_usd'] = campaign['planned_cycle_ceiling_usd']
    return result


def validate_history(config, state, tasks):
    funding = funding_ledger(state)
    # Reducing/disabling the live policy pauses future work; it cannot erase a
    # prior authorization. Validate every envelope against its frozen cap.
    for campaign in state.campaigns():
        if not campaign.get('downstream_recovery'):
            continue
        if json_hash(execution_settings(campaign)) != campaign.get('execution_settings_sha256'):
            raise ContractError('Downstream frozen execution settings changed')
        settings = campaign['downstream_request'].get('continuation_policy')
        if campaign.get('continuation_policy') != settings:
            raise ContractError('Downstream continuation authority changed')
        if settings is not None:
            continuation.validate_policy(settings)
            if (money(campaign.get('planned_cycle_ceiling_usd')) != sum(
                    (money(item['cycle_budget']['total_reserved_usd']) for item in campaign['selection']), money(0))
                    or money(campaign['planned_cycle_ceiling_usd']) > money(campaign['budget_usd'])):
                raise ContractError('Downstream complete-cycle reservations exceed their frozen envelope')
        if campaign.get('empty_selection'):
            if (campaign.get('selection') or campaign.get('tasks') or campaign.get('reserved_usd')
                    or campaign.get('downstream_allocation_usd')):
                raise ContractError('Empty downstream request contains work or allocations')
            continue
        if (not campaign['dry_run'] and money(campaign['allocation_before_usd']) +
                money(campaign['downstream_allocation_usd']) > money(campaign['approved_total_usd'])):
            raise ContractError('Downstream allocation exceeded its approved total')
        if len(campaign['selection']) > min(5, campaign['downstream_request']['max_articles']):
            raise ContractError('Downstream selection exceeds its authorized count cap')
        expected = []
        for item in campaign['selection']:
            previous = tasks.get(item['previous_task_id'])
            if (not previous or previous['status'] not in TERMINAL
                    or json_hash(previous) != item['previous_task_sha256']
                    or json_hash(state.candidate(previous)) != item['candidate_sha256']
                    or previous['source_snapshot'] != item['source_snapshot']
                    or recovery_key(previous) != item['recovery_key']
                    or json_hash(item['record_before']) != item['record_before_sha256']
                    or item['language'] != previous['language'] or item['article_id'] != previous['article_id']
                    or item['mode'] != ('repair' if usable_candidate(state.candidate(previous)) else 'fresh')):
                raise ContractError('Downstream recovery original audit evidence changed')
            if settings is not None:
                frozen_source = state.source(previous)
                if (previous['status'] not in ('not_ready', 'budget_blocked') or previous.get('batch')
                        or previous.get('protected') or refusal(previous)
                        or item['record_before'].get('latest_task') != previous['id']
                        or item['record_before'].get('published')
                        or frozen_source['translation_key'] != previous['translation_key']
                        or frozen_source['article']['id'] != previous['article_id']
                        or (not previous.get('failure_kind') and (
                            'Missing, ambiguous, or truncated model response' in previous.get('failure', '')
                            or previous.get('provider_failure', {}).get('code') == 'provider_error'))):
                    raise ContractError('Downstream frozen predecessor was not eligible held work')
                from .cycle_budget import plan_cycle
                planned = plan_cycle(config, state, new_task(previous, campaign, item), campaign,
                                     settings['max_candidate_bytes'])
                if item.get('cycle_budget') != planned:
                    raise ContractError('Downstream complete-cycle reservation changed')
                chosen_strategy = continuation.strategy(campaign, previous['language'])
                if item.get('continuation', {}).get('strategy_sha256') != chosen_strategy:
                    raise ContractError('Downstream strategy provenance changed')
                prefix = [entry for entry in funding['lineages'].get(item['recovery_key'], [])
                          if entry['cycle'] < item['continuation']['cycle']]
                try:
                    accepted_at = datetime.fromisoformat(campaign['created_at'])
                    if accepted_at.tzinfo is None:
                        raise ValueError('missing timezone')
                except (TypeError, ValueError) as exc:
                    raise ContractError('Invalid continuation acceptance time') from exc
                if continuation.next_reason(state, previous, prefix, settings, chosen_strategy,
                        automatic='scheduled_hour' in campaign['downstream_request'], at=accepted_at):
                    raise ContractError('Downstream frozen continuation admission is ineligible')
            identity = digest(campaign['id'] + ':' + previous['language'] + ':' + previous['article_id'])[:32]
            expected.append(identity)
            task = tasks.get(identity)
            record = state.record(previous['language'], previous['article_id'])
            before = item['record_before']
            if record.get('history', [])[:len(before['history'])] != before['history']:
                raise ContractError('Downstream record history must remain append-only')
            events = [event for event in record.get('history', [])[len(before['history']):]
                      if event.get('task') == identity and event.get('campaign') == campaign['id']
                      and event.get('previous_task') == previous['id']
                      and event.get('event') in ('downstream_repair_requested', 'downstream_staging_aborted')]
            if not campaign['dry_run'] and task and not events:
                if campaign.get('downstream_acceptance_complete') or record != before:
                    raise ContractError('Downstream request/abort event is missing from record history')
            if not task:
                candidate = state.read(f'state/tasks/{identity}/candidate.json')
                if candidate is not None and json_hash(candidate) != item['candidate_sha256']:
                    raise ContractError('Partially staged recovery candidate changed')
                continue
            if settings is not None:
                initial = new_task(previous, campaign, item)
                review_result = state.read(f'state/tasks/{identity}/results/{task["stage"]}.json') if task['stage'].startswith('review') else None
                expected_findings = (review_result['result'].get('findings', []) if review_result
                                     and isinstance(review_result.get('result'), dict) else initial['findings'])
                generation = 'correct' if item['mode'] == 'repair' else 'translate'
                generation_result = state.read(f'state/tasks/{identity}/results/{generation}.json')
                if generation_result is not None:
                    generated = generation_result['result']
                    if campaign.get('scripture_quotes') and isinstance(generated, dict) and all(k in generated for k in ('html','title','subtitle','section')):
                        generated = {k: generated.get(k) for k in ('html','title','subtitle','section')}
                    if json_hash(state.candidate(task)) != json_hash(generated):
                        raise ContractError('Downstream candidate differs from its archived generation result')
                elif item['mode'] == 'repair' and json_hash(state.candidate(task)) != item['candidate_sha256']:
                    raise ContractError('Downstream repair input candidate changed before generation')
                if (task.get('findings') != expected_findings
                        or task.get('rejection_reason') != initial['rejection_reason']):
                    raise ContractError('Downstream frozen repair findings or rejection context changed')
            if (not task.get('downstream_recovery') or task.get('campaign') != campaign['id']
                    or any(task.get(key) != campaign[key] for key in ('model', 'review_model', 'models'))
                    or task.get('downstream_previous_task') != previous['id']
                    or task.get('downstream_previous_sha256') != item['previous_task_sha256']
                    or task.get('downstream_candidate_sha256') != item['candidate_sha256']
                    or task.get('downstream_key') != item['recovery_key']
                    or task.get('continuation') != item.get('continuation')
                    or task.get('cycle_budget') != item.get('cycle_budget')
                    or any(task.get(key) != value for key, value in item.get('scripture_evidence', {}).items())
                    or any(task[k] != previous[k] for k in ('language','article_id','issue_id','source_snapshot','translation_key'))
                    or task['stage'] not in (('correct', 'review2') if item['mode'] == 'repair' else ('translate', 'review1'))
                    or task['translation_attempts'] > 1 or task['review_attempts'] > 1):
                raise ContractError('Downstream recovery task provenance or stage limits changed')
        if any(task.get('campaign') == campaign['id'] and task['id'] not in expected for task in tasks.values()):
            raise ContractError('Unexpected downstream child is outside the authorized selection')
        if len(campaign['tasks']) != len(set(campaign['tasks'])):
            raise ContractError('Duplicate downstream child inventory')
        if (any(t not in expected for t in campaign['tasks']) or
                (not campaign['dry_run'] and campaign.get('downstream_acceptance_complete')
                 and campaign['tasks'] != expected)):
            raise ContractError('Downstream selection/task inventory disagrees')


def abort_incomplete(engine, campaign):
    """Retain the allocated envelope and all staged artifacts; cancel only proven
    never-submitted children. There is no rollback or eligibility reset.
    """
    state = engine.state
    tasks = {task['id']: task for task in state.tasks()}
    validate_history(engine.config, state, tasks)
    if any(batch.get('campaign') == campaign['id'] for batch in state.batches()):
        raise ContractError('Incomplete downstream acceptance has batch work; requires investigation')
    expected = [digest(campaign['id'] + ':' + item['language'] + ':' + item['article_id'])[:32]
                for item in campaign['selection']]
    actual = [identity for identity in expected if identity in tasks]
    if any(task.get('campaign') == campaign['id'] and task['id'] not in expected for task in tasks.values()):
        raise ContractError('Unexpected downstream child blocks safe cancellation')
    for identity in actual:
        task = tasks[identity]
        if (task['status'] not in ('queued', 'cancelled') or task.get('batch')
                or task['translation_attempts'] or task['review_attempts'] or task.get('events')
                or state.path(f'state/tasks/{identity}/attempts').exists()
                or state.path(f'state/tasks/{identity}/results').exists()):
            raise ContractError('Cannot prove downstream partial child was never submitted')
    campaign.update(status='acceptance_aborting', cancel_requested=True, tasks=actual)
    state.save_campaign(campaign)
    engine.checkpoint('runtime: record explicit downstream staging abort without freeing allocation')
    for identity in actual:
        task = tasks[identity]
        record = state.record(task['language'], task['article_id'])
        if not any(event.get('task') == identity and event.get('event') in
                   ('downstream_repair_requested','downstream_staging_aborted') for event in record['history']):
            record['history'].append({'event':'downstream_staging_aborted','task':identity,
                'previous_task':task['downstream_previous_task'],'campaign':campaign['id'],'at':now()})
            state.save_record(record)
        engine.finish(tasks[identity], 'cancelled', 'Owner cancelled incomplete downstream acceptance')
    campaign.update(status='acceptance_aborted', downstream_aborted_at=now())
    state.save_campaign(campaign)
    engine.checkpoint('runtime: retain downstream aborted acceptance and immutable source history')
    verify = getattr(engine.gitstore, 'require_published_checkpoint', None)
    if verify:
        verify()
    return campaign


def frontier(config, state, *, tasks=None, funding=None):
    """Derived work status, never authority to alter a terminal task or spend."""
    tasks = state.tasks() if tasks is None else tasks
    funding = funding_ledger(state) if funding is None else funding
    by_id = {task['id']: task for task in tasks}
    policy = validate_policy(config)
    settings = continuation.policy()
    request = {'id': 'frontier-preview', 'model': policy.get('model', 'gpt-6.1-sol'),
               'review_model': policy.get('review_model', 'gpt-6.1-sol')}
    template = execution_template(config, request)
    template['id'] = request['id']
    budget = money(policy.get('campaign_budget_usd', 0))
    cap = money(policy.get('total_budget_usd', 0))
    funding_state = ('paused' if not policy['enabled'] else
                     'budget_exhausted' if funding['shared_policy_usd'] + budget > cap else 'available')
    items, counts = [], {}
    for record in sorted(state.records(), key=lambda r: (r['language'], r['article_id'])):
        task = by_id.get(record.get('latest_task'))
        if not task or task['status'] == 'complete':
            continue
        history = funding['lineages'].get(recovery_key(task), [])
        row = {'language': task['language'], 'article_id': task['article_id'],
               'latest_task': task['id'], 'source_fingerprint': task['translation_key'],
               'processing_status': task['status'], 'accepted_cycles': len(history),
               'remaining_cycles': max(0, settings['max_cycles'] - len(history)),
               'has_publication': bool(record.get('published'))}
        if task['status'] not in TERMINAL:
            reason = 'active'
        elif task['status'] not in ('not_ready', 'budget_blocked'):
            reason = task['status'] + '_requires_attention'
        else:
            chosen = continuation.strategy(template, task['language'])
            reason = eligible(state, config, task, funding['recovery_keys'], tasks,
                continuation_policy=settings, history=history, next_strategy=chosen, automatic=True)
            if reason is None:
                item = {'previous_task_id': task['id'], 'previous_task_sha256': json_hash(task),
                        'candidate_sha256': json_hash(state.candidate(task)),
                        'language': task['language'], 'article_id': task['article_id'],
                        'recovery_key': recovery_key(task),
                        'mode': 'repair' if usable_candidate(state.candidate(task)) else 'fresh'}
                try:
                    from .cycle_budget import plan_cycle
                    plan = plan_cycle(config, state, new_task(task, template, item), template,
                                      settings['max_candidate_bytes'])
                    row['required_cycle_ceiling_usd'] = plan['total_reserved_usd']
                    reason = ('continuation_cycle_budget_blocked' if money(plan['total_reserved_usd']) > budget
                              else 'eligible_' + funding_state)
                except ContractError as exc:
                    reason = 'continuation_cycle_context_blocked'
                    row['detail'] = str(exc)
        row['reason'] = reason
        counts[reason] = counts.get(reason, 0) + 1
        items.append(row)
    return {'format_version': '1', 'derived': True,
            'source_revision': state.read('state/source.json', {}).get('revision'),
            'continuation_policy': settings, 'hourly_funding_state': funding_state,
            'hourly_envelope_usd': float(budget), 'hourly_total_cap_usd': float(cap),
            'hourly_allocated_usd': float(funding['shared_policy_usd']),
            'counts': dict(sorted(counts.items())), 'items': items}
