"""Persistent work records, review detection, and deterministic website metadata."""
from __future__ import annotations
import copy
from pathlib import Path
from .common import ContractError, digest, json_hash, now, read_json, safe_path, write_json, write_text
from .html import split_article, validate_translation

TERMINAL = {'complete','not_ready','proposal','cancelled','budget_blocked','source_error'}


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

    def publication_candidate(self, publication):
        text = self.path(publication['html_path']).read_text(encoding='utf-8')
        body, tail = split_article(text)
        metadata = self.read(publication['metadata_path'])
        if not isinstance(metadata,dict) or set(metadata) != {'title','subtitle','section'}:
            raise ContractError('Translation sidecar must contain title, subtitle, and section only')
        if tail and tail != publication['notice_html']:
            raise ContractError('AI notice was partially edited; retain it intact or remove the entire block after review')
        return {'html':body,**metadata}, tail, text

    def sync_human_reviews(self, gitstore):
        for record in self.records():
            pub = record.get('published')
            if not pub:
                continue
            candidate, tail, text = self.publication_candidate(pub)
            validate_translation(self.source(pub),candidate)
            new_html_hash = digest(text)
            metadata_hash = json_hash({k:candidate[k] for k in ('title','subtitle','section')})
            if new_html_hash == pub['html_sha256'] and metadata_hash == pub['metadata_sha256']:
                if bool(tail) == pub['human_reviewed']:
                    raise ContractError('Notice and stored human-review state disagree')
                continue
            changed_path = pub['html_path'] if new_html_hash != pub['html_sha256'] else pub['metadata_path']
            evidence = gitstore.human_edit_evidence(changed_path)
            pub.update(html_sha256=new_html_hash,metadata_sha256=metadata_hash,
                       human_reviewed=not bool(tail), human_review=evidence if not tail else None)
            record['history'].append({'event':'human_review' if not tail else 'human_edit_unreviewed',
                                      'evidence':evidence,'html_sha256':new_html_hash,
                                      'metadata_sha256':metadata_hash})
            self.save_record(record)

    def projection(self, config):
        source = self.read('state/source.json',{'revision':None,'articles':{},'issues':[]})
        entries = []
        for record in self.records():
            pub = record.get('published')
            if not pub:
                continue
            candidate, tail, text = self.publication_candidate(pub)
            parsed = validate_translation(self.source(pub), candidate)
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
        errors = sorted((self.root/'state/queue-errors').glob('*.json'))
        if errors:
            rows += ['', '## Rejected requests', '', 'These requests did not start a paid campaign. Inspect the recorded validation error before submitting a new request.', '']
            for path in errors:
                relative = path.relative_to(self.root).as_posix()
                rows.append(f'- [`{path.stem}`]({relative})')
        rows += ['', '## Campaigns', '', '| Request | Operation | Tasks | Reserved ceiling (USD) | Report |',
                 '| --- | --- | ---: | ---: | --- |']
        for campaign in self.campaigns():
            rows.append(f'| `{campaign["id"]}` | {campaign["operation"]} | {len(campaign.get("tasks",[]))} | '
                        f'{campaign.get("reserved_usd",0):.6f} / {campaign["budget_usd"]:.2f} | '
                        f'[state](state/campaigns/{campaign["id"]}.json) |')
        write_text(self.path('STATUS.md'),'\n'.join(rows)+'\n')
        return index
