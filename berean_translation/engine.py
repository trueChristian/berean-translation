"""Resumable, cost-bounded translation state machine.

Manual and authorized source-refresh requests are immutable queue entries. Only the serialized worker mutates
campaign/task/batch records. Checkpoints precede every potentially billable
Batch creation. Uncertain submissions are reconciled, never blindly retried.
"""
from __future__ import annotations
import copy
import re
from collections import defaultdict
from pathlib import Path
from uuid import uuid4
from .batch_telemetry import failure_reason, observe_batch, request_failure
from .common import (ContractError, canonical, csv_values, digest, json_hash, loads, now,
                     positive_money, read_json, read_regular_bytes, write_text)
from .html import notice, validate_translation
from .requests import accepted_review, build_request, parse_response
from .refresh import enqueue_source_refreshes
from .recovery import is_recovery_campaign, plan_recovery, recorded_request, recovery_selector
from .state import State, TERMINAL
from . import downstream, autonomous, stage_budget, manual_admission
from .attempts import archive, decision
from .scripture_evidence import (ScriptureAttention, normalize_scripture_candidate,
    validate_scripture_candidate, freeze_scripture_evidence, policy as scripture_policy,
    adopt_scripture_selection_audit)

BATCH_TERMINAL = {'completed','failed','expired','cancelled'}


def may_continue(check):
    """A soft runtime boundary, never permission to interrupt a checkpoint."""
    return check is None or check()


class Engine:
    def __init__(self, config, source_client, provider, gitstore):
        self.config, self.source_client, self.provider, self.gitstore = config,source_client,provider,gitstore
        self.state = State(config.root)

    def checkpoint(self, message):
        self.state.derive(self.config)
        self.gitstore.checkpoint(message)

    def discover(self):
        discovered = self.source_client.discover()
        self.state.write('state/source.json',discovered)
        return discovered

    def human_protected(self, language, article_id):
        record = self.state.record(language, article_id)
        if any(event.get('event') in ('human_review', 'human_notice_standardized') for event in record.get('history', [])):
            return True
        pub = record.get('published')
        if not pub:
            try:
                return any(self.state.path(f'content/{language}/articles/{article_id}.{ext}').exists()
                           for ext in ('html', 'json'))
            except (ContractError, OSError):
                return True  # An unrecognized working path is never AI-owned.
        if pub['human_reviewed'] or pub.get('edit_issue'):
            return True
        try:
            return (digest(read_regular_bytes(self.state.path(pub['html_path']))) != pub['html_sha256']
                    or json_hash(self.state.read(pub['metadata_path'])) != pub['metadata_sha256'])
        except (ContractError, OSError, UnicodeError, KeyError):
            return True

    def select_issues(self, selection, languages, operation, retry_failed=False, *, claim_index=None):
        if claim_index is None:
            claim_index = manual_admission.claims(self.state)
        source = self.state.read('state/source.json')
        issues = source['issues']
        if selection in ('all','outstanding'):
            return [i['id'] for i in issues]
        if selection == 'next':
            for issue in issues:
                for article in source['articles'].values():
                    if article['issue_id'] == issue['id'] and any(
                        self.eligible(article,lang,operation,retry_failed, claim_index=claim_index)[0] for lang in languages):
                        return [issue['id']]
            return []
        aliases = {}
        for issue in issues:
            for key in (issue['id'],issue['slug'],issue['source_id']):
                if key in aliases and aliases[key] != issue['id']:
                    raise ContractError('Ambiguous source issue selector')
                aliases[key] = issue['id']
        try:
            selected = [aliases[x] for x in csv_values(selection)]
        except KeyError as exc:
            raise ContractError(f'Unknown issue selector {exc.args[0]}; see STATUS.md') from exc
        if len(selected) != len(set(selected)):
            raise ContractError('Issue aliases selected the same issue twice')
        return selected

    def eligible(self, article, language, operation, retry_failed, *, claim_index=None):
        if manual_admission.covered(self.state, language, article['id'], article['translation_key'], claim_index=claim_index):
            return False, 'manual_admission_pending'
        record = self.state.record(language,article['id'])
        latest_id = record.get('latest_task')
        task = self.state.read(f'state/tasks/{latest_id}/task.json') if latest_id else None
        if task and task['status'] not in TERMINAL:
            return False,'already_processing'
        pub = record.get('published')
        if self.human_protected(language, article['id']):
            return False,'human_reviewed_or_edited_protected'
        if operation == 'review':
            if pub or (task and self.state.candidate(task)):
                return True,'review'
            return False,'no_existing_translation'
        if pub and pub['human_reviewed']:
            return False,'human_reviewed_protected'
        if pub and pub['translation_key'] == article['translation_key']:
            return False,'already_translated'
        if task and task['translation_key'] == article['translation_key'] and not retry_failed:
            return False,'already_attempted_use_review_or_explicit_retry'
        return True,'new_or_changed_source'

    def accept_request(self, request):
        manual_admission.restore_orphans(self.state)
        if request.get('autonomous'):
            return autonomous.accept(self, request)
        if request.get('operation') == 'repair':
            return downstream.accept(self, request)
        identity = request.get('id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity):
            raise ContractError('Invalid queue request identity')
        recovery = recovery_selector(request, self.config.runtime['max_tasks_per_request'])
        existing = self.state.read(f'state/campaigns/{identity}.json')
        if existing is None:
            existing = manual_admission.restore_orphan(self.state, request)
        if existing:
            if ((existing.get('request_sha256') and existing['request_sha256'] != json_hash(request))
                    or (recovery and not existing.get('request_sha256'))):
                raise ContractError('Campaign identity already exists with different immutable inputs')
            if existing.get('recovery_of_campaign'):
                recorded_request(self.state, existing)
            if self.state.read(manual_admission.path(identity)) or manual_admission.needs_resume(self.state, existing):
                return manual_admission.resume(self, existing, request)
            return existing
        operation = request.get('operation')
        if operation not in ('translate','review'):
            raise ContractError('Request operation must be translate or review')
        languages = [] if recovery else self.config.select_languages(request.get('languages','all'))
        model = request.get('model') or self.config.runtime['default_model']
        review_model = request.get('review_model') or self.config.runtime['default_review_model']
        self.config.model(model); self.config.model(review_model)
        budget = positive_money(request.get('budget_usd',self.config.runtime['default_budget_usd']),self.config.runtime['max_campaign_usd'])
        retry = request.get('retry_failed',False)
        dry_run = request.get('dry_run',False)
        if type(retry) is not bool or type(dry_run) is not bool:
            raise ContractError('Request dry_run and retry_failed must be booleans')
        refresh = request.get('source_refresh',False)
        if type(refresh) is not bool:
            raise ContractError('Request source_refresh must be a boolean')
        claim_index = manual_admission.claims(self.state)
        selected = [] if recovery else self.select_issues(request.get('issues','next'),languages,operation,retry, claim_index=claim_index)
        source_index = self.state.read('state/source.json')
        refresh_ids, refresh_keys = None, None
        if refresh:
            policy = self.config.runtime.get('automatic_source_refresh',{})
            if (not policy.get('enabled') or operation != 'translate' or len(languages) != 1
                    or len(selected) != 1 or retry or dry_run
                    or model != policy['model'] or review_model != policy['review_model']
                    or budget > policy['budget_usd']):
                raise ContractError('Source refresh must follow the enabled one-issue/language model and budget policy')
            refresh_ids = request.get('article_ids')
            refresh_keys = request.get('source_translation_keys')
            if (not isinstance(refresh_ids,list) or not refresh_ids
                    or not all(isinstance(item,str) for item in refresh_ids)
                    or len(refresh_ids) != len(set(refresh_ids))
                    or not isinstance(refresh_keys,dict) or set(refresh_keys) != set(refresh_ids)
                    or not all(isinstance(key,str) and re.fullmatch(r'[a-f0-9]{64}',key)
                               for key in refresh_keys.values())):
                raise ContractError('Source refresh requires exact article identities and source fingerprints')
            if len(refresh_ids) > self.config.runtime['max_tasks_per_request']:
                raise ContractError('Source refresh group exceeds max_tasks_per_request; no automatic splitting or spending is allowed')
            if any(identity not in source_index['articles'] or
                   source_index['articles'][identity]['issue_id'] != selected[0] for identity in refresh_ids):
                raise ContractError('Source refresh articles must still belong to the selected issue')
        elif 'article_ids' in request or 'source_translation_keys' in request:
            raise ContractError('Exact source-refresh selection fields require source_refresh=true')
        frozen_recovery, recovery_budget = {}, None
        if recovery:
            planned, frozen_recovery, recovery_budget = plan_recovery(self, request, budget, recovery)
            skipped = []
            languages = list(dict.fromkeys(item['language'] for item in planned))
            selected = list(dict.fromkeys(source_index['articles'][item['article_id']]['issue_id'] for item in planned))
        else:
            articles = sorted((a for a in source_index['articles'].values() if a['issue_id'] in selected),
                              key=lambda a:(selected.index(a['issue_id']),a['sequence'],a['id']))
            if refresh:
                articles = [a for a in articles if a['id'] in refresh_ids]
            attempted = {(t['language'],t['article_id'],t['translation_key']) for t in self.state.tasks()} if refresh else set()
            planned, skipped = [],[]
            for article in articles:
                for language in languages:
                    allowed, reason = self.eligible(article,language,operation,retry, claim_index=claim_index)
                    if refresh:
                        pub = self.state.record(language,article['id']).get('published')
                        if not pub:
                            allowed, reason = False,'source_refresh_requires_existing_publication'
                        elif article['translation_key'] != refresh_keys[article['id']]:
                            allowed, reason = False,'source_changed_after_refresh_request'
                        elif (language,article['id'],article['translation_key']) in attempted:
                            allowed, reason = False,'source_fingerprint_already_attempted'
                    (planned if allowed else skipped).append({'article_id':article['id'],'language':language,'reason':reason})
        if len(planned) > self.config.runtime['max_tasks_per_request']:
            raise ContractError('Selection exceeds max_tasks_per_request; choose fewer issues/languages')
        campaign = {'id':identity,'request_sha256':json_hash(request),'operation':operation,'created_at':now(),'requested_by':request.get('requested_by'),
                    'source_revision':source_index['revision'],'budget_usd':budget,'reserved_usd':0.0,
                    'reported_usage_usd':0.0,'tasks':[],'skipped':skipped,'selection':planned,'dry_run':dry_run,
                    'status':'planned' if dry_run else 'active','languages':languages,'issues':selected,
                    'model':model,'review_model':review_model,'models':copy.deepcopy(self.config.models),
                    'prompt_version':self.config.runtime['prompt_version'],
                    'structural_feedback_version':self.config.runtime.get('structural_feedback_version'),
                    'prompts':{name:self.config.prompt(name) for name in ('translation','review')},
                    'language_settings':{lang:copy.deepcopy(self.config.languages[lang]) for lang in languages},
                    'glossaries':read_json(self.config.root/'config/glossaries.json')['languages'],
                    'max_output_tokens':self.config.runtime['max_output_tokens'],
                    'review_output_tokens':self.config.review_output_limit(review_model),
                    'quality_threshold':self.config.runtime['quality_threshold']}
        from .review_contract import frozen_fields
        campaign.update(frozen_fields(self.config))
        if self.config.runtime.get('scripture_quotes_enabled', False):
            campaign['scripture_quotes'] = scripture_policy(self.config.root)
        if refresh:
            campaign.update(source_refresh=True,source_translation_keys=copy.deepcopy(refresh_keys),
                            refresh_policy=copy.deepcopy(self.config.runtime['automatic_source_refresh']))
        if recovery:
            campaign.update(recovery_of_campaign=recovery[0], previous_task_ids=list(recovery[1]),
                            recovery_allocation_usd=0.0 if dry_run else budget, recovery_budget=recovery_budget,
                            recovery_acceptance_complete=False, recovery_request=copy.deepcopy(request))
        if recovery and campaign.get('scripture_quotes') and not dry_run:
            frozen_scripture = {}
            for item in planned:
                saved = frozen_recovery[item['previous_task_id']]
                probe = {'id': digest(identity + ':' + item['language'] + ':' + item['article_id'])[:32],
                         'campaign': identity, 'source_snapshot': item['source_snapshot']}
                fields = freeze_scripture_evidence(self, saved['source'], item['language'],
                                                   frozen_policy=campaign['scripture_quotes'])
                probe.update(fields)
                view = stage_budget.PlanningState(self.state, campaign, saved['source'], saved['candidate'])
                adopted = adopt_scripture_selection_audit(view, probe, saved['previous'])
                frozen_scripture[item['previous_task_id']] = {'fields':fields,
                    'stage':'review1' if adopted else 'correct',
                    'audit':view.writes.get(f'state/tasks/{probe["id"]}/scripture-selections.json')}
            campaign['recovery_scripture'] = frozen_scripture
            campaign['recovery_scripture_sha256'] = json_hash(frozen_scripture)
        if manual_admission.supported(campaign):
            return manual_admission.resume(self, campaign, request, new=True)
        # Persist the accepted full envelope before creating any recovery work.
        # This allocation is never freed, even if acceptance/staging later fails.
        self.state.save_campaign(campaign)
        if dry_run:
            # Selection preview is free; no tasks are reserved and no API objects are created.
            return campaign
        if recovery:
            campaign['status'] = 'acceptance_incomplete'
            self.state.save_campaign(campaign)
            self.checkpoint('runtime: allocate exact recovery envelope before materializing tasks')
        for item in planned:
            article_id, language = item['article_id'],item['language']
            article = source_index['articles'][article_id]
            record = self.state.record(language,article_id)
            previous = self.state.read(f'state/tasks/{record.get("latest_task")}/task.json') if record.get('latest_task') else None
            try:
                source = (frozen_recovery[item['previous_task_id']]['source'] if recovery
                          else self.source_client.snapshot(article_id))
            except (ContractError,UnicodeError) as exc:
                campaign['skipped'].append({**item,'reason':'source_error','detail':str(exc)})
                continue
            snapshot_path = (item['source_snapshot'] if recovery else f'state/sources/{json_hash(source)}.json')
            if not recovery:
                self.state.write(snapshot_path,source)
            task_id = digest(identity + ':' + language + ':' + article_id)[:32]
            pub = record.get('published')
            task = {'id':task_id,'campaign':identity,'article_id':article_id,'issue_id':article['issue_id'],
                    'language':language,'translation_key':article['translation_key'],'source_snapshot':snapshot_path,
                    'created_at':now(),'status':'queued','stage':'review1' if operation == 'review' else 'translate',
                    'model':model,'review_model':review_model,'models':copy.deepcopy(self.config.models),
                    'protected':bool(pub and pub['human_reviewed']),
                    'base_html_sha256':pub['html_sha256'] if pub else None,
                    'base_metadata_sha256':pub['metadata_sha256'] if pub else None,
                    'translation_model_actual':pub['model'] if pub else None,
                    'translation_attempts':0,'review_attempts':0,'events':[]}
            if operation == 'review':
                candidate = (frozen_recovery[item['previous_task_id']]['candidate'] if recovery else
                             self.state.publication_candidate(pub)[0] if pub else self.state.candidate(previous))
                self.state.save_candidate(task,candidate)
                if not task['translation_model_actual'] and previous:
                    task['translation_model_actual'] = previous.get('translation_model_actual')
            if recovery:
                task.update(recovery_of_task=item['previous_task_id'],
                            recovery_candidate_sha256=item['candidate_sha256'],
                            recovery_previous_task_sha256=item['previous_task_sha256'])
            if recovery and campaign.get('recovery_scripture'):
                evidence = campaign['recovery_scripture'][item['previous_task_id']]
                task.update(evidence['fields'], stage=evidence['stage'])
                if evidence['audit'] is not None:
                    self.state.write(f'state/tasks/{task["id"]}/scripture-selections.json', evidence['audit'])
            elif campaign.get('scripture_quotes'):
                try:
                    task.update(freeze_scripture_evidence(self, source, language, frozen_policy=campaign['scripture_quotes']))
                    if task['stage'].startswith('review') and not adopt_scripture_selection_audit(self.state, task, previous):
                        task['stage'] = 'correct'
                        task['scripture_resume_reason'] = 'saved_candidate_requires_quotation_audit'
                except ScriptureAttention as exc:
                    campaign['skipped'].append({**item, 'reason': exc.reason, 'detail': str(exc)})
                    continue
            self.state.save_task(task)
            record['latest_task'] = task_id
            record['history'].append({'event':'requested','task':task_id,'campaign':identity,'at':task['created_at']})
            self.state.save_record(record)
            campaign['tasks'].append(task_id)
        if recovery:
            campaign['recovery_acceptance_complete'] = True
            campaign['status'] = 'active'
        self.state.save_campaign(campaign)
        return campaign

    def accept_queue(self, *, continue_work=None):
        count = 0
        for path in sorted((self.config.root/'state/queue').glob('*.json'), key=lambda p: (
                p.stem.startswith('auto-'),
                self.state.read(f'state/automatic-holds/{p.stem}.json', {}).get('last_checked_at', ''), p.name)):
            if not may_continue(continue_work):
                break
            request = read_json(path)
            existing = self.state.read(f'state/campaigns/{path.stem}.json')
            if (existing is not None and not manual_admission.needs_resume(self.state, existing)
                    or self.state.read(f'state/queue-errors/{path.stem}.json') is not None):
                continue
            if count >= self.config.runtime['max_pending_campaigns_per_tick']:
                break
            if request.get('id') != path.stem:
                raise ContractError('Queue filename and identity mismatch')
            if autonomous.owns_automatic_work(self.config, self.state) and (request.get('source_refresh') or (request.get('operation') == 'repair' and not request.get('manual_authorization'))):
                continue  # Unaccepted legacy automatic requests are superseded by the shared authority.
            if request.get('autonomous') and not autonomous.enabled(self.config):
                continue
            if request.get('source_refresh') is True and not self.config.runtime.get('automatic_source_refresh',{}).get('enabled'):
                continue  # Disabling the policy pauses unaccepted automatic requests.
            if request.get('autonomous'):
                count += 1  # Held automatic admissions still consume this pass's bounded page.
            try:
                self.accept_request(request)
            except ScriptureAttention as exc:
                self.state.write(f'state/automatic-holds/{path.stem}.json', {'reason': exc.reason, 'detail': str(exc), 'last_checked_at': now()})
                continue  # Evidence can recover without a new paid or manual request.
            except autonomous.BudgetUnavailable:
                continue  # A budget hold is resumable, never a permanent queue error.
            except ContractError as exc:
                self.state.write(f'state/queue-errors/{path.stem}.json',{'error':str(exc),'request':path.stem})
                # Invalid queue entries are durable diagnostics, not a reason to stop other requests.
                continue
            if not request.get('autonomous'):
                count += 1
        self.checkpoint('runtime: accept pending translation requests')

    def finish(self, task, status, reason=None):
        if task['status'] in TERMINAL:
            return  # A replay cannot rewrite a terminal task or append duplicate history.
        task['status'],task['finished_at'] = status,now()
        task.pop('batch',None)
        if reason:
            task['failure'] = reason
        self.state.save_task(task)
        decision(self.state, task, 'terminal', status, reason, task.get('findings', []))
        record = self.state.record(task['language'],task['article_id'])
        record['history'].append({'event':status,'task':task['id'],'at':task['finished_at'],'reason':reason})
        self.state.save_record(record)

    def publish(self, task):
        if 'continuation' in task:
            downstream.validate_history(self.config, self.state, {t['id']:t for t in self.state.tasks()})
        if (task.get('downstream_recovery') or task.get('autonomous')) and not downstream.current(self, task):
            return self.finish(task, 'source_error', 'English source changed during downstream recovery')
        record = self.state.record(task['language'],task['article_id'])
        pub = record.get('published')
        if task['protected'] or (pub and (pub['human_reviewed'] or pub.get('edit_issue'))):
            return self.finish(task,'proposal','AI suggestions never overwrite human-reviewed work')
        source, candidate = self.state.source(task),self.state.candidate(task)
        validate_scripture_candidate(self.state, task, candidate)
        validate_translation(source,candidate,language=task['language'])
        if pub:
            if (digest(read_regular_bytes(self.state.path(pub['html_path']))) != task['base_html_sha256'] or
                    json_hash(self.state.read(pub['metadata_path'])) != task['base_metadata_sha256']):
                return self.finish(task,'proposal','Published content changed while this task was running')
        elif task['base_html_sha256'] is not None:
            return self.finish(task,'proposal','Previous publication changed or was removed')
        campaign = self.state.read(f'state/campaigns/{task["campaign"]}.json')
        model = task['translation_model_actual']
        if not model:
            raise ContractError('Cannot publish without the actual translation model identity')
        footer = notice(campaign['language_settings'][task['language']],task['article_id'],model,
                        self.config.runtime['english_route'])
        html_path = f'content/{task["language"]}/articles/{task["article_id"]}.html'
        metadata_path = html_path[:-5] + '.json'
        if not pub and (self.state.path(html_path).exists() or self.state.path(metadata_path).exists()):
            return self.finish(task,'proposal','Untracked existing translation files were not overwritten')
        text = candidate['html'].strip() + '\n\n' + footer + '\n'
        metadata = {k:candidate[k] for k in ('title','subtitle','section')}
        write_text(self.state.path(html_path),text)
        self.state.write(metadata_path,metadata)
        record['published'] = {'html_path':html_path,'metadata_path':metadata_path,
                'source_snapshot':task['source_snapshot'],'source_revision':source['revision'],
                'translation_key':task['translation_key'],'issue_id':task['issue_id'],
                'model':model,'review_model':task.get('review_model_actual'),
                'human_reviewed':False,'human_review':None,'notice_html':footer,
                'html_sha256':digest(text),'metadata_sha256':json_hash(metadata),
                'quality_score':task['quality_score'],'published_at':now(),'task':task['id']}
        self.state.save_record(record)
        self.finish(task,'complete')

    def receive(self, task, row):
        if task['status'] in TERMINAL:
            return
        stage = task['stage']
        observed = archive(self.state, task, row, self.config.runtime['max_result_bytes'])
        task['failure_kind'] = observed['outcome']
        from .requests import usage_cost
        campaign = self.state.read(f'state/campaigns/{task["campaign"]}.json')
        pricing = task['models'][task['review_model'] if stage.startswith('review') else task['model']]
        cost = usage_cost(pricing, observed.get('usage') or {})
        usage_key = task['id'] + ':' + stage
        if cost is not None and usage_key not in campaign.get('accounted_responses', {}):
            campaign.setdefault('accounted_responses', {})[usage_key] = cost
            campaign['reported_usage_usd'] = round(campaign['reported_usage_usd'] + cost, 8)
            self.state.save_campaign(campaign)
        try:
            result, provenance = parse_response(row,self.config.runtime['max_result_bytes'])
            self.state.write(f'state/tasks/{task["id"]}/results/{stage}.json',
                             {'result':result,'provenance':provenance,'received_at':now()})
            task['events'].append({'stage':stage,'model':provenance['model'],'usage':provenance['usage']})
            campaign = self.state.read(f'state/campaigns/{task["campaign"]}.json')
            if stage in ('translate','correct'):
                task['translation_model_actual'] = provenance['model']
                raw_result = result
                if campaign.get('scripture_quotes') and isinstance(result, dict) and all(k in result for k in ('html','title','subtitle','section')):
                    result = {k:result[k] for k in ('html','title','subtitle','section')}
                self.state.save_candidate(task,result)
                try:
                    result = normalize_scripture_candidate(self.state, task, raw_result)
                    self.state.save_candidate(task,result)
                    stage_budget.enforce(task, result)
                    if 'cycle_budget' in task:
                        from .cycle_budget import enforce_candidate_bound
                        try:
                            enforce_candidate_bound(result, task['cycle_budget']['max_candidate_bytes'])
                        except ContractError:
                            task['failure_kind'] = 'candidate_size_limit'
                            raise
                    validate_translation(self.state.source(task),result,language=task['language'])
                except ContractError as exc:
                    diagnostic_findings = None
                    if (stage == 'translate' and not task.get('downstream_recovery')
                            and campaign.get('structural_feedback_version') == '1'):
                        from .structural_feedback import correction_findings
                        diagnostic_findings = correction_findings(self.state.source(task), result, exc)
                    decision(self.state, task, stage, 'structural_rejection', str(exc), diagnostic_findings)
                    if stage == 'translate' and not task.get('downstream_recovery'):
                        if diagnostic_findings is not None:
                            task['findings'] = diagnostic_findings
                        else:
                            # Frozen legacy campaigns retain their original correction request bytes.
                            task['findings'] = [{'severity':'critical','location':'HTML/metadata contract',
                                                 'source_quote':'','translation_quote':'','suggested_fix':str(exc)}]
                        task['stage'],task['status'] = 'correct','queued'
                        task.pop('batch',None)
                        self.state.save_task(task)
                        return
                    raise
                decision(self.state, task, stage, 'structural_pass')
                task['stage'] = 'review1' if stage == 'translate' else 'review2'
            else:
                task['review_model_actual'] = provenance['model']
                task['quality_score'] = result.get('score')
                task['findings'] = result.get('findings',[])
                from .review_contract import frozen_version
                passed = accepted_review(result, campaign['quality_threshold'],
                                         contract_version=frozen_version(campaign))
                if result.get('findings_complete') is False:
                    # A partial report is evidence for attention, not permission
                    # to queue another paid correction or publish a candidate.
                    task['failure_kind'] = 'incomplete_review'
                    reason = 'Review report is incomplete; all returned findings are retained for owner attention'
                    decision(self.state, task, stage, 'incomplete_review', reason, task['findings'])
                    self.finish(task, 'not_ready', reason)
                    return
                if passed:
                    decision(self.state, task, stage, 'quality_pass', findings=task['findings'])
                    self.state.save_task(task)
                    self.publish(task)
                    return
                decision(self.state, task, stage, 'quality_rejection', findings=task['findings'])
                if stage == 'review2' or task.get('downstream_recovery'):
                    task['failure_kind'] = 'quality_rejection'
                    self.finish(task,'not_ready','Final review failed; a separately funded continuation must pass admission')
                    return
                task['stage'] = 'correct'
            task['status'] = 'queued'
            task.pop('batch',None)
            self.state.save_task(task)
        except (ContractError,KeyError,TypeError,UnicodeError) as exc:
            if task.get('failure_kind') == 'structured_response':
                task['failure_kind'] = 'invalid_result'
            if self.state.read(f'state/tasks/{task["id"]}/decisions/{stage}.json') is None:
                decision(self.state, task, stage, task['failure_kind'], str(exc))
            self.finish(task,'not_ready',str(exc))

    def recover(self, batch):
        manual_admission.validate_campaign(self.state, batch['campaign'])
        batch['last_reconciled_at'] = now()
        try:
            matches = self.provider.find(batch['id'])
        except Exception as exc:
            batch.update(status='submission_unknown',reconciliation='lookup_failed_no_resubmission',
                         reconciliation_error_type=type(exc).__name__)
            self.state.save_batch(batch)
            return False
        batch.pop('reconciliation_error_type',None)
        matches = [m for m in matches if m.get('input_file_id') == batch.get('input_file_id')]
        if len(matches) == 1:
            batch.update(remote_id=matches[0]['id'],status='submitted',recovered_at=now())
            observe_batch(batch,matches[0],batch['recovered_at'])
            self.state.save_batch(batch)
            self.checkpoint('runtime: reconcile an uncertain OpenAI batch submission')
            return True
        batch['status'] = 'submission_unknown'
        batch['reconciliation'] = 'multiple_matches' if matches else 'no_match_yet_no_resubmission'
        self.state.save_batch(batch)
        return False

    def submit(self, batch, *, continue_work=None):
        manual_admission.validate_campaign(self.state, batch['campaign'])
        if not self.provider or not may_continue(continue_work):
            return
        if batch['status'] in ('submitting','submission_unknown'):
            self.recover(batch)
            return
        if batch['status'] != 'prepared':
            return
        campaign = self.state.read(f'state/campaigns/{batch["campaign"]}.json')
        from .review_contract import frozen_version
        frozen_version(campaign)
        from .scripture_evidence import frozen_policy
        frozen_policy(campaign)
        if self.exclude_protected_prepared(batch):
            return
        if self.hold_expired_prepared(batch):
            return
        campaign = self.state.read(f'state/campaigns/{batch["campaign"]}.json')
        if campaign.get('autonomous'):
            autonomous.validate_history(self.config, self.state)
            if not autonomous.enabled(self.config):
                return
        if campaign.get('downstream_recovery'):
            downstream.validate_history(self.config, self.state, {t['id']:t for t in self.state.tasks()})
            if not downstream.submission_enabled(self.config, campaign):
                return
        if campaign.get('downstream_recovery') or campaign.get('autonomous'):
            tasks = [self.state.read(f'state/tasks/{identity}/task.json') for identity in batch['tasks']]
            if any(not downstream.current(self, task) for task in tasks):
                for task in tasks:
                    self.finish(task, 'source_error', 'English source changed before downstream batch submission')
                batch['status'] = 'cancelled_before_submission'
                self.state.save_batch(batch)
                self.checkpoint('runtime: hold stale downstream work without releasing reservations')
                return
        payload = self.state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Persisted batch payload was changed; refusing submission')
        if not may_continue(continue_work):
            return
        if not batch.get('input_file_id'):
            try:
                batch['input_file_id'] = self.provider.upload(payload,batch['id']+'.jsonl')
            except Exception as exc:
                batch['upload_failures'] = batch.get('upload_failures',0)+1
                batch['error_type'] = type(exc).__name__
                if batch['upload_failures'] >= 3:
                    batch['status'] = 'upload_failed'
                    for task_id in batch['tasks']:
                        task = self.state.read(f'state/tasks/{task_id}/task.json')
                        self.finish(task,'not_ready','File upload failed three times; no Batch submission was made')
                self.state.save_batch(batch)
                self.checkpoint('runtime: record a bounded file-upload failure')
                return
            self.state.save_batch(batch)
            self.checkpoint('runtime: persist OpenAI input file before creating a batch')
        if self.exclude_protected_prepared(batch):
            return  # A checkpoint may have incorporated a concurrent human edit.
        if not may_continue(continue_work):
            return  # Prepared + uploaded is safe to resume without another upload.
        # Do not yield between intent and its result checkpoint. A durable intent
        # without a create response must take the conservative reconciliation path.
        batch['status'],batch['submission_started_at'] = 'submitting',now()
        self.state.save_batch(batch)
        self.checkpoint('runtime: record batch submission intent before the billable request')
        if self.exclude_protected_prepared(batch, before_create=True):
            return  # The intent push incorporated an edit; create has not been called.
        if self.hold_expired_prepared(batch, before_create=True):
            return
        manual_admission.validate_campaign(self.state, batch['campaign'])
        try:
            remote = self.provider.create(batch['input_file_id'],batch['id'],batch['campaign'])
            batch.update(remote_id=remote['id'],status='submitted')
            observe_batch(batch,remote,now())
        except Exception as exc:
            # Do not store exception bodies: upstream exceptions may contain sensitive headers.
            batch.update(status='submission_unknown',error_type=type(exc).__name__)
        self.state.save_batch(batch)
        self.checkpoint('runtime: persist OpenAI batch identity or uncertain-submission state')

    def hold_expired_prepared(self, batch, *, before_create=False):
        """A frozen unsubmitted payload cannot outlive its external evidence."""
        campaign = self.state.read(f'state/campaigns/{batch["campaign"]}.json')
        from .scripture_evidence import load_evidence, frozen_policy
        scripture_policy = frozen_policy(campaign)
        if scripture_policy is None:
            return False
        if scripture_policy.get('version') == '2':
            from .scripture_component_evidence import reject_component_runtime
            reject_component_runtime(scripture_policy)
        tasks = [self.state.read(f'state/tasks/{identity}/task.json') for identity in batch['tasks']]
        try:
            for task in tasks:
                load_evidence(self.state, task, require_fresh=True)
            return False
        except ScriptureAttention as exc:
            if exc.reason != 'evidence_expired':
                raise
            fresh_intent = before_create and batch['status'] == 'submitting'
            if batch.get('remote_id') or (batch.get('submission_started_at') and not fresh_intent):
                raise ContractError('An uncertain Scripture submission must reconcile, never expire locally')
            for task in tasks:
                task['failure_kind'] = 'scripture_evidence_expired'
                self.finish(task, 'not_ready', str(exc))
            batch.update(status='cancelled_before_submission', exclusion_reason='scripture_evidence_expired')
            if fresh_intent:
                batch['create_not_called'] = True
            self.state.save_batch(batch)
            self.checkpoint('runtime: hold expired frozen evidence before provider creation')
            return True

    def exclude_protected_prepared(self, batch, *, before_create=False):
        """Partition an unsubmitted payload without another attempt or reservation."""
        tasks = [self.state.read(f'state/tasks/{identity}/task.json') for identity in batch['tasks']]
        protected = [task for task in tasks if self.human_protected(task['language'], task['article_id'])]
        if not protected:
            return False
        # A disjoint checkpoint rebase can incorporate an editor's content after
        # this tick's initial sync. Recognize/isolate it before deriving reports.
        self.state.sync_human_reviews(self.gitstore)
        fresh_intent = before_create and batch['status'] == 'submitting'
        if (batch.get('remote_id') or (not fresh_intent and
                (batch['status'] != 'prepared' or batch.get('submission_started_at')))):
            raise ContractError('Only a proven never-submitted batch can exclude human-controlled work')
        ids = {task['id'] for task in protected}
        remaining = [task for task in tasks if task['id'] not in ids]
        payload = self.state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Persisted batch payload was changed; refusing partition')
        if remaining:
            child_id = uuid4().hex
            custom_ids = [task['id'] + ':' + batch['stage'] for task in remaining]
            lines = [line for line in payload.splitlines(keepends=True)
                     if loads(line)['custom_id'] in custom_ids]
            if len(lines) != len(custom_ids):
                raise ContractError('Prepared partition does not match its exact request identities')
            child_payload = b''.join(lines)
            child = {key:copy.deepcopy(batch[key]) for key in
                     ('campaign','stage','model','reserved_usd')}
            child.update(id=child_id, status='prepared', created_at=now(),
                         tasks=[task['id'] for task in remaining], custom_ids=custom_ids,
                         payload_sha256=digest(child_payload), input_file_id=None, remote_id=None,
                         reservation_reused_from=batch['id'])
            write_text(self.state.path(f'state/batches/{child_id}/input.jsonl'), child_payload.decode('utf-8'))
            self.state.save_batch(child)
            batch['replacement_batch'] = child_id
            for task in remaining:
                task['batch'] = child_id
                self.state.save_task(task)
        for task in protected:
            self.finish(task, 'cancelled', 'Human editorial authority permanently excludes further AI work')
        batch.update(status='cancelled_before_submission',
                     exclusion_reason='human_editorial_authority', excluded_task_ids=sorted(ids))
        if fresh_intent:
            batch['create_not_called'] = True
        self.state.save_batch(batch)
        self.checkpoint('runtime: exclude human-controlled work before submission without new reservations')
        return True

    def collect(self, *, continue_work=None):
        if not self.provider:
            return
        downstream.validate_history(self.config, self.state, {t['id']:t for t in self.state.tasks()})
        batches = self.state.batches()
        if continue_work is not None:
            # A bounded pass must not repeatedly spend its whole window on the
            # same slow prefix. A worker visit is separate from provider polling
            # and also covers paused prepared work and upload failures.
            batches.sort(key=lambda batch: (max(batch.get(key) or '' for key in
                ('created_at', 'last_collector_visit_at', 'last_polled_at',
                 'last_reconciled_at', 'remote_observed_at')), batch['id']))
        for batch in batches:
            if not may_continue(continue_work):
                break
            if batch['status'] not in ('prepared','submitting','submission_unknown','submitted','cancelling'):
                continue
            if continue_work is not None:
                batch['last_collector_visit_at'] = now()
                self.state.save_batch(batch)
            if batch['status'] in ('prepared','submitting','submission_unknown'):
                self.submit(batch, continue_work=continue_work)
                continue
            if batch['status'] not in ('submitted','cancelling'):
                continue
            batch['last_polled_at'] = now()
            try:
                remote = self.provider.retrieve(batch['remote_id'])
            except Exception as exc:
                # A failed read does not prove a provider failure or authorize a resubmission.
                batch['poll_error_type'] = type(exc).__name__
                self.state.save_batch(batch)
                continue
            batch.pop('poll_error_type',None)
            observe_batch(batch,remote,batch['last_polled_at'])
            if batch['remote_status'] not in BATCH_TERMINAL:
                if batch.get('cancel_requested') and batch['remote_status'] != 'cancelling':
                    self.provider.cancel(batch['remote_id'])
                    batch['status'] = 'cancelling'
                self.state.save_batch(batch)
                continue
            rows = {}
            files = []
            try:
                for field in ('output_file_id','error_file_id'):
                    if remote.get(field):
                        files.append(self.provider.content(remote[field]))
            except Exception as exc:
                batch['collection_error_type'] = type(exc).__name__
                self.state.save_batch(batch)
                continue
            batch.pop('collection_error_type',None)
            try:
                for data in files:
                    if len(data) > 100000000:
                        raise ContractError('Batch result file exceeded the 100 MB collection safety limit')
                    for line in data.splitlines():
                        if not line.strip():
                            continue
                        try:
                            row = loads(line)
                        except ContractError as exc:
                            raise ContractError('Batch result file contains invalid JSON') from exc
                        if not isinstance(row,dict) or not isinstance(row.get('custom_id'),str):
                            raise ContractError('Batch result row is missing a valid custom ID')
                        key = row.get('custom_id')
                        if key in rows or key not in batch['custom_ids']:
                            raise ContractError('Batch results contain duplicate or unexpected custom IDs')
                        rows[key] = row
                for task_id in batch['tasks']:
                    task = self.state.read(f'state/tasks/{task_id}/task.json')
                    if task['status'] in TERMINAL:
                        continue
                    if task.get('batch') != batch['id']:
                        raise ContractError('Batch/task ownership mismatch')
                    key = task['id'] + ':' + task['stage']
                    if batch.get('cancel_requested'):
                        if key in rows and task.get('autonomous'):
                            archive(self.state, task, rows[key], self.config.runtime['max_result_bytes'])
                        self.finish(task,'cancelled','Cancelled by explicit owner request')
                    elif key not in rows:
                        reason = f'Missing batch result; remote batch ended as {batch["remote_status"]}'
                        if batch.get('remote_errors'):
                            reason += '. ' + failure_reason(batch['remote_errors'][0])
                        else:
                            reason += '. Inspect the provider batch before requesting any retry; no automatic replacement was submitted.'
                        self.finish(task,'not_ready',reason)
                    else:
                        diagnostic = request_failure(rows[key])
                        if diagnostic:
                            observed = archive(self.state, task, rows[key], self.config.runtime['max_result_bytes'])
                            task['failure_kind'] = observed['outcome']
                            task['provider_failure'] = diagnostic
                            self.finish(task,'not_ready',failure_reason(diagnostic))
                        else:
                            self.receive(task,rows[key])
                batch['status'] = 'collected'
                batch['collected_at'] = now()
                # Compatibility: completed_at has always meant LOCAL collection.
                # Provider completion is exclusively remote_completed_at (Unix seconds).
                batch['completed_at'] = batch['collected_at']
            except ContractError as exc:
                batch.update(status='results_invalid',error=str(exc))
                batch['collection_finished_at'] = now()
                for task_id in batch['tasks']:
                    task = self.state.read(f'state/tasks/{task_id}/task.json')
                    if task['status'] not in TERMINAL:
                        self.finish(task,'not_ready',str(exc))
            self.state.save_batch(batch)
            self.checkpoint('runtime: collect batch results and bounded quality decisions')

    def prepare(self, *, continue_work=None, max_batches=None):
        if not self.provider:
            return 0
        limit = self.config.runtime['max_batches_per_tick'] if max_batches is None else max_batches
        downstream.validate_history(self.config, self.state, {t['id']:t for t in self.state.tasks()})
        autonomous.validate_history(self.config, self.state)
        manual_admission.validate_history(self.state)
        groups = defaultdict(list)
        for task in self.state.tasks():
            if task['status'] == 'queued':
                if not manual_admission.admitted(self.state, task):
                    continue  # A partially materialized admission cannot become billable.
                if self.human_protected(task['language'], task['article_id']):
                    self.finish(task, 'cancelled', 'Human editorial authority permanently excludes further AI work')
                    continue
                chosen = task['review_model'] if task['stage'].startswith('review') else task['model']
                groups[(task['campaign'],task['stage'],chosen)].append(task)
        prepared_count = 0
        for (campaign_id,stage,model), tasks in sorted(groups.items()):
            while (tasks and prepared_count < limit
                   and may_continue(continue_work)):
                campaign = self.state.read(f'state/campaigns/{campaign_id}.json')
                if campaign.get('autonomous'):
                    if not campaign.get('automatic_acceptance_complete'):
                        raise ContractError('Automatic admission is incomplete; paid work is blocked')
                    if not autonomous.enabled(self.config):
                        break
                if campaign.get('downstream_recovery'):
                    if not campaign.get('downstream_acceptance_complete'):
                        raise ContractError('Downstream acceptance incomplete; allocation retained and paid work blocked')
                    if not downstream.submission_enabled(self.config, campaign):
                        break
                if campaign.get('recovery_of_campaign') and not campaign.get('recovery_acceptance_complete'):
                    raise ContractError('Recovery acceptance is incomplete; its allocation is retained and paid work is blocked')
                if campaign.get('cancel_requested'):
                    for task in tasks:
                        self.finish(task,'cancelled','Campaign cancelled by owner')
                    break
                selected, lines, costs, size = [],[],[],0
                while tasks and len(selected) < self.config.runtime['max_batch_requests']:
                    task = tasks[0]
                    if task.get('autonomous') and not downstream.current(self, task):
                        self.finish(task, 'source_error', 'English source changed before automatic submission')
                        tasks.pop(0); continue
                    if task.get('downstream_recovery'):
                        if not downstream.current(self, task):
                            self.finish(task, 'source_error', 'English source changed before downstream submission')
                            tasks.pop(0); continue
                        if ((task['translation_attempts'] >= 1 and stage in ('translate', 'correct')) or
                                (task['review_attempts'] >= 1 and stage in ('review1', 'review2'))):
                            self.finish(task, 'not_ready', 'Downstream repair/review attempt limit reached')
                            tasks.pop(0); continue
                    if ((stage in ('translate','correct') and task['translation_attempts'] >= 2) or
                            (stage.startswith('review') and task['review_attempts'] >= 2)):
                        self.finish(task,'not_ready','Hard attempt limit reached')
                        tasks.pop(0)
                        continue
                    try:
                        stage_budget.enforce(task, self.state.candidate(task))
                        line,cost,input_bound = build_request(self.config,self.state,task)
                        if 'stage_budget' in task and downstream.money(cost) > downstream.money(task['stage_budget']['stages_usd'][stage]):
                            raise ContractError('Request exceeds its frozen complete-stage reservation')
                        if 'cycle_budget' in task:
                            ceiling = task['cycle_budget']['review_reserved_usd' if stage.startswith('review') else 'repair_reserved_usd']
                            if downstream.money(cost) > downstream.money(ceiling):
                                raise ContractError('Request exceeds its frozen complete-cycle reservation')
                    except ContractError as exc:
                        if isinstance(exc, ScriptureAttention) and exc.reason == 'evidence_expired':
                            task['failure_kind'] = 'scripture_evidence_expired'
                        self.finish(task,'not_ready',str(exc)); tasks.pop(0); continue
                    raw = canonical(line)+b'\n'
                    if len(raw) > self.config.runtime['max_batch_bytes']:
                        self.finish(task,'not_ready','Single request exceeds batch byte limit'); tasks.pop(0); continue
                    if size+len(raw) > self.config.runtime['max_batch_bytes']:
                        break
                    if campaign['reserved_usd']+sum(costs)+cost > campaign['budget_usd']+1e-9:
                        self.finish(task,'budget_blocked','Campaign spending reservation exhausted; no API request submitted')
                        tasks.pop(0); continue
                    selected.append(task); lines.append(raw); costs.append(cost); size += len(raw); tasks.pop(0)
                if not selected:
                    break
                batch_id = uuid4().hex
                payload = b''.join(lines)
                batch = {'id':batch_id,'campaign':campaign_id,'stage':stage,'model':model,
                         'status':'prepared','created_at':now(),'tasks':[t['id'] for t in selected],
                         'custom_ids':[t['id']+':'+stage for t in selected],
                         'payload_sha256':digest(payload),'reserved_usd':round(sum(costs),6),
                         'input_file_id':None,'remote_id':None}
                write_text(self.state.path(f'state/batches/{batch_id}/input.jsonl'),payload.decode('utf-8'))
                self.state.save_batch(batch)
                for task in selected:
                    task['status'],task['batch'] = 'in_batch',batch_id
                    task['review_attempts' if stage.startswith('review') else 'translation_attempts'] += 1
                    self.state.save_task(task)
                campaign['reserved_usd'] = round(campaign['reserved_usd']+sum(costs),6)
                self.state.save_campaign(campaign)
                self.checkpoint('runtime: reserve task identities and budget before OpenAI submission')
                self.submit(batch, continue_work=continue_work)
                prepared_count += 1
        return prepared_count

    def cancel_campaign(self, identity):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identity):
            raise ContractError('Invalid campaign ID')
        campaign = self.state.read(f'state/campaigns/{identity}.json')
        if not campaign:
            raise ContractError('Unknown campaign')
        if campaign.get('downstream_recovery') and not campaign.get('downstream_acceptance_complete'):
            return downstream.abort_incomplete(self, campaign)
        if is_recovery_campaign(self.state, campaign):
            recorded_request(self.state, campaign)
            if (campaign.get('status') in ('acceptance_incomplete', 'acceptance_aborting', 'acceptance_aborted')
                    or (not campaign.get('dry_run') and not campaign.get('recovery_acceptance_complete'))):
                from .recovery_abort import abort_incomplete_recovery
                return abort_incomplete_recovery(self, campaign)
        campaign['cancel_requested'] = True
        self.state.save_campaign(campaign)
        if self.state.read(manual_admission.path(identity)) or manual_admission.needs_resume(self.state, campaign):
            request = self.state.read(f'state/queue/{identity}.json')
            if request is not None:
                manual_admission.resume(self, campaign, request)
        self.checkpoint('runtime: persist explicit campaign cancellation')
        for batch in self.state.batches():
            if batch['campaign'] != identity or batch['status'] in ('collected','results_invalid','upload_failed'):
                continue
            batch['cancel_requested'] = True
            if batch['status'] in ('submitting','submission_unknown') and self.provider:
                self.recover(batch)
            if batch.get('remote_id') and self.provider:
                self.provider.cancel(batch['remote_id'])
                batch['status'] = 'cancelling'
            elif batch['status'] == 'prepared':
                batch['status'] = 'cancelled_before_submission'
                for task_id in batch['tasks']:
                    self.finish(self.state.read(f'state/tasks/{task_id}/task.json'),'cancelled')
            self.state.save_batch(batch)
        for task in self.state.tasks():
            if task['campaign'] == identity and task['status'] == 'queued':
                self.finish(task,'cancelled')
        self.state.derive(self.config)
        self.checkpoint('runtime: record campaign cancellation results')

    def tick(self, *, discover_source=True, continue_work=None):
        self.continue_work = continue_work
        manual_admission.restore_orphans(self.state)
        self.state.sync_human_reviews(self.gitstore)
        autonomous.settle(self)
        if discover_source and may_continue(continue_work):
            self.discover()
            if autonomous.owns_automatic_work(self.config, self.state):
                if may_continue(continue_work):
                    autonomous.enqueue(self)
            else:
                if may_continue(continue_work):
                    enqueue_source_refreshes(self)
                if may_continue(continue_work):
                    downstream.enqueue_hour(self)
        for campaign in self.state.campaigns():
            if campaign.get('autonomous') and not campaign.get('automatic_acceptance_complete') and not campaign.get('cancel_requested'):
                autonomous.stage_accepted(self, campaign)
        prepared = 0
        automatic = autonomous.owns_automatic_work(self.config, self.state)
        if automatic and may_continue(continue_work):
            # Complete funded stages before potentially slow read-only Scripture
            # prefetch. A provider outage cannot starve already accepted work.
            self.collect(continue_work=continue_work)
            autonomous.settle(self)
            if may_continue(continue_work):
                prepared = self.prepare(continue_work=continue_work)
        if may_continue(continue_work):
            self.accept_queue(continue_work=continue_work)
        if not automatic and may_continue(continue_work):
            self.collect(continue_work=continue_work)
        if may_continue(continue_work):
            self.prepare(continue_work=continue_work,
                         max_batches=max(0, self.config.runtime['max_batches_per_tick'] - prepared))
        autonomous.settle(self)
        for campaign in self.state.campaigns():
            if campaign.get('downstream_recovery') and not campaign.get('downstream_acceptance_complete'):
                continue
            if campaign['dry_run'] or (campaign.get('recovery_of_campaign')
                                       and not campaign.get('recovery_acceptance_complete')):
                continue
            tasks = [self.state.read(f'state/tasks/{identity}/task.json') for identity in campaign['tasks']]
            admissions = manual_admission.counts(self.state, campaign)
            if manual_admission.needs_resume(self.state, campaign):
                campaign['admission_counts'] = admissions
            campaign['status'] = (
                'admission_attention' if not campaign.get('cancel_requested') and admissions.get('attention') else
                'admission_pending' if not campaign.get('cancel_requested') and any(admissions.get(s) for s in ('pending', 'ready')) else
                'finished' if all(t['status'] in TERMINAL for t in tasks) else 'active')
            campaign['task_counts'] = dict(sorted({s:sum(t['status']==s for t in tasks) for s in {t['status'] for t in tasks}}.items()))
            self.state.save_campaign(campaign)
        source = self.state.read('state/source.json', {'revision':None,'articles':{},'issues':[]})
        heartbeat = self.state.read('state/heartbeat.json',{})
        snapshot = {'utc_date':now()[:10],
            'source_revision':source['revision'],'articles_discovered':len(source['articles']),
            'issues_discovered':len(source['issues']),
            'pending_tasks':sum(t['status'] not in TERMINAL for t in self.state.tasks()),
            'unfinished_manual_admissions':sum(sum(n for status,n in manual_admission.counts(self.state,c).items()
                if status in manual_admission.UNFINISHED) for c in self.state.campaigns() if not c.get('cancel_requested'))}
        if heartbeat != snapshot:
            self.state.write('state/heartbeat.json',snapshot)
        result = self.state.derive(self.config)
        self.checkpoint('runtime: update source discovery and translation publication index')
        return result
