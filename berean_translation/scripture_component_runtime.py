"""Collector-owned, explicitly gated component admission and request protocol.

Only never-paid manual holds can acquire an additive frozen revision. Original
v1 campaign/request/attention records remain authoritative and unchanged. The
ordinary task, batch, attempt, decision and campaign reservation histories own
execution; this module has no independent lifecycle or funding ledger.
"""
from __future__ import annotations

import copy
from decimal import Decimal

from .common import ContractError, canonical, digest, json_hash, loads, now
from .scripture_evidence import ScriptureAttention
from .scripture_component_evidence import validate_component_evidence, check_component_selections
from .scripture_component_processing import processing_contract, validate_processing_contract

GATE = 'scripture_components_runtime_enabled'
VERSION = '1'
STAGES = ('translate', 'review1', 'correct', 'review2')


def enabled(config):
    value = config.runtime.get(GATE, False)
    if type(value) is not bool:
        raise ContractError('Component runtime gate must be an explicit boolean')
    return value


def require_enabled(config):
    if not enabled(config):
        raise ScriptureAttention('component_runtime_not_enabled', 'Component runtime admission is disabled')


def _same(a, b):
    return canonical(a) == canonical(b)


def _immutable(state, path, value):
    old = state.read(path)
    if old is not None and not _same(old, value):
        raise ContractError('Immutable component runtime artifact changed: ' + path)
    if old is None:
        state.write(path, value)


def is_component(state, task):
    if 'component_revision_sha256' in task:
        return True
    if not task.get('campaign') or not task.get('id'):
        return False
    from .manual_admission import path
    ledger = state.read(path(task['campaign']))
    if ledger is None:
        return False
    if not isinstance(ledger, dict):
        raise ContractError('Malformed manual admission ledger cannot select a runtime contract')
    return 'component_revision' in ledger.get('entries', {}).get(task['id'], {})


def _offline_contract(processing):
    value = copy.deepcopy(processing)
    if value.pop('runtime_protocol_version', None) != VERSION:
        raise ContractError('Unsupported collector component protocol')
    value.update(offline_only=True, funding_authorized=False, publication_authorized=False)
    return value


def _stage_budget(processing):
    from .scripture_admission_revisions import complete_cycle_ceiling
    bound = complete_cycle_ceiling(_offline_contract(processing))
    stages = {stage: float(bound['review_stage_usd' if stage.startswith('review') else 'generation_stage_usd'])
              for stage in STAGES}
    return {'version': 1, 'initial_stage': 'translate',
            'max_candidate_bytes': 500000, 'max_findings_bytes': 32768,
            'stages_usd': stages, 'total_reserved_usd': float(bound['total_usd'])}


def task_fields(revision):
    return {'component_revision_sha256': revision['sha256'],
            'scripture_evidence_path': revision['evidence_path'],
            'scripture_evidence_sha256': revision['evidence_sha256'],
            'stage_budget': copy.deepcopy(revision['stage_budget'])}


def validate_revision(state, campaign, entry):
    """Validate additive authority independently of partial materialization."""
    from .manual_admission import contract
    revision = entry.get('component_revision')
    expected = {'version', 'origin', 'scripture_policy', 'evidence_path', 'evidence_sha256',
                'processing', 'stage_budget', 'original_campaign', 'sha256'}
    if (not isinstance(revision, dict) or set(revision) != expected or revision['version'] != VERSION
            or revision['sha256'] != json_hash({k:v for k,v in revision.items() if k != 'sha256'})):
        raise ContractError('Component admission revision changed')
    original = {k:v for k,v in entry.items() if k != 'component_revision'}
    origin, processing = revision['origin'], revision['processing']
    proof = entry.get('provenance')
    if (not isinstance(origin, dict) or not proof or entry['status'] != 'attention'
            or origin.get('entry_sha256') != json_hash(original)
            or origin.get('campaign_contract_sha256') != json_hash(contract(campaign))
            or origin.get('request_sha256') != campaign.get('request_sha256')
            or origin.get('campaign_id') != campaign['id'] or origin.get('entry_id') != entry['task_id']
            or origin.get('task_template_sha256') != json_hash(proof['task'])
            or origin.get('record_sha256') != proof['record_sha256']
            or origin.get('models_sha256') != json_hash(campaign['models'])
            or origin.get('budget_usd') != campaign['budget_usd']
            or proof['previous_task_id'] is not None or proof['candidate_path'] is not None
            or proof['task']['stage'] != 'translate' or proof['task'].get('protected')
            or any(proof['task'].get(k) != 0 for k in ('translation_attempts', 'review_attempts'))):
        raise ContractError('Original never-paid component admission provenance changed')
    frozen = _offline_contract(processing)
    validate_processing_contract(frozen)
    projected_campaign = copy.deepcopy(revision['original_campaign'])
    if not _same(contract(projected_campaign), contract(campaign)):
        raise ContractError('Component origin campaign differs from original manual authority')
    projected_campaign['scripture_quotes'] = copy.deepcopy(revision['scripture_policy'])
    # The original campaign's mutable progress is intentionally excluded from
    # provenance reconstruction; its exact initial projection was frozen below.
    projected_task = {**copy.deepcopy(proof['task']), 'models': copy.deepcopy(campaign['models'])}
    source = state.source(projected_task)
    if (processing['origin_campaign_sha256'] != json_hash(projected_campaign)
            or processing['article_id'] != proof['task']['article_id']
            or processing['language'] != proof['task']['language']
            or processing['glossary'] != campaign['glossaries'].get(proof['task']['language'], {})
            or not processing['prompts']['generation'].startswith(campaign['prompts']['translation'] + '\n\n')
            or not processing['prompts']['review'].startswith(campaign['prompts']['review'] + '\n\n')
            or processing['task_id'] != entry['task_id'] or processing['campaign_id'] != campaign['id']
            or processing['origin_task_sha256'] != json_hash(projected_task)
            or processing['source_sha256'] != origin['source_sha256']
            or json_hash(source) != origin['source_sha256']
            or processing['scripture_policy_sha256'] != json_hash(revision['scripture_policy'])
            or processing['generation_model'] != campaign['model']
            or processing['review_model'] != campaign['review_model']
            or processing['language_settings'] != campaign['language_settings'][proof['task']['language']]
            or any(processing[k] != campaign[k] for k in
                   ('quality_threshold', 'max_output_tokens', 'review_output_tokens'))
            or any(processing['models'].get(name) != campaign['models'][name]
                   for name in (campaign['model'], campaign['review_model']))
            or not _same(revision['stage_budget'], _stage_budget(processing))):
        raise ContractError('Component processing changed the original frozen limits')
    evidence = state.read(revision['evidence_path'])
    if (revision['evidence_path'] != f'state/scripture/{revision["evidence_sha256"]}.json'
            or json_hash(evidence) != revision['evidence_sha256']):
        raise ContractError('Component admission evidence changed')
    validate_component_evidence(source, evidence, revision['scripture_policy'])
    if evidence['language_tag'] != processing['language_settings']['tag']:
        raise ContractError('Component evidence language differs from the original manual target')
    task = state.read(f'state/tasks/{entry["task_id"]}/task.json')
    if task is not None:
        expected_task = {**projected_task, **task_fields(revision)}
        mutable = {'stage', 'status', 'translation_attempts', 'review_attempts', 'events', 'translation_model_actual'}
        if any(not _same(task.get(k), v) for k,v in expected_task.items() if k not in mutable):
            raise ContractError('Component task changed its frozen admission provenance')
    return revision


def load(state, task, *, require_fresh=False):
    from . import manual_admission
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    ledger = state.read(manual_admission.path(task['campaign']))
    if not campaign or not ledger:
        raise ContractError('Component task has no authoritative manual admission')
    manual_admission.validate(state, campaign, ledger)
    entry = ledger['entries'].get(task['id'])
    if not entry or 'component_revision' not in entry:
        raise ContractError('Component task has no frozen revision')
    revision = validate_revision(state, campaign, entry)
    expected = {**entry['provenance']['task'], 'models':campaign['models'], **task_fields(revision)}
    mutable = {'stage', 'status', 'translation_attempts', 'review_attempts', 'events', 'translation_model_actual'}
    if any(not _same(task.get(k),v) for k,v in expected.items() if k not in mutable):
        raise ContractError('Supplied component task differs from its admission')
    validate_attempt_counts(state, task)
    evidence = state.read(revision['evidence_path'])
    validate_component_evidence(state.source(task), evidence, revision['scripture_policy'], require_fresh=require_fresh)
    return revision, evidence


def materialized(state, campaign, entry):
    revision = validate_revision(state, campaign, entry)
    task = state.read(f'state/tasks/{entry["task_id"]}/task.json')
    from .manual_admission import requested_event
    record = state.record(entry['item']['language'], entry['item']['article_id'])
    return bool(task and task['id'] in campaign['tasks'] and requested_event(task) in record['history'])


def resume(engine, campaign, entry):
    """Reconcile exact pre-billable writes after the immutable admission intent."""
    from . import manual_admission
    state = engine.state
    revision = validate_revision(state, campaign, entry)
    if materialized(state, campaign, entry):
        return state.read(f'state/tasks/{entry["task_id"]}/task.json')
    require_enabled(engine.config)
    source, record = manual_admission._guard(engine, campaign, entry)
    human_ownership_guard(state, entry['provenance']['task'])
    task = {**copy.deepcopy(entry['provenance']['task']), 'models': copy.deepcopy(campaign['models']),
            **task_fields(revision)}
    existing = state.read(f'state/tasks/{task["id"]}/task.json')
    if existing is not None and not _same(existing, task):
        raise ContractError('Incomplete component admission cannot reuse attempted work')
    if campaign.get('cancel_requested'):
        raise ContractError('Cancelled component admission cannot materialize new work')
    state.save_task(task)
    event = manual_admission.requested_event(task)
    if event not in record['history']:
        record['history'].append(event)
    record['latest_task'] = task['id']
    state.save_record(record)
    if task['id'] not in campaign['tasks']:
        campaign['tasks'].append(task['id'])
    campaign['status'] = 'active'
    state.save_campaign(campaign)
    engine.checkpoint('runtime: materialize never-paid component revision in the original manual envelope')
    return task


def accept(engine, campaign_id, entry_id, scripture_policy, evidence):
    from . import manual_admission
    from .scripture_admission_revisions import inspect_never_paid_hold
    require_enabled(engine.config)
    state = engine.state
    campaign = state.read(f'state/campaigns/{campaign_id}.json')
    ledger = state.read(manual_admission.path(campaign_id))
    if not campaign or not ledger or entry_id not in ledger.get('entries', {}):
        raise ContractError('No original manual admission hold')
    entry = ledger['entries'][entry_id]
    if 'component_revision' in entry:
        revision = validate_revision(state, campaign, entry)
        if not _same(revision['scripture_policy'], scripture_policy) or revision['evidence_sha256'] != json_hash(evidence):
            raise ContractError('Conflicting component revision cannot replace accepted history')
        return resume(engine, campaign, entry)
    inspected = inspect_never_paid_hold(engine, campaign_id, entry_id)
    human_ownership_guard(state, entry['provenance']['task'])
    validate_component_evidence(inspected['source'], evidence, scripture_policy, require_fresh=True)
    projected = copy.deepcopy(campaign)
    projected['scripture_quotes'] = copy.deepcopy(scripture_policy)
    task = {**copy.deepcopy(entry['provenance']['task']), 'models':copy.deepcopy(campaign['models'])}
    processing = processing_contract(engine.config.root, projected, task,
                                    maximum_result_bytes=engine.config.runtime['max_result_bytes'])
    for key in ('offline_only', 'funding_authorized', 'publication_authorized'):
        processing.pop(key)
    processing['runtime_protocol_version'] = VERSION
    budget = _stage_budget(processing)
    if committed_total(state, campaign) + Decimal(str(budget['total_reserved_usd'])) > Decimal(str(campaign['budget_usd'])):
        raise ContractError('Complete component cycle does not fit the original manual envelope')
    sha = json_hash(evidence)
    revision = {'version':VERSION, 'origin':inspected['origin'], 'scripture_policy':copy.deepcopy(scripture_policy),
                'evidence_path':f'state/scripture/{sha}.json', 'evidence_sha256':sha,
                'processing':processing, 'stage_budget':budget, 'original_campaign':copy.deepcopy(campaign)}
    revision['sha256'] = json_hash(revision)
    _immutable(state, revision['evidence_path'], evidence)
    entry['component_revision'] = revision
    manual_admission.save(state, ledger)
    engine.checkpoint('runtime: freeze additive component admission and full-cycle commitment')
    return resume(engine, campaign, entry)


def has_revisions(state, campaign):
    from .manual_admission import path
    ledger = state.read(path(campaign['id']), {})
    return any('component_revision' in entry for entry in ledger.get('entries', {}).values())


def committed_total(state, campaign):
    """Original permanent reservations plus unspent permanent chain earmarks.

    Every subtraction is recomputed from exact prepared payload bytes and the
    original model rates. Any interrupted batch/counter inventory is a blocker,
    never a source of extra spending room.
    """
    from .manual_admission import path
    from .requests import reserve_cost
    ledger = state.read(path(campaign['id']), {})
    revisions = {identity:entry['component_revision'] for identity,entry in ledger.get('entries', {}).items()
                 if 'component_revision' in entry}
    if not revisions:
        return Decimal(str(max(campaign.get('reserved_usd', 0), campaign.get('reported_usage_usd', 0))))
    batches = {batch['id']:batch for batch in state.batches() if batch['campaign'] == campaign['id']}
    spent, reserved, seen = {identity:Decimal(0) for identity in revisions}, Decimal(0), set()
    for batch in batches.values():
        payload = state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Original campaign prepared payload changed')
        rows = [loads(raw) for raw in payload.splitlines()]
        expected_ids = [identity + ':' + batch['stage'] for identity in batch['tasks']]
        if (not rows or batch['custom_ids'] != expected_ids
                or [row.get('custom_id') for row in rows] != expected_ids
                or len(set(expected_ids)) != len(expected_ids)):
            raise ContractError('Original campaign prepared request inventory changed')
        if batch.get('reservation_reused_from'):
            parent = batches.get(batch['reservation_reused_from'])
            if (not parent or parent.get('replacement_batch') != batch['id']
                    or parent.get('status') != 'cancelled_before_submission'
                    or parent.get('exclusion_reason') != 'human_editorial_authority'
                    or parent.get('remote_id') or (parent.get('submission_started_at') and parent.get('create_not_called') is not True)
                    or any(not _same(batch.get(k), parent.get(k)) for k in ('campaign','stage','model','reserved_usd'))
                    or batch['tasks'] != [x for x in parent['tasks'] if x not in parent.get('excluded_task_ids', [])]):
                raise ContractError('Original campaign reservation transfer is unproven')
            original = state.path(f'state/batches/{parent["id"]}/input.jsonl').read_bytes()
            if payload != b''.join(raw for raw in original.splitlines(keepends=True) if loads(raw)['custom_id'] in expected_ids):
                raise ContractError('Transferred prepared payload changed')
            continue
        amounts = Decimal(0)
        for identity, row in zip(batch['tasks'], rows):
            key = identity + ':' + batch['stage']
            if key in seen:
                raise ContractError('A component campaign cannot reserve the same task stage twice')
            seen.add(key)
            task = state.read(f'state/tasks/{identity}/task.json')
            if not task or task['campaign'] != campaign['id']:
                raise ContractError('Original campaign batch has an unresolved task')
            chosen = task['review_model'] if batch['stage'].startswith('review') else task['model']
            model = campaign['models'][chosen]
            body = row['body']
            if batch['model'] != chosen or body['model'] != model['api_model']:
                raise ContractError('Original campaign prepared model changed')
            amount = Decimal(str(reserve_cost(model, len(canonical(body)) + 4096, body['max_completion_tokens'])))
            amounts += amount
            if identity in revisions:
                request = validate_saved_request(state, task, batch['stage'])
                if not _same(request['line'], row) or amount != Decimal(str(request['estimated_usd'])):
                    raise ContractError('Component reservation differs from exact prepared request')
                spent[identity] += amount
        if amounts != Decimal(str(batch['reserved_usd'])):
            raise ContractError('Original campaign batch reservation differs from frozen payload pricing')
        reserved += amounts
    if reserved != Decimal(str(campaign.get('reserved_usd', 0))):
        raise ContractError('Original campaign reservation inventory is inconsistent; reconcile before proceeding')
    total = max(reserved, Decimal(str(campaign.get('reported_usage_usd', 0))))
    for identity, revision in revisions.items():
        ceiling = Decimal(str(revision['stage_budget']['total_reserved_usd']))
        if spent[identity] > ceiling:
            raise ContractError('Component stage reservations exceed their frozen complete-cycle commitment')
        total += ceiling - spent[identity]
    return total


def incremental_cost(state, task, cost):
    # Component stages spend an already-earmarked chain. Other tasks compete for
    # remaining uncommitted room, while every actual reservation remains intact.
    return Decimal(0) if is_component(state, task) else Decimal(str(cost))


def _audit(state, task, revision, evidence, candidate):
    audit = state.read(f'state/tasks/{task["id"]}/scripture-selections.json')
    if not isinstance(audit, dict) or set(audit) != {'version', 'candidate_sha256', 'selections', 'proof', 'generation_stage'}:
        raise ContractError('Missing complete component selection audit')
    proof = check_component_selections(evidence, candidate, audit['selections'], source=state.source(task),
                                       contract=revision['scripture_policy'])
    if audit['version'] != VERSION or audit['candidate_sha256'] != json_hash(candidate) or not _same(audit['proof'], proof):
        raise ContractError('Component candidate or selection proof changed')
    stage = audit['generation_stage']
    if stage not in ('translate', 'correct'):
        raise ContractError('Unknown component generation provenance')
    result = validated_result(state, task, stage)['result']
    if not _same(result.get('candidate'), candidate) or not _same(result.get('scripture_selections'), audit['selections']):
        raise ContractError('Component audit differs from the exact archived generation')
    return audit


def validate_candidate(state, task, candidate):
    revision, evidence = load(state, task)
    return _audit(state, task, revision, evidence, candidate)


def request(config, state, task):
    require_enabled(config)
    revision, evidence = load(state, task, require_fresh=True)
    execution_guard(state, task)
    contract = revision['processing']
    stage = task['stage']
    if stage not in STAGES:
        raise ContractError('Unknown component stage')
    validate_request_position(state, task)
    review = stage.startswith('review')
    model = contract['models'][contract['review_model'] if review else contract['generation_model']]
    language, source = contract['language_settings'], state.source(task)
    payload = {'target_language':language['name'], 'language_tag':language['tag'],
               'language_guidance':language['guidance'], 'terminology_glossary':contract['glossary'],
               'source':{'html':source['html'], **{k:source['article'].get(k) for k in ('title', 'subtitle', 'section')}},
               'source_context':{'byline':source['article'].get('byline')}, 'scripture_evidence':evidence}
    candidate = proof = None
    if stage != 'translate':
        candidate = state.candidate(task)
        audit = _audit(state, task, revision, evidence, candidate)
        proof = audit['proof']
        payload.update(translation=candidate, scripture_selection_audit=proof, scripture_selections=audit['selections'])
    if stage == 'correct':
        payload['correction_findings'] = task.get('findings', [])
    kind = 'review' if review else 'generation'
    output = min(contract['review_output_tokens'] if review else contract['max_output_tokens'], model['max_output_tokens'])
    body = {'model':model['api_model'], 'messages':[{'role':'system','content':contract['prompts'][kind]},
            {'role':'user','content':canonical(payload).decode('utf-8')}], 'max_completion_tokens':output,
            'response_format':{'type':'json_schema','json_schema':{'name':'component_' + kind + '_v1',
                               'strict':True, 'schema':contract['schemas'][kind]}}}
    if model.get('reasoning_effort'):
        body['reasoning_effort'] = model['reasoning_effort']
    binding = {'version':VERSION, 'stage':stage, 'ordinal':STAGES.index(stage) + 1,
               'context_sha256':revision['sha256'], 'source_sha256':json_hash(source),
               'evidence_sha256':json_hash(evidence), 'processing_contract_sha256':json_hash(contract),
               'input_sha256':json_hash(body), 'candidate_sha256':json_hash(candidate) if candidate is not None else None,
               'selection_proof_sha256':json_hash(proof) if proof is not None else None}
    payload['expected_binding'] = binding
    body['messages'][1]['content'] = canonical(payload).decode('utf-8')
    line = {'custom_id':task['id'] + ':' + stage, 'method':'POST', 'url':'/v1/chat/completions', 'body':body}
    bound = len(canonical(body)) + 4096
    if bound + output > model['context_tokens']:
        raise ContractError('Complete component request exceeds frozen model context; never truncate')
    from .requests import reserve_cost
    cost = reserve_cost(model, bound, output)
    value = {'version':VERSION, 'line':line, 'request_sha256':json_hash(line), 'binding':binding,
             'estimated_usd':cost, 'input_bound':bound}
    validate_saved_request(state, task, stage, proposed=value)
    _immutable(state, f'state/tasks/{task["id"]}/requests/{stage}.json', value)
    return line, cost, bound


def validate_pending(state, task, row, result, provenance):
    revision, evidence = load(state, task)
    stage = task['stage']
    if len(canonical(row)) > revision['processing']['maximum_result_bytes'] + 65536:
        raise ContractError('Complete component response exceeds its frozen byte bound')
    if len(row.get('response', {}).get('body', {}).get('choices', [{}])[0].get('message', {}).get('content', '').encode('utf-8')) > revision['processing']['maximum_result_bytes']:
        raise ContractError('Component model output exceeds its frozen result byte bound')
    request = validate_saved_request(state, task, stage)
    batch = state.read(f'state/batches/{task.get("batch")}/batch.json')
    if (task['status'] != 'in_batch' or not request or not batch or task['id'] not in batch['tasks']
            or batch['stage'] != stage or batch['status'] not in ('submitted', 'cancelling')
            or row.get('custom_id') != task['id'] + ':' + stage
            or type(row.get('response', {}).get('status_code')) is not int
            or request['request_sha256'] != json_hash(request['line'])
            or provenance['model'] != request['line']['body']['model']):
        raise ContractError('Component response lacks its exact submitted request provenance')
    payload = state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
    if digest(payload) != batch['payload_sha256'] or canonical(request['line']) + b'\n' not in payload.splitlines(keepends=True):
        raise ContractError('Component response request differs from prepared batch bytes')
    message = row['response']['body']['choices'][0]['message']
    if message.get('refusal') is not None:
        raise ContractError('Component response contains a refusal')
    expected = {'binding', 'review'} if stage.startswith('review') else {'binding', 'candidate', 'scripture_selections'}
    if set(result) != expected or not _same(result['binding'], request['binding']):
        raise ContractError('Component response binding or schema changed')
    return revision, evidence


def receive(engine, task, row, result, provenance):
    """Use ordinary State histories and return via the Engine's bounded lifecycle."""
    from .attempts import decision
    from .requests import accepted_review
    from .review_contract import validate_review
    from .html import validate_translation
    state, stage = engine.state, task['stage']
    revision, evidence = validate_pending(state, task, row, result, provenance)
    path = f'state/tasks/{task["id"]}/results/{stage}.json'
    old = state.read(path)
    value = {'result':result, 'provenance':provenance, 'received_at':old['received_at'] if old else now()}
    _immutable(state, path, value)
    event = {'stage':stage, 'model':provenance['model'], 'usage':provenance['usage']}
    if event not in task['events']:
        task['events'].append(event)
    if stage in ('translate', 'correct'):
        candidate = result['candidate']
        state.save_candidate(task, candidate)
        proof = check_component_selections(evidence, candidate, result['scripture_selections'],
                    source=state.source(task), contract=revision['scripture_policy'])
        validate_translation(state.source(task), candidate, language=task['language'])
        from .stage_budget import enforce
        enforce(task, candidate)
        audit = {'version':VERSION, 'candidate_sha256':json_hash(candidate),
                 'selections':result['scripture_selections'], 'proof':proof, 'generation_stage':stage}
        # Stage-specific immutable audits survive later corrections; this pointer
        # follows the authoritative current candidate just like candidate.json.
        _immutable(state, f'state/tasks/{task["id"]}/component-selections/{stage}.json', audit)
        state.write(f'state/tasks/{task["id"]}/scripture-selections.json', audit)
        task['translation_model_actual'] = provenance['model']
        decision(state, task, stage, 'structural_pass')
        task['stage'] = 'review1' if stage == 'translate' else 'review2'
    else:
        validate_review(result['review'], 2)
        verdict = result['review']
        validate_candidate(state, task, state.candidate(task))
        task.update(review_model_actual=provenance['model'], quality_score=verdict['score'], findings=verdict['findings'])
        from .stage_budget import enforce
        enforce(task, state.candidate(task))
        if not verdict['findings_complete']:
            task['failure_kind'] = 'incomplete_review'
            decision(state, task, stage, 'incomplete_review', findings=task['findings'])
            return engine.finish(task, 'not_ready', 'Component independent review findings are incomplete')
        if accepted_review(verdict, revision['processing']['quality_threshold'], contract_version=2):
            decision(state, task, stage, 'quality_pass', findings=task['findings'])
            state.save_task(task)
            return engine.publish(task)
        decision(state, task, stage, 'quality_rejection', findings=task['findings'])
        if stage == 'review2' or not task['findings']:
            task['failure_kind'] = 'quality_rejection'
            return engine.finish(task, 'not_ready', 'Component final review failed; no additional cycle is authorized')
        task['stage'] = 'correct'
    task['status'] = 'queued'
    task.pop('batch', None)
    state.save_task(task)


def human_ownership_guard(state, task):
    """Permanent human exclusion, including committed deletion of absent files."""
    from .gitstore import GitStore, NoHumanEditEvidence
    resolver = getattr(state, 'component_gitstore', None)
    gitstore = resolver() if resolver is not None else getattr(state, 'gitstore', None)
    gitstore = gitstore or GitStore(state.root, publish=False)
    for extension in ('html', 'json'):
        destination = f'content/{task["language"]}/articles/{task["article_id"]}.{extension}'
        try:
            gitstore.human_edit_evidence(destination, all_history=True)
        except NoHumanEditEvidence:
            continue
        raise ContractError('Committed human ownership excludes component admission and execution')


def execution_guard(state, task):
    human_ownership_guard(state, task)
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    if campaign.get('cancel_requested'):
        raise ContractError('Original component campaign is cancelled')
    record = state.record(task['language'], task['article_id'])
    if (record.get('published') or {}).get('human_reviewed') or any(event.get('event') in ('human_review','human_notice_standardized') for event in record['history']):
        raise ContractError('Human work excludes component execution')
    if record.get('latest_task') != task['id']:
        raise ContractError('Another task displaced the component admission')
    current = state.read('state/source.json', {}).get('articles', {}).get(task['article_id'])
    if not current or current['translation_key'] != task['translation_key']:
        raise ContractError('English source changed before component execution')


def validate_saved_request(state, task, stage, *, proposed=None):
    """Rebuild exact submitted protocol inputs from immutable stage artifacts."""
    revision, evidence = load(state, task)
    contract = revision['processing']
    record = proposed if proposed is not None else state.read(f'state/tasks/{task["id"]}/requests/{stage}.json')
    if not isinstance(record, dict) or set(record) != {'version','line','request_sha256','binding','estimated_usd','input_bound'}:
        raise ContractError('Missing exact component request artifact')
    line = record['line']
    review = stage.startswith('review')
    kind = 'review' if review else 'generation'
    model = contract['models'][contract['review_model'] if review else contract['generation_model']]
    source, language = state.source(task), contract['language_settings']
    payload = {'target_language':language['name'], 'language_tag':language['tag'],
               'language_guidance':language['guidance'], 'terminology_glossary':contract['glossary'],
               'source':{'html':source['html'], **{k:source['article'].get(k) for k in ('title','subtitle','section')}},
               'source_context':{'byline':source['article'].get('byline')}, 'scripture_evidence':evidence}
    candidate = proof = None
    if stage != 'translate':
        generation = 'correct' if stage == 'review2' else 'translate'
        archived = validated_result(state, task, generation)['result']
        decision = state.read(f'state/tasks/{task["id"]}/decisions/{generation}.json', {})
        if decision.get('outcome') != 'structural_pass':
            raise ContractError('Component stage requires its structurally accepted exact generation')
        candidate, selections = archived.get('candidate'), archived.get('scripture_selections')
        proof = check_component_selections(evidence, candidate, selections, source=source, contract=revision['scripture_policy'])
        payload.update(translation=candidate, scripture_selection_audit=proof, scripture_selections=selections)
    if stage == 'correct':
        verdict = validated_result(state, task, 'review1')['result'].get('review', {})
        from .review_contract import validate_review
        from .requests import accepted_review
        validate_review(verdict, 2)
        decision = state.read(f'state/tasks/{task["id"]}/decisions/review1.json', {})
        if decision.get('outcome') != 'quality_rejection' or not _same(decision.get('findings'), verdict['findings']):
            raise ContractError('Component correction lacks its exact rejected independent-review decision')
        if not verdict['findings_complete'] or not verdict['findings'] or accepted_review(verdict, contract['quality_threshold'], contract_version=2):
            raise ContractError('Component correction has no complete rejected independent review')
        payload['correction_findings'] = verdict['findings']
    output = min(contract['review_output_tokens'] if review else contract['max_output_tokens'], model['max_output_tokens'])
    body = {'model':model['api_model'], 'messages':[{'role':'system','content':contract['prompts'][kind]},
            {'role':'user','content':canonical(payload).decode('utf-8')}], 'max_completion_tokens':output,
            'response_format':{'type':'json_schema','json_schema':{'name':'component_' + kind + '_v1',
                               'strict':True, 'schema':contract['schemas'][kind]}}}
    if model.get('reasoning_effort'):
        body['reasoning_effort'] = model['reasoning_effort']
    binding = {'version':VERSION, 'stage':stage, 'ordinal':STAGES.index(stage) + 1,
               'context_sha256':revision['sha256'], 'source_sha256':json_hash(source),
               'evidence_sha256':json_hash(evidence), 'processing_contract_sha256':json_hash(contract),
               'input_sha256':json_hash(body), 'candidate_sha256':json_hash(candidate) if candidate is not None else None,
               'selection_proof_sha256':json_hash(proof) if proof is not None else None}
    payload['expected_binding'] = binding
    body['messages'][1]['content'] = canonical(payload).decode('utf-8')
    expected = {'custom_id':task['id'] + ':' + stage, 'method':'POST','url':'/v1/chat/completions','body':body}
    from .requests import reserve_cost
    bound = len(canonical(body)) + 4096
    if (not _same(line, expected) or record['version'] != VERSION
            or record['request_sha256'] != json_hash(expected) or not _same(record['binding'], binding)
            or record['input_bound'] != bound or bound + output > model['context_tokens']
            or not _same(record['estimated_usd'], reserve_cost(model, bound, output))):
        raise ContractError('Exact component request differs from its frozen stage provenance')
    return record


def repeated_response(state, task, row, maximum_bytes):
    """An already consumed stage can only replay its exact archived response."""
    if row.get('custom_id') == task['id'] + ':' + task['stage']:
        return False
    prefix = task['id'] + ':'
    if not isinstance(row.get('custom_id'), str) or not row['custom_id'].startswith(prefix):
        raise ContractError('Component response belongs to a different task')
    stage = row['custom_id'][len(prefix):]
    if stage not in STAGES:
        raise ContractError('Unknown component response stage')
    from .requests import parse_response
    from .attempts import evidence
    result, provenance = parse_response(row, maximum_bytes)
    if (type(row.get('response', {}).get('status_code')) is not int
            or row['response']['body']['choices'][0]['message'].get('refusal') is not None):
        raise ContractError('Malformed replayed component response envelope')
    saved = state.read(f'state/tasks/{task["id"]}/results/{stage}.json')
    attempt = state.read(f'state/tasks/{task["id"]}/attempts/{stage}.json', {}).get('response')
    if (not saved or not _same(saved['result'], result) or not _same(saved['provenance'], provenance)
            or not _same(attempt, evidence(row, maximum_bytes))):
        raise ContractError('Conflicting response cannot replace consumed component history')
    return True


def preparation_intent(state, campaign, selected, batch):
    if not any(is_component(state, task) for task in selected):
        return
    from .manual_admission import contract
    intent = {'version':VERSION, 'campaign_contract_sha256':json_hash(contract(campaign)),
              'campaign_reserved_before':campaign['reserved_usd'],
              'campaign_reserved_after':round(campaign['reserved_usd'] + batch['reserved_usd'], 6),
              'tasks_before':copy.deepcopy(selected), 'payload_sha256':batch['payload_sha256'],
              'batch_id':batch['id']}
    intent['sha256'] = json_hash(intent)
    batch['component_preparation'] = intent


def reconcile_preparations(engine):
    """Finish only exact local reservation writes proved before any provider call."""
    from .manual_admission import contract
    state = engine.state
    for batch in state.batches():
        intent = batch.get('component_preparation')
        if intent is None or batch.get('component_preparation_complete') is True:
            continue
        if (intent.get('version') != VERSION or intent.get('sha256') != json_hash({k:v for k,v in intent.items() if k != 'sha256'})
                or intent.get('batch_id') != batch['id'] or intent.get('payload_sha256') != batch['payload_sha256']
                or batch['status'] != 'prepared' or batch.get('input_file_id') or batch.get('remote_id') or batch.get('submission_started_at')):
            raise ContractError('Incomplete component preparation cannot be reconciled safely')
        campaign = state.read(f'state/campaigns/{batch["campaign"]}.json')
        if (json_hash(contract(campaign)) != intent['campaign_contract_sha256']
                or campaign['reserved_usd'] not in (intent['campaign_reserved_before'], intent['campaign_reserved_after'])
                or intent['campaign_reserved_after'] != round(intent['campaign_reserved_before'] + batch['reserved_usd'], 6)
                or [task['id'] for task in intent['tasks_before']] != batch['tasks']):
            raise ContractError('Component preparation conflicts with existing campaign authority')
        payload = state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Component preparation payload changed')
        expected_tasks = []
        for before in intent['tasks_before']:
            if before['status'] != 'queued' or before['stage'] != batch['stage'] or before.get('batch'):
                raise ContractError('Component preparation did not start from an unreserved stage')
            after = copy.deepcopy(before)
            after.update(status='in_batch', batch=batch['id'])
            counter = 'review_attempts' if batch['stage'].startswith('review') else 'translation_attempts'
            after[counter] += 1
            existing = state.read(f'state/tasks/{before["id"]}/task.json')
            if not (_same(existing, before) or _same(existing, after)):
                raise ContractError('Component preparation task changed before reconciliation')
            expected_tasks.append(after)
        # Validate every remaining write before repairing any of them.
        for task in expected_tasks:
            state.save_task(task)
        campaign['reserved_usd'] = intent['campaign_reserved_after']
        state.save_campaign(campaign)
        batch['component_preparation_complete'] = True
        state.save_batch(batch)
        engine.checkpoint('runtime: reconcile exact component reservation before any provider call')


def publish(engine, task):
    """Idempotent first publication, with an immutable intent before file writes."""
    from .html import notice, validate_translation
    from .common import read_regular_bytes
    from .engine import write_text  # Keep the collector's single publication write boundary.
    state = engine.state
    require_enabled(engine.config)
    revision, evidence = load(state, task)
    if not _same(state.read(f'state/tasks/{task["id"]}/task.json'), task):
        raise ContractError('Component publication task differs from authoritative current State')
    if task['status'] != 'in_batch':
        raise ContractError('Component publication requires the current submitted independent review')
    batch = state.read(f'state/batches/{task.get("batch")}/batch.json', {})
    if batch.get('status') not in ('submitted','cancelling') or task['id'] not in batch.get('tasks', []) or batch.get('stage') != task['stage']:
        raise ContractError('Component publication lacks its submitted review batch')
    execution_guard(state, task)
    source, candidate = state.source(task), state.candidate(task)
    validate_translation(source, candidate, language=task['language'])
    audit = validate_candidate(state, task, candidate)
    if task['stage'] not in ('review1', 'review2'):
        raise ContractError('Component publication requires independent review')
    request = validate_saved_request(state, task, task['stage'])
    result = validated_result(state, task, task['stage'])['result']
    decision = state.read(f'state/tasks/{task["id"]}/decisions/{task["stage"]}.json', {})
    if decision.get('outcome') != 'quality_pass' or decision.get('findings') != result.get('review', {}).get('findings'):
        raise ContractError('Component publication lacks its accepted immutable review decision')
    from .requests import accepted_review
    if (not _same(result.get('binding'), request['binding'])
            or request['binding']['candidate_sha256'] != json_hash(candidate)
            or request['binding']['selection_proof_sha256'] != json_hash(audit['proof'])
            or not accepted_review(result.get('review', {}), revision['processing']['quality_threshold'], contract_version=2)):
        raise ContractError('Component publication lacks its exact complete independent review')
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    record = state.record(task['language'], task['article_id'])
    if task.get('protected') or any(event.get('event') in ('human_review','human_notice_standardized') for event in record['history']):
        raise ContractError('Human work is excluded from component publication')
    model = task.get('translation_model_actual')
    if not model or not task.get('review_model_actual'):
        raise ContractError('Component publication lacks actual model provenance')
    footer = notice(campaign['language_settings'][task['language']], task['article_id'], model, engine.config.runtime['english_route'])
    text = candidate['html'].strip() + '\n\n' + footer + '\n'
    metadata = {k:candidate[k] for k in ('title','subtitle','section')}
    html_path = f'content/{task["language"]}/articles/{task["article_id"]}.html'
    metadata_path = html_path[:-5] + '.json'
    path = f'state/tasks/{task["id"]}/component-publication.json'
    intent = state.read(path)
    publication = {'html_path':html_path,'metadata_path':metadata_path,'source_snapshot':task['source_snapshot'],
        'source_revision':source['revision'],'translation_key':task['translation_key'],'issue_id':task['issue_id'],
        'model':model,'review_model':task['review_model_actual'],'human_reviewed':False,'human_review':None,
        'notice_html':footer,'html_sha256':digest(text),'metadata_sha256':json_hash(metadata),
        'quality_score':result['review']['score'],'published_at':intent['publication']['published_at'] if intent else now(),
        'task':task['id']}
    if intent is None:
        if record.get('published') or engine.human_protected(task['language'], task['article_id']):
            raise ContractError('Existing publication updates and untracked human files remain excluded')
        value = {'version':VERSION,'task_id':task['id'],'revision_sha256':revision['sha256'],
                 'candidate_sha256':json_hash(candidate),'review_result_sha256':json_hash(result),
                 'record_before':copy.deepcopy(record),'publication':publication,'html':text,'metadata':metadata}
        value['sha256'] = json_hash(value)
        _immutable(state, path, value)
        intent = value
    if (not isinstance(intent, dict) or set(intent) != {'version','task_id','revision_sha256','candidate_sha256',
            'review_result_sha256','record_before','publication','html','metadata','sha256'}
            or intent['sha256'] != json_hash({k:v for k,v in intent.items() if k != 'sha256'})
            or intent['version'] != VERSION or intent['task_id'] != task['id'] or intent['revision_sha256'] != revision['sha256']
            or intent['candidate_sha256'] != json_hash(candidate) or intent['review_result_sha256'] != json_hash(result)
            or not _same(intent['publication'], publication) or intent['html'] != text or not _same(intent['metadata'], metadata)
            or intent['record_before'].get('published')):
        raise ContractError('Frozen first-publication intent changed')
    expected_record = {**copy.deepcopy(intent['record_before']), 'published':publication}
    if not (_same(record, intent['record_before']) or _same(record, expected_record)):
        raise ContractError('Publication or human record changed after component intent')
    # Intent proves destination absence before the first write. Recovery may
    # complete only absent or exact own bytes, never an intervening human edit.
    for destination, expected in ((html_path, text.encode('utf-8')), (metadata_path, None)):
        target = state.path(destination)
        if target.exists() or target.is_symlink():
            if destination == html_path:
                same = read_regular_bytes(target) == expected
            else:
                same = _same(state.read(destination), metadata)
            if not same:
                raise ContractError('Human or concurrent file change blocks component publication replay')
    if not state.path(html_path).exists():
        write_text(state.path(html_path), text)
    if not state.path(metadata_path).exists():
        state.write(metadata_path, metadata)
    if not _same(record, expected_record):
        state.save_record(expected_record)
    engine.finish(task, 'complete')
    reconcile_terminal(engine, task)


def reconcile_terminal(engine, task):
    """Complete only missing audit writes of an already terminal task."""
    from .state import TERMINAL
    from .attempts import decision
    if task['status'] not in TERMINAL or not task.get('finished_at'):
        return
    state = engine.state
    load(state, task)
    decision(state, task, 'terminal', task['status'], task.get('failure'), task.get('findings', []))
    record = state.record(task['language'], task['article_id'])
    event = {'event':task['status'],'task':task['id'],'at':task['finished_at'],'reason':task.get('failure')}
    if event not in record['history']:
        if any(e.get('task') == task['id'] and e.get('event') == task['status'] for e in record['history']):
            raise ContractError('Component terminal history conflicts with its exact result')
        record['history'].append(event)
        state.save_record(record)


def cancel_materialized(engine, campaign, entry):
    """Cancel an exact partial never-paid child without creating an absent one."""
    from .manual_admission import requested_event
    state = engine.state
    revision = validate_revision(state, campaign, entry)
    task = state.read(f'state/tasks/{entry["task_id"]}/task.json')
    if task is None or materialized(state, campaign, entry):
        return
    expected = {**entry['provenance']['task'], 'models':campaign['models'], **task_fields(revision)}
    if (task.get('status') not in ('queued','cancelled') or task.get('batch')
            or any(task.get(k) != 0 for k in ('translation_attempts','review_attempts'))
            or any(not _same(task.get(k),v) for k,v in expected.items() if k not in ('status','events'))):
        raise ContractError('Partial component cancellation lacks exact never-paid provenance')
    record = state.record(task['language'], task['article_id'])
    event = requested_event(task)
    if event not in record['history']:
        record['history'].append(event)
    if record.get('latest_task') not in (None, task['id']):
        raise ContractError('Another task replaced the partial component admission')
    record['latest_task'] = task['id']
    state.save_record(record)
    if task['id'] not in campaign['tasks']:
        campaign['tasks'].append(task['id'])
    state.save_campaign(campaign)
    engine.finish(task, 'cancelled', 'Campaign cancelled by owner during component admission')
    reconcile_terminal(engine, task)


def archive_unprocessed(engine, task, batch, row):
    """Retain paid terminal/cancelled results and usage without advancing work."""
    from .attempts import archive
    from .requests import parse_response, usage_cost
    state = engine.state
    stage = batch['stage']
    if row.get('custom_id') != task['id'] + ':' + stage or task['id'] not in batch['tasks']:
        raise ContractError('Late component result has no matching original batch')
    request = validate_saved_request(state, task, stage)
    archived_task = {**task, 'stage':stage}
    observed = archive(state, archived_task, row, engine.config.runtime['max_result_bytes'])
    value = {'version':VERSION, 'stage':stage, 'batch_id':batch['id'],
             'request_sha256':request['request_sha256'], 'response':observed}
    try:
        result, provenance = parse_response(row, engine.config.runtime['max_result_bytes'])
        value.update(result=result, provenance=provenance)
    except ContractError:
        pass  # Full bounded allowlisted response evidence is already retained.
    _immutable(state, f'state/tasks/{task["id"]}/component-collected/{stage}.json', value)
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    model = task['models'][task['review_model'] if stage.startswith('review') else task['model']]
    amount = usage_cost(model, observed.get('usage') or {})
    key = task['id'] + ':' + stage
    if amount is not None and key not in campaign.get('accounted_responses', {}):
        campaign.setdefault('accounted_responses', {})[key] = amount
        campaign['reported_usage_usd'] = round(campaign['reported_usage_usd'] + amount, 8)
        state.save_campaign(campaign)


def validated_result(state, task, stage):
    """Bind parsed results to the immutable provider text and exact stage request."""
    saved = state.read(f'state/tasks/{task["id"]}/results/{stage}.json')
    attempt = state.read(f'state/tasks/{task["id"]}/attempts/{stage}.json', {}).get('response', {})
    request = validate_saved_request(state, task, stage)
    if (not isinstance(saved, dict) or set(saved) != {'result','provenance','received_at'}
            or attempt.get('outcome') != 'structured_response' or attempt.get('finish_reason') != 'stop'
            or attempt.get('content_evidence_truncated') is not False
            or not isinstance(attempt.get('content'), str) or attempt.get('refusal')
            or digest(attempt['content']) != attempt.get('content_sha256')
            or len(attempt['content'].encode('utf-8')) != attempt.get('content_bytes')
            or not _same(loads(attempt['content']), saved['result'])
            or saved['provenance'].get('model') != attempt.get('model')
            or saved['provenance'].get('model') != request['line']['body']['model']
            or not _same(saved['provenance'].get('usage'), attempt.get('usage'))
            or not _same(saved['result'].get('binding'), request['binding'])):
        raise ContractError('Parsed component result differs from immutable provider attempt evidence')
    return saved


def validate_attempt_counts(state, task):
    stages = set()
    for batch in state.batches():
        if task['id'] not in batch.get('tasks', []) or batch.get('reservation_reused_from'):
            continue
        if batch['campaign'] != task['campaign'] or batch['stage'] not in STAGES or batch['stage'] in stages:
            raise ContractError('Component attempt history has a duplicate or foreign root reservation')
        stages.add(batch['stage'])
    expected = {'translation_attempts':sum(stage in ('translate','correct') for stage in stages),
                'review_attempts':sum(stage.startswith('review') for stage in stages)}
    if any(type(task.get(k)) is not int or task[k] != value or value > 2 for k,value in expected.items()):
        raise ContractError('Component attempt counters differ from immutable root reservations')
    return stages


def validate_request_position(state, task):
    if not _same(state.read(f'state/tasks/{task["id"]}/task.json'), task):
        raise ContractError('Requested component stage differs from authoritative current task')
    if task['status'] not in ('queued','in_batch'):
        raise ContractError('A terminal component task cannot prepare a new request')
    stages = validate_attempt_counts(state, task)
    position = STAGES.index(task['stage']) + (task['status'] == 'in_batch')
    if stages != set(STAGES[:position]):
        raise ContractError('Component request stage is not the exact next unconsumed predecessor stage')
    if task['status'] == 'in_batch':
        batch = state.read(f'state/batches/{task.get("batch")}/batch.json', {})
        if task['id'] not in batch.get('tasks', []) or batch.get('stage') != task['stage']:
            raise ContractError('Component request has no current reservation owner')


def validate_history(state):
    """Read-only validation independent of the deployment gate."""
    from .manual_admission import path
    for campaign in state.campaigns():
        if not has_revisions(state, campaign):
            continue
        ledger = state.read(path(campaign['id']))
        if committed_total(state, campaign) > Decimal(str(campaign['budget_usd'])):
            raise ContractError('Component commitments exceed the original manual envelope')
        for identity, entry in ledger['entries'].items():
            if 'component_revision' not in entry:
                continue
            validate_revision(state, campaign, entry)
            task = state.read(f'state/tasks/{identity}/task.json')
            if task is None:
                continue
            load(state, task)
            for stage in STAGES:
                if state.read(f'state/tasks/{identity}/requests/{stage}.json') is not None:
                    validate_saved_request(state, task, stage)
                if state.read(f'state/tasks/{identity}/results/{stage}.json') is not None:
                    # Invalid responses are intentionally retained as failed
                    # attempts; only proven accepted stages support future work.
                    decision = state.read(f'state/tasks/{identity}/decisions/{stage}.json', {})
                    if decision.get('outcome') in ('structural_pass','quality_pass','quality_rejection','incomplete_review'):
                        validated_result(state, task, stage)
