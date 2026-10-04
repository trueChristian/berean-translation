"""Persistent work records, review detection, and deterministic website metadata."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
from pathlib import Path
from .batch_telemetry import provider_error
from .common import ContractError, digest, json_hash, now, read_json, safe_path, write_json, write_text
from .html import split_article, validate_translation, human_notice
from .review_notice import validate_human_content, validate_recorded_notice
from . import publication_edits

TERMINAL = {'complete','not_ready','proposal','cancelled','budget_blocked','source_error'}


def provider_time(value):
    if type(value) is not int or value < 0:
        return 'not recorded'
    try:
        return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec='seconds')
    except (OverflowError,OSError,ValueError):
        return 'not recorded'


class State:
    def __init__(self, root: Path):
        self.root = root

    def path(self, path: str) -> Path:
        return safe_path(self.root, path)

    def read(self, path: str, default=None):
        return read_json(self.path(path), default)

    def write(self, path: str, value) -> None:
        write_json(self.path(path), value)

    def tasks(self):
        return [read_json(p) for p in sorted((self.root/'state/tasks').glob('*/task.json'))]

    def campaigns(self):
        return [read_json(p) for p in sorted((self.root/'state/campaigns').glob('*.json'))]

    def batches(self):
        return [read_json(p) for p in sorted((self.root/'state/batches').glob('*/batch.json'))]

    def records(self):
        return [read_json(p) for p in sorted((self.root/'state/records').glob('*/*.json'))]

    def record(self, language, article_id):
        return self.read(f'state/records/{language}/{article_id}.json',
                         {'language':language,'article_id':article_id,'published':None,'history':[]})

    def save_record(self, record):
        self.write(f'state/records/{record["language"]}/{record["article_id"]}.json',record)

    def save_task(self, task):
        self.write(f'state/tasks/{task["id"]}/task.json',task)

    def save_campaign(self, campaign):
        self.write(f'state/campaigns/{campaign["id"]}.json',campaign)

    def save_batch(self, batch):
        self.write(f'state/batches/{batch["id"]}/batch.json',batch)

    def source(self, task_or_publication):
        source = self.read(task_or_publication['source_snapshot'])
        expected = Path(task_or_publication['source_snapshot']).stem
        if not source or json_hash(source) != expected:
            raise ContractError('Pinned source snapshot is missing or changed')
        return source

    def candidate(self, task):
        return self.read(f'state/tasks/{task["id"]}/candidate.json')

    def save_candidate(self, task, candidate):
        self.write(f'state/tasks/{task["id"]}/candidate.json',candidate)

    def publication_candidate(self, publication, *, working=False):
        if not working and publication.get('edit_issue'):
            if publication_edits.observed_files(self, publication) != publication['edit_issue']['observed_files']:
                raise ContractError('Publication working files changed after edit isolation; run review synchronization')
            return publication_edits.candidate(publication_edits.accepted_payload(self, publication))
        text = self.path(publication['html_path']).read_text(encoding='utf-8')
        body, tail = split_article(text)
        metadata = self.read(publication['metadata_path'])
        if not isinstance(metadata,dict) or set(metadata) != {'title','subtitle','section'}:
            raise ContractError('Translation sidecar must contain title, subtitle, and section only')
        if not working:
            publication_edits.verify({'html':text, 'metadata':metadata}, publication)
        return {'html':body,**metadata}, tail, text

    def sync_human_reviews(self, gitstore):
        from .config import Config
        config = Config(self.root)
        for record in self.records():
            pub = record.get('published')
            if not pub:
                continue
            # Source/provenance corruption is not an editorial problem.
            source = self.source(pub)
            unchanged = False
            try:
                candidate, tail, text = self.publication_candidate(pub, working=True)
                new_html_hash = digest(text)
                metadata = {k:candidate[k] for k in ('title','subtitle','section')}
                metadata_hash = json_hash(metadata)
                if new_html_hash == pub['html_sha256'] and metadata_hash == pub['metadata_sha256']:
                    unchanged = True
                    evidence = pub.get('human_review')
                    if not evidence:
                        evidence = next((event.get('evidence') for event in reversed(record['history'])
                            if event.get('event') == 'human_edit_unreviewed'
                            and event.get('html_sha256') == pub['html_sha256']
                            and event.get('metadata_sha256') == pub['metadata_sha256']), None)
                    normalized = human_notice(config.languages[record['language']], record['article_id'],
                                              config.runtime['english_route'])
                    if evidence and (not pub['human_reviewed'] or tail != normalized):
                        validate_human_content(candidate, normalized, record['article_id'])
                        text = candidate['html'] + '\n\n' + normalized + '\n'
                        pub.update(html_sha256=digest(text), human_reviewed=True, human_review=evidence,
                                   human_review_notice_html=normalized)
                        pub.pop('edit_issue', None)
                        publication_edits.remember(self, pub, {'html':text, 'metadata':metadata})
                        write_text(self.path(pub['html_path']), text)
                        record['history'].append({'event':'human_notice_standardized', 'evidence':evidence,
                                                  'html_sha256':pub['html_sha256'],
                                                  'metadata_sha256':metadata_hash})
                        self.save_record(record)
                        continue
                    validate_recorded_notice(pub, tail)
                    if pub['human_reviewed']:
                        validate_human_content(candidate, tail, record['article_id'])
                    else:
                        validate_translation(source, candidate, language=record['language'])
                    if pub.pop('edit_issue', None) is not None:
                        self.save_record(record)
                    continue
                changed_path = pub['html_path'] if new_html_hash != pub['html_sha256'] else pub['metadata_path']
                evidence = gitstore.human_edit_evidence(changed_path)
                if new_html_hash != pub['html_sha256'] and metadata_hash != pub['metadata_sha256']:
                    gitstore.human_edit_evidence(pub['metadata_path'])
                # Human editorial authority does not depend on model judgments,
                # source parity, reference numbers or the wording of a notice.
                # The submitted footer is presentation, never an input protocol.
                # Replace it only after human attribution, keeping the body intact.
                tail = human_notice(config.languages[record['language']], record['article_id'],
                                    config.runtime['english_route'])
                validate_human_content(candidate, tail, record['article_id'])
                text = candidate['html'] + '\n\n' + tail + '\n'
                new_html_hash = digest(text)
                pub.update(html_sha256=new_html_hash,metadata_sha256=metadata_hash,
                           human_reviewed=True, human_review=evidence,
                           human_review_notice_html=tail)
                pub.pop('edit_issue', None)
                publication_edits.remember(self, pub, {'html': text, 'metadata': metadata})
                write_text(self.path(pub['html_path']), text)
                record['history'].append({'event':'human_review',
                                          'evidence':evidence,'html_sha256':new_html_hash,
                                          'metadata_sha256':metadata_hash})
                self.save_record(record)
            except (ContractError, OSError, UnicodeError) as exc:
                if unchanged:
                    raise  # An unchanged accepted record failing is not a new edit.
                # Keep the editor's files intact. A separately verified accepted
                # copy remains exportable while other articles keep processing.
                payload = publication_edits.recover_accepted(self, pub, gitstore, record['history'])
                publication_edits.remember(self, pub, payload)
                issue = {'reason':str(exc)[:1200],
                         'observed_files':publication_edits.observed_files(self, pub)}
                if pub.get('edit_issue') != issue:
                    pub['edit_issue'] = issue
                    record['history'].append({'event':'publication_edit_requires_attention', **issue})
                    self.save_record(record)

    def projection(self, config):
        source = self.read('state/source.json',{'revision':None,'articles':{},'issues':[]})
        entries = []
        for record in self.records():
            pub = record.get('published')
            if not pub:
                continue
            candidate, tail, text = self.publication_candidate(pub)
            validate_recorded_notice(pub, tail)
            source_snapshot = self.source(pub)
            parsed = (validate_human_content(candidate, tail, record['article_id']) if pub['human_reviewed']
                      else validate_translation(source_snapshot, candidate, language=record['language']))
            current = source['articles'].get(record['article_id'])
            status = 'ready' if current and current['translation_key'] == pub['translation_key'] else 'stale'
            if current is None:
                status = 'source_removed'
            lang = config.languages[record['language']]
            entries.append({'id':record['article_id'],'language':record['language'],'language_tag':lang['tag'],
                            'direction':lang['dir'],'issue_id':current['issue_id'] if current else pub['issue_id'],
                            'html':pub['html_path'],'metadata':pub['metadata_path'],
                            'title':candidate['title'],'subtitle':candidate['subtitle'],'section':candidate['section'],
                            'status':status,'human_reviewed':pub['human_reviewed'],
                            'ai_notice_required':not pub['human_reviewed'],'provider':'OpenAI',
                            'translation_model':pub['model'],'review_model':pub['review_model'],
                            'source_revision':pub['source_revision'],'source_translation_key':pub['translation_key'],
                            'html_sha256':digest(text),'metadata_sha256':pub['metadata_sha256'],
                            'images':[{'public_path':im['src'],'alt':im['alt']} for im in parsed.images]})
            if pub['human_reviewed']:
                entries[-1]['human_edit'] = copy.deepcopy(pub['human_review'])
                entries[-1]['notice_present'] = bool(tail)
            if pub.get('edit_issue'):
                entries[-1]['pending_edit'] = {'reason':pub['edit_issue']['reason'],
                                               'serving_last_accepted':True}
        return {'format_version':'1.0','source_repository':config.runtime['source_repository'],
                'observed_source_revision':source['revision'],
                'articles':sorted(entries,key=lambda x:(x['language'],x['id']))}

    def derive(self, config):
        index = self.projection(config)
        self.write('index.json',index)
        source = self.read('state/source.json',{'revision':None,'articles':{},'issues':[]})
        tasks = self.tasks()
        task_by_id = {t['id']:t for t in tasks}
        current_tasks = {(r['language'],r['article_id']):task_by_id[r['latest_task']]
                         for r in self.records() if r.get('latest_task') in task_by_id}
        entries = {(e['language'],e['id']):e for e in index['articles']}
        rows = ['# Translation status','',
                'Generated from the pinned source catalogue and durable work records. No API call is made by this report.',
                '', 'Ready means source-compatible and exportable here; live deployment is verified separately.',
                'Finished means processing has stopped, not that every requested translation passed. Held items still need action.',
                '',f'Observed English revision: `{source["revision"] or "not yet discovered"}`','',
                '| Issue selector | Articles | Ready / target | Active | Not ready | Stale |',
                '| --- | ---: | ---: | ---: | ---: | ---: |']
        for issue in source['issues']:
            ids = [a['id'] for a in source['articles'].values() if a['issue_id'] == issue['id']]
            counts = {'ready':0,'active':0,'not_ready':0,'stale':0}
            for language in config.languages:
                for identity in ids:
                    entry = entries.get((language,identity))
                    task = current_tasks.get((language,identity))
                    if entry:
                        counts['ready' if entry['status'] == 'ready' else 'stale'] += 1
                    if task and task['status'] not in TERMINAL:
                        counts['active'] += 1
                    elif task and task['status'] in ('not_ready','budget_blocked','source_error'):
                        counts['not_ready'] += 1
            rows.append(f'| `{issue["source_id"]}` | {len(ids)} | {counts["ready"]} / {len(ids)*len(config.languages)} | {counts["active"]} | {counts["not_ready"]} | {counts["stale"]} |')
        rows += ['', '## Language readiness', '',
                 '| Language | Ready / source articles | Human reviewed | Active candidates | Not-ready candidates | Corrected candidates |',
                 '| --- | ---: | ---: | ---: | ---: | ---: |']
        for language, settings in config.languages.items():
            pubs = [e for e in index['articles'] if e['language'] == language]
            latest = [t for (code,_),t in current_tasks.items() if code == language]
            rows.append(f'| `{language}` ({settings["tag"]}) | '
                        f'{sum(e["status"]=="ready" for e in pubs)} / {len(source["articles"])} | '
                        f'{sum(e["human_reviewed"] for e in pubs)} | '
                        f'{sum(t["status"] not in TERMINAL for t in latest)} | '
                        f'{sum(t["status"] in ("not_ready","budget_blocked","source_error") for t in latest)} | '
                        f'{sum(t["translation_attempts"] > 1 for t in latest)} |')
        rows += ['', '## Issue / language work', '',
                 'Only combinations with requested or published work appear below. Counts of pending/failed candidates are separate from existing public versions.', '',
                 '| Issue | Language | Ready / articles | Human reviewed | Active | Not ready | Proposals |',
                 '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
        for issue in source['issues']:
            ids = {a['id'] for a in source['articles'].values() if a['issue_id'] == issue['id']}
            for language in config.languages:
                pubs = [entries[(language,identity)] for identity in ids if (language,identity) in entries]
                latest = [current_tasks[(language,identity)] for identity in ids if (language,identity) in current_tasks]
                if not pubs and not latest:
                    continue
                rows.append(f'| `{issue["source_id"]}` | `{language}` | '
                            f'{sum(e["status"]=="ready" for e in pubs)} / {len(ids)} | '
                            f'{sum(e["human_reviewed"] for e in pubs)} | '
                            f'{sum(t["status"] not in TERMINAL for t in latest)} | '
                            f'{sum(t["status"] in ("not_ready","budget_blocked","source_error") for t in latest)} | '
                            f'{sum(t["status"]=="proposal" for t in latest)} |')
        recovery_policy = config.runtime.get('automatic_downstream_recovery', {})
        # Keep reporting on the same exact, non-recyclable ledger as acceptance.
        # Local imports avoid the recovery modules' dependency on TERMINAL.
        from .downstream import funding_ledger, frontier
        from .recovery import money
        funding = funding_ledger(self)
        recovery_frontier = frontier(config, self, tasks=list(task_by_id.values()), funding=funding)
        self.write('RECOVERY.json', recovery_frontier)
        allocated = funding['shared_policy_usd']
        cap = money(recovery_policy.get('total_budget_usd', 0))
        if not recovery_policy.get('enabled'):
            recovery_status = 'Paused: hourly/shared-policy recovery submissions are disabled; separately authorized manual workflow requests retain their own ceilings.'
        elif allocated + money(recovery_policy.get('campaign_budget_usd', 0)) > cap:
            recovery_status = 'Budget blocked: the next hourly recovery envelope does not fit the remaining authorization.'
        else:
            recovery_status = 'Enabled: eligible held candidates can enter bounded hourly recovery; passing all gates is still required.'
        rows += ['', '## Held-work recovery', '', recovery_status,
                 f'Hourly/shared-policy funding. Accepted lifetime recovery allocations: ${allocated:.6f} / ${cap:.2f}. '
                 'Allocations are not recycled after failure or cancellation.',
                 f'Separately authorized manual workflow allocations: ${funding["manual_workflow_usd"]:.6f} '
                 f'across {funding["manual_workflow_count"]} accepted runs. Each run is limited to its own explicit ceiling; '
                 'these permanent allocations do not consume or enable the hourly policy.',
                 'New continuation requests allow at most three accepted cycles per article/language/English fingerprint, '
                 'including historical cycles, and at most two per repair strategy. No-progress and ambiguous cases remain unfinished for attention.',
                 '[Recovery frontier](RECOVERY.json) lists every unfinished latest task, its accepted cycle count, '
                 'eligibility, complete-cycle reservation and explicit blocking reason. It is a derived report, not spending authority.',
                 ' | '.join(f'{reason}: {count}' for reason, count in recovery_frontier['counts'].items()),
                 'A finished original campaign remains historical; current publication readiness is shown in the issue/language rows.']
        from .review_diagnostics import preserved_review_diagnostics, render_review_diagnostics
        rows += render_review_diagnostics(preserved_review_diagnostics(config, self, tasks=tasks))
        errors = sorted((self.root/'state/queue-errors').glob('*.json'))
        if errors:
            rows += ['', '## Rejected requests', '', 'These requests did not start a paid campaign. Inspect the recorded validation error before submitting a new request.', '']
            for path in errors:
                relative = path.relative_to(self.root).as_posix()
                rows.append(f'- [`{path.stem}`]({relative})')
        edit_issues = [record for record in self.records() if (record.get('published') or {}).get('edit_issue')]
        if edit_issues:
            rows += ['', '## Publication edits needing attention', '',
                     'The edited working files are preserved. Their hash-verified last accepted versions remain exportable; other articles continue processing.', '']
            for record in edit_issues:
                pub = record['published']
                rows.append(f'- `{record["language"]}/{record["article_id"]}`: '
                            f'{pub["edit_issue"]["reason"]} [edited file]({pub["html_path"]})')
        rows += ['', '## Campaigns', '', '| Request | Trigger | Operation | Processing state | Tasks | Outcomes | Reported usage (USD) | Reserved ceiling (USD) | Report |',
                 '| --- | --- | --- | --- | ---: | --- | ---: | ---: | --- |']
        for campaign in self.campaigns():
            outcomes = {'complete':0, 'held':0, 'active':0, 'proposal':0, 'cancelled':0, 'unknown':0}
            for identity in campaign.get('tasks', []):
                task = task_by_id.get(identity)
                status = task.get('status') if task else None
                category = ('held' if status in ('not_ready','budget_blocked','source_error') else
                            'active' if status in ('queued','in_batch') else
                            status if status in ('complete','proposal','cancelled') else 'unknown')
                outcomes[category] += 1
            summary = ', '.join(f'{count} {label}' for label,count in outcomes.items() if count) or 'no task work'
            trigger = ('hourly recovery' if campaign.get('downstream_request', {}).get('scheduled_hour') else
                       'manual recovery' if campaign.get('downstream_recovery') else
                       'exact recovery' if campaign.get('recovery_of_campaign') else
                       'source refresh' if campaign.get('source_refresh') else 'manual')
            rows.append(f'| `{campaign["id"]}` | {trigger} | {campaign["operation"]} | {campaign["status"]} | {len(campaign.get("tasks",[]))} | '
                        f'{summary} | {campaign.get("reported_usage_usd",0):.8f} | '
                        f'{campaign.get("reserved_usd",0):.6f} / {campaign["budget_usd"]:.2f} | '
                        f'[state](state/campaigns/{campaign["id"]}.json) |')
        batches = self.batches()
        if batches:
            rows += ['', '## Provider batch lifecycle', '',
                     'Provider completion and local collection are separate clocks. Older records without provider timestamps remain unknown; legacy `completed_at` is local collection time. Provider completion does not mean a translation passed its quality gates.', '',
                     '| Batch / stage | Worker / provider status | Requests total / completed / failed | Provider completed (UTC) | Collected locally (UTC) | Last poll attempted (UTC) |',
                     '| --- | --- | --- | --- | --- | --- |']
            for batch in batches:
                counts = batch.get('remote_request_counts') or {}
                rows.append(f'| [{batch["id"]}](state/batches/{batch["id"]}/batch.json) / {batch["stage"]} | '
                            f'{batch["status"]} / {batch.get("remote_status","not recorded")} | '
                            f'{counts.get("total","?")} / {counts.get("completed","?")} / {counts.get("failed","?")} | '
                            f'{provider_time(batch.get("remote_completed_at"))} | '
                            f'{batch.get("collected_at") or batch.get("completed_at") or "not collected"} | '
                            f'{batch.get("last_polled_at","not recorded")} |')
            diagnostics = []
            for batch in batches:
                link = f'[batch {batch["id"]}](state/batches/{batch["id"]}/batch.json)'
                if batch.get('poll_error_type') or batch.get('collection_error_type'):
                    diagnostics.append(f'- {link}: Provider read failed. Check provider availability and project access; collection can resume without resubmitting the batch.')
                if batch.get('reconciliation_error_type') or batch['status'] == 'submission_unknown':
                    diagnostics.append(f'- {link}: Submission remains uncertain. Reconcile the submission key; never retry creation without verified absence and explicit owner confirmation.')
                for error in batch.get('remote_errors',[]):
                    safe = provider_error(error)
                    diagnostics.append(f'- {link}: `{safe["code"]}`. {safe["action"]}')
            for task in tasks:
                if task.get('provider_failure'):
                    safe = provider_error(task['provider_failure'])
                    diagnostics.append(f'- [task {task["id"]}](state/tasks/{task["id"]}/task.json): `{safe["code"]}`. {safe["action"]}')
            if diagnostics:
                rows += ['', '## Provider diagnostics', '', *diagnostics]
        write_text(self.path('STATUS.md'),'\n'.join(rows)+'\n')
        return index
