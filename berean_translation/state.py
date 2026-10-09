"""Persistent work records, review detection, and deterministic website metadata."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
from pathlib import Path
from .batch_telemetry import provider_error
from .common import ContractError, digest, json_hash, read_json, read_regular_bytes, safe_path, write_json, write_text
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
                # A checkpoint excludes uncommitted technical replacements. Its
                # clean checkout may therefore contain the original accepted
                # blobs again; these are safe until sync clears the diagnostic.
                try:
                    restored = {'html':read_regular_bytes(self.path(publication['html_path'])).decode('utf-8'),
                                'metadata':self.read(publication['metadata_path'])}
                    publication_edits.verify(restored, publication)
                except (ContractError, OSError, UnicodeError):
                    raise ContractError('Publication working files changed after edit isolation; run review synchronization')
            return publication_edits.candidate(publication_edits.accepted_payload(self, publication))
        text = read_regular_bytes(self.path(publication['html_path'])).decode('utf-8')
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

        from .content_isolation import synchronize
        synchronize(self)

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

    def work_summary(self, config, *, index=None, tasks=None):
        """Classify every source/language pair once, independently of old attempts.

        Accepted publications stay published while a replacement is queued,
        running or held. Admission ledgers describe work that has no task yet;
        include them without authorizing work or rewriting their audit history.
        """
        source = self.read('state/source.json', {'articles': {}, 'issues': []})
        index = self.projection(config) if index is None else index
        tasks = self.tasks() if tasks is None else tasks
        by_id = {task['id']: task for task in tasks}
        records = {(record['language'], record['article_id']): record for record in self.records()}
        publications = {(item['language'], item['id']): item for item in index['articles']}
        campaigns = {campaign['id']: campaign for campaign in self.campaigns()}
        admissions = {}
        pending_automatic_requests = 0

        def admit(language, article_id, status, reason=None):
            key = (language, article_id)
            # An attention hold is visible even when another campaign also
            # requested the same pair. Active paid work takes precedence below.
            if key not in admissions or status == 'attention':
                admissions[key] = {'status': status, 'reason': reason}

        from .manual_admission import attention_superseded, legacy_targets
        for campaign in campaigns.values():
            if campaign.get('cancel_requested') or campaign.get('dry_run'):
                continue
            ledger = self.read(f'state/manual-admissions/{campaign["id"]}.json')
            if ledger is None:
                for item in legacy_targets(campaign):
                    admit(item['language'], item['article_id'], 'pending', 'manual_admission_pending')
                continue
            for entry in ledger.get('entries', {}).values():
                if entry.get('status') not in ('pending', 'ready', 'attention'):
                    continue
                if (entry.get('status') == 'attention' and entry.get('provenance')
                        and attention_superseded(self, campaign, entry)):
                    continue
                item = entry['item']
                materialized = by_id.get(entry.get('task_id'))
                if materialized is not None:
                    continue
                admit(item['language'], item['article_id'], entry['status'], entry.get('reason'))

        # Explicit never-admitted queue selections have known identities. Broad
        # requests retain their own non-authorizing campaign/admission reports
        # until selection is materialized; do not guess their future targets.
        for path in sorted((self.root / 'state/queue').glob('*.json')):
            request = read_json(path)
            if path.stem in campaigns or self.read(f'state/queue-errors/{path.stem}.json'):
                continue
            selection = request.get('selection')
            if not isinstance(selection, dict) or not selection.get('article_id'):
                continue
            if request.get('autonomous'):
                pending_automatic_requests += 1
            hold = self.read(f'state/automatic-holds/{path.stem}.json', {})
            reason = hold.get('reason')
            admit(selection['language'], selection['article_id'],
                  'attention' if reason and reason != 'prefetch_wait_budget' else 'pending',
                  reason or 'pending_automatic_admission')

        categories = ('published', 'unstarted', 'queued', 'active', 'held_without_publication')
        counts = dict.fromkeys(categories, 0)
        replacement_counts = dict.fromkeys(('queued', 'active', 'held'), 0)
        pairs = []
        source_errors = source.get('source_errors', {})
        for article_id, article in source.get('articles', {}).items():
            for language in config.languages:
                key = (language, article_id)
                record = records.get(key, {})
                task = by_id.get(record.get('latest_task'))
                publication = publications.get(key)
                admission = admissions.get(key)
                source_error = article.get('source_error') or source_errors.get(article_id)
                status = task.get('status') if task else None
                accepted_task = bool(publication and task
                                     and record.get('published', {}).get('task') == task['id'])
                if status == 'in_batch':
                    work = 'active'
                elif source_error:
                    work = 'held_without_publication'
                elif status == 'queued':
                    work = 'queued'
                elif admission and admission['status'] == 'attention':
                    work = 'held_without_publication'
                elif admission:
                    work = 'queued'
                elif publication and (status == 'complete' or accepted_task):
                    work = 'finished'
                elif status in TERMINAL:
                    work = 'held_without_publication'
                else:
                    work = 'unstarted'
                category = 'published' if publication else work
                counts[category] += 1
                row = {'language': language, 'article_id': article_id,
                       'issue_id': article['issue_id'], 'category': category,
                       'published': bool(publication),
                       'stale': bool(publication and publication['status'] != 'ready')}
                if task:
                    row['latest_task'] = task['id']
                if admission:
                    row.update(admission_status=admission['status'],
                               reason=admission['reason'] or 'manual_admission_' + admission['status'])
                if source_error:
                    row.update(source_attention=True, reason='source_error')
                replacement_pending = bool(admission or (task and status != 'complete' and not accepted_task))
                if publication and replacement_pending and work in ('queued', 'active', 'held_without_publication'):
                    replacement = 'held' if work == 'held_without_publication' else work
                    row['replacement_status'] = replacement
                    replacement_counts[replacement] += 1
                pairs.append(row)
        return {'target_pairs': len(pairs), 'counts': counts,
                'pending_automatic_requests': pending_automatic_requests,
                'source_stale': sum(row['stale'] for row in pairs),
                'source_attention': sum(bool(row.get('source_attention')) for row in pairs),
                'replacement_counts': replacement_counts, 'pairs': pairs}

    def derive(self, config):
        index = self.projection(config)
        self.write('index.json',index)
        source = self.read('state/source.json',{'revision':None,'articles':{},'issues':[]})
        tasks = self.tasks()
        task_by_id = {t['id']:t for t in tasks}
        current_tasks = {(r['language'],r['article_id']):task_by_id[r['latest_task']]
                         for r in self.records() if r.get('latest_task') in task_by_id}
        work = self.work_summary(config, index=index, tasks=tasks)
        issue_pairs, language_pairs, issue_language_pairs = {}, {}, {}
        for pair in work['pairs']:
            issue_pairs.setdefault(pair['issue_id'], []).append(pair)
            language_pairs.setdefault(pair['language'], []).append(pair)
            issue_language_pairs.setdefault((pair['issue_id'], pair['language']), []).append(pair)
        report_generation = self.read('state/report-generation.json', {})
        last_collection = self.read('state/last-collection.json', {})
        discovery = self.read('state/discovery-status.json', {})
        stamps = [value for value in (report_generation.get('generated_at'),
                  last_collection.get('completed_at'), discovery.get('observed_at')) if value]
        generated_at = max(stamps, key=datetime.fromisoformat) if stamps else 'not recorded'
        rows = ['# Translation status','',
                'Generated from the pinned source catalogue and durable work records. No API call is made by this report.',
                '', f'Generated: {generated_at}.',
                'Published means an accepted translation is exportable here; live deployment is verified separately.',
                'Target categories are disjoint. Accepted publications remain published during replacement work. '
                'Source-stale versions and replacement outcomes are reported separately.',
                'Ready means source-compatible and exportable here; live deployment is verified separately.',
                'Finished means processing has stopped, not that every requested translation passed. Held items still need action.',
                '',f'Observed English revision: `{source["revision"] or "not yet discovered"}`','',
                f'Total target: {work["target_pairs"]} article/language pairs. '
                + ' | '.join(f'{category.replace("_", " ")}: {count}' for category, count in work['counts'].items()) + '.', '']
        if last_collection:
            collection = last_collection.get('collection', {})
            rows += [f'Last collection: {last_collection.get("completed_at", "not recorded")}; '
                     f'newly published: {last_collection.get("newly_published", 0)}; '
                     f'stop reason: {collection.get("stop_reason", "not recorded")}; '
                     f'submitted batches remaining: {collection.get("submitted_batches", "not recorded")}.', '']
        rows += ['| Issue selector | Articles | Published / target | Unstarted | Queued / admission | Active | Held without publication | Source stale | Failed replacements |',
                 '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
        for issue in source['issues']:
            ids = [a['id'] for a in source['articles'].values() if a['issue_id'] == issue['id']]
            pairs = issue_pairs.get(issue['id'], [])
            counts = {category: sum(pair['category'] == category for pair in pairs) for category in work['counts']}
            rows.append(f'| `{issue["source_id"]}` | {len(ids)} | {counts["published"]} / {len(pairs)} | '
                        f'{counts["unstarted"]} | {counts["queued"]} | {counts["active"]} | '
                        f'{counts["held_without_publication"]} | {sum(pair["stale"] for pair in pairs)} | '
                        f'{sum(pair.get("replacement_status") == "held" for pair in pairs)} |')
        rows += ['', '## Language readiness', '',
                 '| Language | Published / source articles | Unstarted | Queued / admission | Active | Held without publication | Source stale | Failed replacements | Human reviewed | Corrected candidates |',
                 '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
        for language, settings in config.languages.items():
            pubs = [e for e in index['articles'] if e['language'] == language]
            latest = [t for (code,_),t in current_tasks.items() if code == language]
            pairs = language_pairs.get(language, [])
            counts = {category: sum(pair['category'] == category for pair in pairs) for category in work['counts']}
            rows.append(f'| `{language}` ({settings["tag"]}) | '
                        f'{counts["published"]} / {len(pairs)} | '
                        f'{counts["unstarted"]} | {counts["queued"]} | {counts["active"]} | '
                        f'{counts["held_without_publication"]} | {sum(pair["stale"] for pair in pairs)} | '
                        f'{sum(pair.get("replacement_status") == "held" for pair in pairs)} | '
                        f'{sum(e["human_reviewed"] for e in pubs)} | '
                        f'{sum(t["translation_attempts"] > 1 for t in latest)} |')
        rows += ['', '## Issue / language work', '',
                 'Every source issue and configured language appears, including unstarted work. '
                 'Replacement failures never subtract accepted publications.', '',
                 '| Issue | Language | Published / articles | Unstarted | Queued / admission | Active | Held without publication | Failed replacements |',
                 '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
        for issue in source['issues']:
            ids = {a['id'] for a in source['articles'].values() if a['issue_id'] == issue['id']}
            for language in config.languages:
                pairs = issue_language_pairs.get((issue['id'], language), [])
                counts = {category: sum(pair['category'] == category for pair in pairs) for category in work['counts']}
                rows.append(f'| `{issue["source_id"]}` | `{language}` | '
                            f'{counts["published"]} / {len(ids)} | '
                            f'{counts["unstarted"]} | {counts["queued"]} | {counts["active"]} | '
                            f'{counts["held_without_publication"]} | '
                            f'{sum(pair.get("replacement_status") == "held" for pair in pairs)} |')
        # Keep reporting on the same exact, non-recyclable ledger as acceptance.
        from .downstream import funding_ledger
        funding = funding_ledger(self)
        from . import autonomous
        recovery_frontier = autonomous.frontier(config, self, tasks, work_summary=work)
        self.write('RECOVERY.json', recovery_frontier)
        self.write('state/automatic-status.json', {
            'allocated_usd': recovery_frontier['committed_usd'],
            'approved_total_usd': recovery_frontier['approved_total_usd'],
            'pending_requests': work['pending_automatic_requests'],
            'target_pairs': work['target_pairs'], 'counts': work['counts'],
            'replacement_counts': work['replacement_counts'], 'generated_at': generated_at})
        rows += ['', '## Automatic archive work', '',
                f'Automatic policy enabled: {autonomous.enabled(config)}. '
                'Scheduled discovery and collection create missing work across the whole archive and all configured languages. '
                'Disable repository Actions to stop starting work; already submitted provider batches may finish.',
                f'Automatic committed ceiling: ${recovery_frontier["committed_usd"]:.6f} / '
                f'${recovery_frontier["approved_total_usd"]:.2f}. '
                'This is a cumulative cap with no automatic renewal. Accepted legacy recovery allocations remain charged in full. '
                'Accepted legacy refresh and manual envelopes retain their separate original authority.',
                'New automatic work reserves its complete remaining stage chain, up to $10 per envelope. '
                'Proven unused reservations settle only when every potentially billable request has complete terminal usage evidence. '
                'Usage is provider-reported and priced at frozen rates, not invoice reconciliation.',
                'Funded progressing repairs can continue beyond three historical cycles. Repeated or uncertain progress, '
                'refusals and unknown outcomes remain held for attention. Human-reviewed pairs never enter AI work.',
                '[Recovery frontier](RECOVERY.json) records current holds and funding.']
        rows += ['', '## Previously accepted recovery authority', '',
                 f'Accepted shared allocations: ${funding["shared_policy_usd"]:.6f} '
                 f'across {funding["shared_policy_count"]} runs. '
                 'Allocations are not recycled after failure or cancellation.',
                 f'Separately authorized manual workflow allocations: ${funding["manual_workflow_usd"]:.6f} '
                 f'across {funding["manual_workflow_count"]} accepted runs. Each run is limited to its own explicit ceiling; '
                 'each prior run retains its frozen funding and attempt limits.',
                 '[Recovery frontier](RECOVERY.json) lists missing work, admission holds, latest task outcomes '
                 'and explicit blocking reasons. It is a derived report, not spending authority.',
                 ' | '.join(f'{reason}: {count}' for reason, count in recovery_frontier['counts'].items()),
                 'A finished original campaign remains historical; current publication readiness is shown in the issue/language rows.']
        from .review_diagnostics import preserved_review_diagnostics, render_review_diagnostics
        rows += render_review_diagnostics(preserved_review_diagnostics(config, self, tasks=tasks))
        isolated = self.read('state/content-isolation.json', {}).get('files', {})
        if isolated:
            rows += ['', '## Unrecognized content files', '',
                     f'{len(isolated)} working file(s) are isolated because they have no accepted publication path. '
                     'Their bytes remain untouched and are not exported or automatically added to Git. '
                     'Accepted articles continue from their verified copies. '
                     '[Inspect the isolated paths](state/content-isolation.json).']
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
            from .manual_admission import counts, UNFINISHED
            for status, count in counts(self, campaign).items():
                if status in UNFINISHED and count and not campaign.get('cancel_requested'):
                    outcomes['admission '+status] = count
            summary = ', '.join(f'{count} {label}' for label,count in outcomes.items() if count) or 'no task work'
            trigger = ('automatic archive' if campaign.get('autonomous') else
                       'hourly recovery' if campaign.get('downstream_request', {}).get('scheduled_hour') else
                       'manual recovery' if campaign.get('downstream_recovery') else
                       'source refresh' if campaign.get('source_refresh') else 'manual')
            rows.append(f'| `{campaign["id"]}` | {trigger} | {campaign["operation"]} | {campaign["status"]} | {len(campaign.get("tasks",[]))} | '
                        f'{summary} | {campaign.get("reported_usage_usd",0):.8f} | '
                        f'{campaign.get("reserved_usd",0):.6f} / {campaign["budget_usd"]:.2f} | '
                        f'[state](state/campaigns/{campaign["id"]}.json)'
                        + (f' / [admissions](state/manual-admissions/{campaign["id"]}.json)'
                           if self.read(f'state/manual-admissions/{campaign["id"]}.json') else '') + ' |')
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
