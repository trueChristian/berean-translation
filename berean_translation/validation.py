"""Offline invariants and display export with truthful retained-source provenance."""
from __future__ import annotations
import copy
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from .common import ContractError, digest, json_hash, loads, read_json, safe_path, uuid, write_json, write_text
from .html import rewrite_export_urls, validate_translation
from .source import translation_key
from .recovery import validate_recoveries
from .state import State, TERMINAL
from .review_notice import validate_human_content, validate_recorded_notice


def validate_repository(config, check_index=True):
    state = State(config.root)
    expected_files = set()
    seen = set()
    for record in state.records():
        language, identity = record['language'],uuid(record['article_id'])
        if language not in config.languages or (language,identity) in seen:
            raise ContractError('Invalid or duplicate translation record')
        seen.add((language,identity))
        pub = record.get('published')
        if not pub:
            continue
        if pub['html_path'] != f'content/{language}/articles/{identity}.html' or pub['metadata_path'] != f'content/{language}/articles/{identity}.json':
            raise ContractError('Translation publication paths do not match their identities')
        expected_files.update((pub['html_path'],pub['metadata_path']))
        source = state.source(pub)
        candidate,tail,text = state.publication_candidate(pub)
        if pub['human_reviewed']:
            validate_human_content(candidate, tail, identity)
        else:
            validate_translation(source,candidate,language=language)
        validate_recorded_notice(pub, tail)
        if digest(text) != pub['html_sha256'] or json_hash({k:candidate[k] for k in ('title','subtitle','section')}) != pub['metadata_sha256']:
            raise ContractError('Published files changed without review synchronization')
        if source['translation_key'] != pub['translation_key'] or source['article']['id'] != identity:
            raise ContractError('Publication source identity/fingerprint mismatch')
        if not isinstance(pub.get('model'),str) or not isinstance(pub.get('review_model'),str):
            raise ContractError('Missing model provenance')
    from . import content_isolation
    observed_content = content_isolation.inventory(config.root)
    content_isolation.validate(state, observed_content)
    actual_files = set(observed_content)
    isolated_missing = {record['published'][field] for record in state.records()
                        if (record.get('published') or {}).get('edit_issue')
                        for field, value in record['published']['edit_issue']['observed_files'].items()
                        if not isinstance(value, str) and record['published'][field] not in actual_files}
    if actual_files & expected_files != expected_files - isolated_missing:
        raise ContractError('Missing accepted content files without a verified retained copy')
    task_list = state.tasks()
    tasks = {t['id']:t for t in task_list}
    if len(tasks) != len(task_list):
        raise ContractError('Duplicate task identity in runtime history')
    for task in tasks.values():
        if not re.fullmatch(r'[a-f0-9]{32}',task['id']) or task['language'] not in config.languages:
            raise ContractError('Invalid task identity')
        if task['status'] not in TERMINAL | {'queued','in_batch'}:
            raise ContractError('Unknown task status')
        if task['stage'] not in ('translate','review1','correct','review2'):
            raise ContractError('Invalid task stage')
        if not 0 <= task['translation_attempts'] <= 2 or not 0 <= task['review_attempts'] <= 2:
            raise ContractError('Attempt limit violated')
        state.source(task)
    for campaign in state.campaigns():
        from .review_contract import frozen_version
        frozen_version(campaign)
        from .scripture_evidence import frozen_policy
        frozen_policy(campaign)
        if not 0 <= campaign['reserved_usd'] <= campaign['budget_usd']+1e-8:
            raise ContractError('Campaign budget invariant violated')
        if any(identity not in tasks for identity in campaign['tasks']):
            raise ContractError('Campaign references a missing task')
    from . import manual_admission
    manual_admission.validate_history(state)
    validate_recoveries(state, tasks, state.campaigns(), config.runtime['max_tasks_per_request'])
    from .downstream import validate_history
    validate_history(config, state, tasks)
    from .autonomous import validate_history as validate_automatic_history
    validate_automatic_history(config, state)
    batches = {batch['id']:batch for batch in state.batches()}
    for batch in batches.values():
        payload = state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Batch input payload changed')
        if len(batch['custom_ids']) != len(set(batch['custom_ids'])):
            raise ContractError('Duplicate batch request identities')
        if any(identity not in tasks for identity in batch['tasks']):
            raise ContractError('Batch references a missing task')
        if batch.get('replacement_batch'):
            replacement = batches.get(batch['replacement_batch'])
            if not replacement or replacement.get('reservation_reused_from') != batch['id']:
                raise ContractError('Prepared-batch replacement must retain its reciprocal reservation link')
        if batch.get('reservation_reused_from'):
            parent = batches.get(batch['reservation_reused_from'])
            if (not parent or parent.get('status') != 'cancelled_before_submission'
                    or parent.get('exclusion_reason') != 'human_editorial_authority'
                    or parent.get('replacement_batch') != batch['id'] or parent.get('remote_id')
                    or (parent.get('submission_started_at') and parent.get('create_not_called') is not True)
                    or any(batch.get(key) != parent.get(key) for key in
                           ('campaign','stage','model','reserved_usd'))):
                raise ContractError('Prepared-batch reservation transfer is not proven unsubmitted')
            kept = [identity for identity in parent['tasks'] if identity not in parent['excluded_task_ids']]
            original = state.path(f'state/batches/{parent["id"]}/input.jsonl').read_bytes()
            expected = b''.join(line for line in original.splitlines(keepends=True)
                                if loads(line)['custom_id'] in batch['custom_ids'])
            if (batch['tasks'] != kept or batch['custom_ids'] != [identity+':'+batch['stage'] for identity in kept]
                    or payload != expected):
                raise ContractError('Prepared-batch replacement changed an authorized request')
    result = state.projection(config)
    if check_index and state.read('index.json') != result:
        raise ContractError('Generated index is stale; derive it before publication')
    return {'published':len(result['articles']),'ready':sum(a['status']=='ready' for a in result['articles']),
            'tasks':len(tasks),'campaigns':len(state.campaigns()),'batches':len(state.batches())}


def export(config, destination: Path, source_inventory: dict, source_revision: str, base='/', translation_revision=None):
    validate_repository(config)
    root = config.root.resolve()
    destination = destination.absolute()
    resolved = destination.resolve()
    if destination.is_symlink() or resolved == root or resolved in root.parents:
        raise ContractError('Export destination overlaps source or is a symlink')
    if resolved.is_relative_to(root) and not resolved.is_relative_to(root/'.build'):
        raise ContractError('Exports inside the repository must be below .build/')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ContractError('Export destination must be absent or empty; existing data is never deleted')
    if not re.fullmatch(r'[0-9a-f]{40}',source_revision):
        raise ContractError('Export requires the exact English source revision')
    if source_inventory.get('fingerprint_origin') != 'translation-runtime' or source_inventory.get('revision') != source_revision:
        raise ContractError('Export requires a current runtime scan of the selected English checkout')
    source_articles = source_inventory.get('articles')
    if not isinstance(source_articles,dict):
        raise ContractError('English source scan has no article inventory')
    if (root/'.git').exists():
        def git(*args):
            return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True).stdout.strip()
        if git('status','--porcelain','--untracked-files=all','--','content','state','index.json','config','prompts','berean_translation'):
            raise ContractError('Commit translation inputs before exporting')
        actual_revision = git('rev-parse','HEAD')
        if translation_revision and translation_revision != actual_revision:
            raise ContractError('Translation revision differs from the checkout')
        translation_revision = actual_revision
    if not translation_revision or not re.fullmatch(r'[0-9a-f]{40}',translation_revision):
        raise ContractError('Export requires an exact translation revision')
    # Validate base even when the archive is empty.
    rewrite_export_urls('',base,config.runtime['english_route'],'00000000-0000-4000-8000-000000000001')
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.translation-export-',dir=destination.parent))
    try:
        entries = []
        omitted = []
        retained = []
        for item in State(root).projection(config)['articles']:
            observed = source_articles.get(item['id'])
            target = copy.deepcopy(item)
            publication = State(root).record(item['language'], item['id'])['published']
            target['status'] = ('source_removed' if not observed else
                                'stale' if observed['translation_key'] != item['source_translation_key'] else 'ready')
            if observed:
                target['issue_id'] = observed['issue_id']
            if target['status'] != 'ready':
                frozen_source = State(root).source(publication)
                target['issue_id'] = frozen_source['article']['issue_id']
                reason = 'english_removed' if not observed else 'english_changed'
                target.update(retained=True, retention_reason=reason,
                              current_source_translation_key=observed['translation_key'] if observed else None,
                              retained_source={
                                  **{key:copy.deepcopy(frozen_source[key]) for key in
                                     ('article','fingerprints','repository','revision','translation_key')},
                                  'snapshot_sha256':Path(publication['source_snapshot']).stem,
                                  'article_id':item['id'],
                                  'html_repository_path':f'content/articles/{item["id"]}.html',
                                  'html_sha256':frozen_source['fingerprints']['html_sha256'],
                                  'index_repository_path':'index.json',
                                  'catalogue_repository_path':'catalogue.json'})
                retained.append({'id':item['id'],'language':item['language'],'reason':reason,
                                 'source_revision':item['source_revision']})
            candidate, tail, text = State(root).publication_candidate(publication)
            if (digest(text) != item['html_sha256'] or
                    json_hash({key:candidate[key] for key in ('title','subtitle','section')}) != item['metadata_sha256']):
                raise ContractError('Accepted publication changed during export; retain the prior deployment and retry')
            output = rewrite_export_urls(text,base,config.runtime['english_route'],item['id'])
            write_text(safe_path(stage,item['html']),output)
            write_json(safe_path(stage,item['metadata']),{key:candidate[key] for key in ('title','subtitle','section')})
            target['html_sha256'] = digest(output)
            target['images'] = [{'public_path':base.rstrip('/')+image['public_path'],'alt':image['alt']}
                                for image in item.get('images',[])]
            entries.append(target)
        index = {'format_version':'1.0','source_repository':config.runtime['source_repository'],
                 'source_revision':source_revision,'translation_revision':translation_revision,
                 'base_path':base,'articles':entries}
        write_json(stage/'index.json',index)
        manifest = {'format_version':'1.0','translation_revision':translation_revision,'source_revision':source_revision,
                    'base_path':base,'article_count':len(entries),'omitted':omitted,'retained':retained,
                    'files':{p.relative_to(stage).as_posix():digest(p.read_bytes()) for p in sorted(stage.rglob('*')) if p.is_file()}}
        write_json(stage/'manifest.json',manifest)
        if destination.exists():
            destination.rmdir()
        os.replace(stage,destination)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)
