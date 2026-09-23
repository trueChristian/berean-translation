"""Resumable, cost-bounded translation state machine.

Manual requests are immutable queue entries. Only the serialized worker mutates
campaign/task/batch records. Checkpoints precede every potentially billable
Batch creation. Uncertain submissions are reconciled, never blindly retried.
"""
from __future__ import annotations
import copy
import re
from collections import defaultdict
from pathlib import Path
from uuid import uuid4
from .common import (ContractError, canonical, csv_values, digest, json_hash, loads, now,
                     positive_money, read_json, write_text)
from .html import notice, validate_translation
from .requests import accepted_review, build_request, parse_response
from .state import State, TERMINAL

BATCH_TERMINAL = {'completed','failed','expired','cancelled'}


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

    def select_issues(self, selection, languages, operation, retry_failed=False):
        source = self.state.read('state/source.json')
        issues = source['issues']
        if selection in ('all','outstanding'):
            return [i['id'] for i in issues]
        if selection == 'next':
            for issue in issues:
                for article in source['articles'].values():
                    if article['issue_id'] == issue['id'] and any(
                        self.eligible(article,lang,operation,retry_failed)[0] for lang in languages):
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

    def eligible(self, article, language, operation, retry_failed):
        record = self.state.record(language,article['id'])
        latest_id = record.get('latest_task')
        task = self.state.read(f'state/tasks/{latest_id}/task.json') if latest_id else None
        if task and task['status'] not in TERMINAL:
            return False,'already_processing'
        pub = record.get('published')
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
        identity = request.get('id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity):
            raise ContractError('Invalid queue request identity')
        existing = self.state.read(f'state/campaigns/{identity}.json')
        if existing:
            return existing
        operation = request.get('operation')
        if operation not in ('translate','review'):
            raise ContractError('Request operation must be translate or review')
        languages = self.config.select_languages(request.get('languages','all'))
        model = request.get('model') or self.config.runtime['default_model']
        review_model = request.get('review_model') or self.config.runtime['default_review_model']
        self.config.model(model); self.config.model(review_model)
        budget = positive_money(request.get('budget_usd',5),self.config.runtime['max_campaign_usd'])
        retry = request.get('retry_failed',False)
        dry_run = request.get('dry_run',False)
        if type(retry) is not bool or type(dry_run) is not bool:
            raise ContractError('Request dry_run and retry_failed must be booleans')
        selected = self.select_issues(request.get('issues','next'),languages,operation,retry)
        source_index = self.state.read('state/source.json')
        articles = sorted((a for a in source_index['articles'].values() if a['issue_id'] in selected),
                          key=lambda a:(selected.index(a['issue_id']),a['sequence'],a['id']))
        planned, skipped = [],[]
        for article in articles:
            for language in languages:
                allowed, reason = self.eligible(article,language,operation,retry)
                (planned if allowed else skipped).append({'article_id':article['id'],'language':language,'reason':reason})
        if len(planned) > self.config.runtime['max_tasks_per_request']:
            raise ContractError('Selection exceeds max_tasks_per_request; choose fewer issues/languages')
        campaign = {'id':identity,'operation':operation,'created_at':now(),'requested_by':request.get('requested_by'),
                    'source_revision':source_index['revision'],'budget_usd':budget,'reserved_usd':0.0,
                    'reported_usage_usd':0.0,'tasks':[],'skipped':skipped,'selection':planned,'dry_run':dry_run,
                    'status':'planned' if dry_run else 'active','languages':languages,'issues':selected,
                    'model':model,'review_model':review_model,'models':copy.deepcopy(self.config.models),
                    'prompt_version':self.config.runtime['prompt_version'],
                    'prompts':{name:self.config.prompt(name) for name in ('translation','review')},
                    'language_settings':{lang:copy.deepcopy(self.config.languages[lang]) for lang in languages},
                    'glossaries':read_json(self.config.root/'config/glossaries.json')['languages'],
                    'max_output_tokens':self.config.runtime['max_output_tokens'],
                    'review_output_tokens':self.config.runtime['review_output_tokens'],
                    'quality_threshold':self.config.runtime['quality_threshold']}
        self.state.save_campaign(campaign)
        if dry_run:
            # Selection preview is free; no tasks are reserved and no API objects are created.
            return campaign
        for item in planned:
            article_id, language = item['article_id'],item['language']
            article = source_index['articles'][article_id]
            record = self.state.record(language,article_id)
            previous = self.state.read(f'state/tasks/{record.get("latest_task")}/task.json') if record.get('latest_task') else None
            try:
                source = self.source_client.snapshot(article_id)
            except (ContractError,UnicodeError) as exc:
                campaign['skipped'].append({**item,'reason':'source_error','detail':str(exc)})
                continue
            snapshot_path = f'state/sources/{json_hash(source)}.json'
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
                candidate = self.state.publication_candidate(pub)[0] if pub else self.state.candidate(previous)
                self.state.save_candidate(task,candidate)
                if not task['translation_model_actual'] and previous:
                    task['translation_model_actual'] = previous.get('translation_model_actual')
            self.state.save_task(task)
            record['latest_task'] = task_id
            record['history'].append({'event':'requested','task':task_id,'campaign':identity,'at':task['created_at']})
            self.state.save_record(record)
            campaign['tasks'].append(task_id)
        self.state.save_campaign(campaign)
        return campaign

    def accept_queue(self):
        count = 0
        for path in sorted((self.config.root/'state/queue').glob('*.json')):
            request = read_json(path)
            if (self.state.read(f'state/campaigns/{path.stem}.json') is not None or
                    self.state.read(f'state/queue-errors/{path.stem}.json') is not None):
                continue
            if count >= self.config.runtime['max_pending_campaigns_per_tick']:
                break
            if request.get('id') != path.stem:
                raise ContractError('Queue filename and identity mismatch')
            try:
                self.accept_request(request)
            except ContractError as exc:
                self.state.write(f'state/queue-errors/{path.stem}.json',{'error':str(exc),'request':path.stem})
                # Invalid queue entries are durable diagnostics, not a reason to stop other requests.
                continue
            count += 1
        self.checkpoint('runtime: accept pending translation requests')

    def finish(self, task, status, reason=None):
        task['status'],task['finished_at'] = status,now()
        task.pop('batch',None)
        if reason:
            task['failure'] = reason
        self.state.save_task(task)
        record = self.state.record(task['language'],task['article_id'])
        record['history'].append({'event':status,'task':task['id'],'at':task['finished_at'],'reason':reason})
        self.state.save_record(record)

    def publish(self, task):
        source, candidate = self.state.source(task),self.state.candidate(task)
        validate_translation(source,candidate)
        record = self.state.record(task['language'],task['article_id'])
        pub = record.get('published')
        if task['protected'] or (pub and pub['human_reviewed']):
            return self.finish(task,'proposal','AI suggestions never overwrite human-reviewed work')
        if pub:
            if (digest(self.state.path(pub['html_path']).read_bytes()) != task['base_html_sha256'] or
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
        stage = task['stage']
        try:
            result, provenance = parse_response(row,self.config.runtime['max_result_bytes'])
            self.state.write(f'state/tasks/{task["id"]}/results/{stage}.json',
                             {'result':result,'provenance':provenance,'received_at':now()})
            task['events'].append({'stage':stage,'model':provenance['model'],'usage':provenance['usage']})
            campaign = self.state.read(f'state/campaigns/{task["campaign"]}.json')
            pricing = task['models'][task['review_model'] if stage.startswith('review') else task['model']]
            usage = provenance.get('usage') or {}
            if isinstance(usage.get('prompt_tokens'),int) and isinstance(usage.get('completion_tokens'),int):
                campaign['reported_usage_usd'] = round(campaign['reported_usage_usd'] +
                    (max(0,usage['prompt_tokens'])*pricing['input_batch_usd_per_million'] +
                     max(0,usage['completion_tokens'])*pricing['output_batch_usd_per_million'])/1000000,8)
                self.state.save_campaign(campaign)
            if stage in ('translate','correct'):
                task['translation_model_actual'] = provenance['model']
                self.state.save_candidate(task,result)
                try:
                    validate_translation(self.state.source(task),result)
                except ContractError as exc:
                    if stage == 'translate':
                        task['findings'] = [{'severity':'critical','location':'HTML/metadata contract',
                                             'source_quote':'','translation_quote':'','suggested_fix':str(exc)}]
                        task['stage'],task['status'] = 'correct','queued'
                        task.pop('batch',None)
                        self.state.save_task(task)
                        return
                    raise
                task['stage'] = 'review1' if stage == 'translate' else 'review2'
            else:
                task['review_model_actual'] = provenance['model']
                task['quality_score'] = result.get('score')
                task['findings'] = result.get('findings',[])
                if accepted_review(result,campaign['quality_threshold']):
                    self.state.save_task(task)
                    self.publish(task)
                    return
                if stage == 'review2':
                    self.finish(task,'not_ready','Final review failed; no further automatic correction is allowed')
                    return
                task['stage'] = 'correct'
            task['status'] = 'queued'
            task.pop('batch',None)
            self.state.save_task(task)
        except (ContractError,KeyError,TypeError,UnicodeError) as exc:
            self.finish(task,'not_ready',str(exc))

    def recover(self, batch):
        matches = self.provider.find(batch['id'])
        matches = [m for m in matches if m.get('input_file_id') == batch.get('input_file_id')]
        if len(matches) == 1:
            batch.update(remote_id=matches[0]['id'],status='submitted',recovered_at=now())
            self.state.save_batch(batch)
            self.checkpoint('runtime: reconcile an uncertain OpenAI batch submission')
            return True
        batch['status'] = 'submission_unknown'
        batch['reconciliation'] = 'multiple_matches' if matches else 'no_match_yet_no_resubmission'
        self.state.save_batch(batch)
        return False

    def submit(self, batch):
        if not self.provider:
            return
        if batch['status'] in ('submitting','submission_unknown'):
            self.recover(batch)
            return
        if batch['status'] != 'prepared':
            return
        payload = self.state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Persisted batch payload was changed; refusing submission')
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
        batch['status'],batch['submission_started_at'] = 'submitting',now()
        self.state.save_batch(batch)
        self.checkpoint('runtime: record batch submission intent before the billable request')
        try:
            remote = self.provider.create(batch['input_file_id'],batch['id'],batch['campaign'])
            batch.update(remote_id=remote['id'],status='submitted')
        except Exception as exc:
            # Do not store exception bodies: upstream exceptions may contain sensitive headers.
            batch.update(status='submission_unknown',error_type=type(exc).__name__)
        self.state.save_batch(batch)
        self.checkpoint('runtime: persist OpenAI batch identity or uncertain-submission state')

    def collect(self):
        if not self.provider:
            return
        for batch in self.state.batches():
            if batch['status'] in ('prepared','submitting','submission_unknown'):
                self.submit(batch)
                continue
            if batch['status'] not in ('submitted','cancelling'):
                continue
            remote = self.provider.retrieve(batch['remote_id'])
            batch['remote_status'] = remote['status']
            if remote['status'] not in BATCH_TERMINAL:
                if batch.get('cancel_requested') and remote['status'] != 'cancelling':
                    self.provider.cancel(batch['remote_id'])
                    batch['status'] = 'cancelling'
                self.state.save_batch(batch)
                continue
            rows = {}
            try:
                for field in ('output_file_id','error_file_id'):
                    if not remote.get(field):
                        continue
                    data = self.provider.content(remote[field])
                    if len(data) > 100000000:
                        raise ContractError('Batch result file exceeded the 100 MB collection safety limit')
                    for line in data.splitlines():
                        if not line.strip():
                            continue
                        row = loads(line)
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
                        self.finish(task,'cancelled','Cancelled by explicit owner request')
                    elif key not in rows:
                        self.finish(task,'not_ready',f'Missing batch result; remote batch ended as {remote["status"]}')
                    else:
                        self.receive(task,rows[key])
                batch['status'] = 'collected'
                batch['completed_at'] = now()
            except ContractError as exc:
                batch.update(status='results_invalid',error=str(exc))
                for task_id in batch['tasks']:
                    task = self.state.read(f'state/tasks/{task_id}/task.json')
                    if task['status'] not in TERMINAL:
                        self.finish(task,'not_ready',str(exc))
            self.state.save_batch(batch)
            self.checkpoint('runtime: collect batch results and bounded quality decisions')

    def prepare(self):
        if not self.provider:
            return
        groups = defaultdict(list)
        for task in self.state.tasks():
            if task['status'] == 'queued':
                chosen = task['review_model'] if task['stage'].startswith('review') else task['model']
                groups[(task['campaign'],task['stage'],chosen)].append(task)
        prepared_count = 0
        for (campaign_id,stage,model), tasks in sorted(groups.items()):
            while tasks and prepared_count < self.config.runtime['max_batches_per_tick']:
                campaign = self.state.read(f'state/campaigns/{campaign_id}.json')
                if campaign.get('cancel_requested'):
                    for task in tasks:
                        self.finish(task,'cancelled','Campaign cancelled by owner')
                    break
                selected, lines, costs, size = [],[],[],0
                while tasks and len(selected) < self.config.runtime['max_batch_requests']:
                    task = tasks[0]
                    if ((stage in ('translate','correct') and task['translation_attempts'] >= 2) or
                            (stage.startswith('review') and task['review_attempts'] >= 2)):
                        self.finish(task,'not_ready','Hard attempt limit reached')
                        tasks.pop(0)
                        continue
                    try:
                        line,cost,input_bound = build_request(self.config,self.state,task)
                    except ContractError as exc:
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
                self.submit(batch)
                prepared_count += 1

    def cancel_campaign(self, identity):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identity):
            raise ContractError('Invalid campaign ID')
        campaign = self.state.read(f'state/campaigns/{identity}.json')
        if not campaign:
            raise ContractError('Unknown campaign')
        campaign['cancel_requested'] = True
        self.state.save_campaign(campaign)
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

    def tick(self):
        self.state.sync_human_reviews(self.gitstore)
        self.discover()
        self.accept_queue()
        self.collect()
        self.prepare()
        for campaign in self.state.campaigns():
            if campaign['dry_run']:
                continue
            tasks = [self.state.read(f'state/tasks/{identity}/task.json') for identity in campaign['tasks']]
            campaign['status'] = 'finished' if all(t['status'] in TERMINAL for t in tasks) else 'active'
            campaign['task_counts'] = dict(sorted({s:sum(t['status']==s for t in tasks) for s in {t['status'] for t in tasks}}.items()))
            self.state.save_campaign(campaign)
        source = self.state.read('state/source.json')
        heartbeat = self.state.read('state/heartbeat.json',{})
        if heartbeat.get('utc_date') != now()[:10]:
            self.state.write('state/heartbeat.json',{'utc_date':now()[:10],
                'source_revision':source['revision'],'articles_discovered':len(source['articles']),
                'issues_discovered':len(source['issues']),
                'pending_tasks':sum(t['status'] not in TERMINAL for t in self.state.tasks())})
        result = self.state.derive(self.config)
        self.checkpoint('runtime: update source discovery and translation publication index')
        return result
