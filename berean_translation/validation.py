"""Offline invariants and a display-only, source-compatible translation export."""
from __future__ import annotations
import copy
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from .common import ContractError, digest, json_hash, read_json, safe_path, uuid, write_json, write_text
from .html import rewrite_export_urls, validate_translation
from .source import translation_key
from .state import State, TERMINAL


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
        validate_translation(source,candidate)
        if bool(tail) == pub['human_reviewed']:
            raise ContractError('Notice state disagrees with human review; run the review-sync worker')
        if digest(text) != pub['html_sha256'] or json_hash({k:candidate[k] for k in ('title','subtitle','section')}) != pub['metadata_sha256']:
            raise ContractError('Published files changed without review synchronization')
        if source['translation_key'] != pub['translation_key'] or source['article']['id'] != identity:
            raise ContractError('Publication source identity/fingerprint mismatch')
        if not isinstance(pub.get('model'),str) or not isinstance(pub.get('review_model'),str):
            raise ContractError('Missing model provenance')
    actual_files = {p.relative_to(config.root).as_posix() for p in (config.root/'content').rglob('*') if p.is_file()}
    if actual_files != expected_files:
        raise ContractError('Missing or orphaned content files')
    tasks = {t['id']:t for t in state.tasks()}
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
        if not 0 <= campaign['reserved_usd'] <= campaign['budget_usd']+1e-8:
            raise ContractError('Campaign budget invariant violated')
        if any(identity not in tasks for identity in campaign['tasks']):
            raise ContractError('Campaign references a missing task')
    for batch in state.batches():
        payload = state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
        if digest(payload) != batch['payload_sha256']:
            raise ContractError('Batch input payload changed')
        if len(batch['custom_ids']) != len(set(batch['custom_ids'])):
            raise ContractError('Duplicate batch request identities')
        if any(identity not in tasks for identity in batch['tasks']):
            raise ContractError('Batch references a missing task')
    result = state.projection(config)
    if check_index and state.read('index.json') != result:
        raise ContractError('Generated index is stale; derive it before publication')
    return {'published':len(result['articles']),'ready':sum(a['status']=='ready' for a in result['articles']),
            'tasks':len(tasks),'campaigns':len(state.campaigns()),'batches':len(state.batches())}


def export(config, destination: Path, source_manifest: dict, source_revision: str, base='/', translation_revision=None):
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
    if source_manifest.get('format_version') != '2.0':
        raise ContractError('Unsupported English source manifest')
    fingerprints = source_manifest.get('articles',source_manifest.get('source_article_fingerprints'))
    if not isinstance(fingerprints,dict):
        raise ContractError('English manifest has no article fingerprints')
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
        for item in State(root).projection(config)['articles']:
            fp = fingerprints.get(item['id'])
            if not fp or translation_key(fp) != item['source_translation_key']:
                omitted.append({'id':item['id'],'language':item['language'],'reason':'incompatible_with_selected_English_source'})
                continue
            target = copy.deepcopy(item)
            target['status'] = 'ready'
            text = safe_path(root,item['html']).read_text(encoding='utf-8')
            output = rewrite_export_urls(text,base,config.runtime['english_route'],item['id'])
            write_text(safe_path(stage,item['html']),output)
            write_json(safe_path(stage,item['metadata']),read_json(safe_path(root,item['metadata'])))
            target['html_sha256'] = digest(output)
            target['images'] = [{'public_path':base.rstrip('/')+image['public_path'],'alt':image['alt']}
                                for image in item.get('images',[])]
            entries.append(target)
        index = {'format_version':'1.0','source_repository':config.runtime['source_repository'],
                 'source_revision':source_revision,'translation_revision':translation_revision,
                 'base_path':base,'articles':entries}
        write_json(stage/'index.json',index)
        manifest = {'format_version':'1.0','translation_revision':translation_revision,'source_revision':source_revision,
                    'base_path':base,'article_count':len(entries),'omitted':omitted,
                    'files':{p.relative_to(stage).as_posix():digest(p.read_bytes()) for p in sorted(stage.rglob('*')) if p.is_file()}}
        write_json(stage/'manifest.json',manifest)
        if destination.exists():
            destination.rmdir()
        os.replace(stage,destination)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)
