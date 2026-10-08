"""Command-line entry points used identically by local runs and GitHub Actions."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4
from .common import ContractError, csv_values, loads, now, read_json, write_json
from .collector import collect_window
from .config import Config
from .engine import Engine
from .gitstore import GitStore
from .provider import OpenAIProvider
from .queue import enqueue_github
from .source import SourceClient
from .state import State
from .validation import export, validate_repository
from . import plain_policy


def env_bool(name, default=False):
    raw = os.environ.get(name,str(default)).lower()
    if raw not in ('true','false'):
        raise ContractError(f'{name} must be true or false')
    return raw == 'true'


def manual_repair_authorization(request):
    """Bind one explicit request to the trusted Actions dispatch, not an input flag.

    GitHub supplies this context to the main-only workflow. It is durable audit
    provenance within the trusted repository, not a bearer token or permission
    to start further runs. Reruns keep the original run ID and request identity.
    """
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    workflow_ref = repository + '/.github/workflows/ai-repair.yml@refs/heads/main'
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_WORKFLOW_REF') != workflow_ref):
        raise ContractError('A manual repair budget requires the trusted main AI Repair workflow_dispatch context')
    return {'kind': 'github_workflow_dispatch', 'repository': repository,
            'workflow_ref': workflow_ref, 'run_id': os.environ.get('GITHUB_RUN_ID', ''),
            'actor': request.get('requested_by')}


def component_authorization():
    from .scripture_component_plans import WORKFLOW
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    workflow = repository + '/' + WORKFLOW + '@refs/heads/main'
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_WORKFLOW_REF') != workflow):
        raise ContractError('Component selection requires the trusted main Scripture workflow_dispatch context')
    return {'kind': 'github_workflow_dispatch', 'repository': repository,
            'workflow_ref': workflow, 'run_id': os.environ.get('GITHUB_RUN_ID', ''),
            'actor': os.environ.get('GITHUB_ACTOR', '')}


def component_summary(package):
    return {key: package[key] for key in ('sha256', 'source_sha256', 'current_source_revision',
            'holds', 'funding_allocated', 'publication_ready', 'selected_entry_ids')} | {
        'evidence_backed_entries': [{key: item[key] for key in ('entry_id', 'language', 'stage_budget')}
                                   for item in package['entries']]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    commands = parser.add_subparsers(dest='command',required=True)
    validation = commands.add_parser('validate')
    validation.add_argument('--recognize-human-edits',action='store_true')
    commands.add_parser('derive')
    commands.add_parser('enqueue-env')
    components = commands.add_parser('inspect-scripture-components',
        help='Read-only bounded Jacques source/evidence inspection; no admission or model calls')
    components.add_argument('--evidence-dir', type=Path)
    components.add_argument('--fetch-evidence', action='store_true',
        help='Explicitly fetch missing anonymous approved GetBible chapter envelopes')
    components.add_argument('--output', type=Path,
        help='Write the complete hash-bound package outside the repository')
    commands.add_parser('enqueue-scripture-components-env',
        help='Trusted main workflow only; dry-run and empty selection by default')
    discover = commands.add_parser('discover')
    discover.add_argument('--check-only',action='store_true')
    worker = commands.add_parser('tick')
    worker.add_argument('--publish',action='store_true')
    mode = worker.add_mutually_exclusive_group()
    mode.add_argument('--discover-only', action='store_true',
                      help='Read coherent English source and queue work without calling OpenAI')
    mode.add_argument('--no-discover', action='store_true',
                      help='Collect and resume using the last coherent discovered source snapshot')
    worker.add_argument('--wait-seconds',type=int,default=0,
                        help='Bounded active-work pickup window (0..900 seconds; default: one tick)')
    worker.add_argument('--poll-seconds',type=int,default=60,
                        help='Submitted-batch poll interval (30..300 seconds)')
    cancel = commands.add_parser('cancel')
    cancel.add_argument('--campaign',required=True)
    cancel.add_argument('--publish',action='store_true')
    resolve = commands.add_parser('resolve-absent')
    resolve.add_argument('--batch',required=True)
    resolve.add_argument('--confirmed-no-remote-batch',action='store_true',required=True)
    resolve.add_argument('--publish',action='store_true')
    output = commands.add_parser('export')
    output.add_argument('--output',type=Path,required=True)
    output.add_argument('--source-checkout',type=Path,required=True,
                        help='English checkout used by the website build; hashes are computed automatically')
    output.add_argument('--base',default='/')
    try:
        args = parser.parse_args(argv)
        config = Config(args.root.resolve())
        state = State(config.root)
        if args.command == 'validate':
            if args.recognize_human_edits:
                state.sync_human_reviews(GitStore(config.root))
                # Records may already have been backfilled in a reviewed commit.
                # Reports are derived views, so refresh them even when recognition
                # is idempotent; strict repository validation still follows.
                state.derive(config)
            result = validate_repository(config)
        elif args.command == 'derive':
            result = state.derive(config)
        elif args.command in ('inspect-scripture-components', 'enqueue-scripture-components-env'):
            if plain_policy.enabled(config):
                raise ContractError('Scripture processing is retired; use ordinary translation or improvement review')
            from .scripture_component_plans import (ChapterProvider, inspect_jacques, build_request,
                validate_request as validate_component_request, external_path)
            enqueue = args.command == 'enqueue-scripture-components-env'
            authorization = component_authorization() if enqueue else None
            raw_selection = os.environ.get('INPUT_ENTRY_IDS', '').strip() if enqueue else ''
            selected = csv_values(raw_selection) if raw_selection else []
            dry_run = env_bool('INPUT_DRY_RUN', True) if enqueue else True
            output = os.environ.get('SCRIPTURE_COMPONENT_PLAN_OUTPUT') if enqueue else args.output
            output = external_path(config.root, output) if output else None
            identity = 'component-gh-' + authorization['run_id'] if enqueue else None
            previous = state.read(f'state/queue/{identity}.json') if enqueue else None
            if previous is not None:
                validate_component_request(config, previous)
                if previous['authorization'] != authorization or previous['selected_entry_ids'] != selected:
                    raise ContractError('This workflow run already has a different immutable component selection')
                # A rerun cannot refresh timestamps/evidence and replace its request.
                result = {'already_queued': True, 'request': identity,
                          'selected_entry_ids': selected, 'publication_ready': False}
            else:
                if enqueue and not dry_run and not selected:
                    raise ContractError('Admission requires an explicit nonempty list of inspected entry IDs')
                directory = os.environ.get('SCRIPTURE_COMPONENT_EVIDENCE_DIR') if enqueue else args.evidence_dir
                provider = ChapterProvider(config.root, directory, fetch=True if enqueue else args.fetch_evidence)
                # No OpenAI provider, tick, discovery State write, or Git checkpoint.
                engine = Engine(config, SourceClient(config), None, GitStore(config.root, publish=False))
                package = inspect_jacques(engine, provider)
                if output:
                    write_json(output, package)
                result = component_summary(package)
                result.update(dry_run=dry_run, requested_entry_ids=selected)
                if not dry_run:
                    request = build_request(package, selected, request_id=identity, authorization=authorization)
                    result['queue'] = enqueue_github(config, request, authorization['repository'], os.environ.get('GH_TOKEN', ''))
                elif not output and not enqueue:
                    result = package
        elif args.command == 'enqueue-env':
            if os.environ.get('GITHUB_REF') != 'refs/heads/main':
                raise ContractError('Manual paid-work requests must run from main')
            selection_mode = os.environ.get('TRANSLATION_SELECTION', 'ordinary')
            if selection_mode not in ('ordinary', 'exact-recovery', 'downstream-recovery'):
                raise ContractError('Unknown manual selection mode')
            request = {'id':'gh-'+os.environ['GITHUB_RUN_ID'],
                       'operation':os.environ['TRANSLATION_OPERATION'],
                       'model':os.environ.get('INPUT_MODEL') or config.runtime['default_model'],
                       'review_model':os.environ.get('INPUT_REVIEW_MODEL') or config.runtime['default_review_model'],
                       'budget_usd':os.environ.get('INPUT_BUDGET_USD',str(config.runtime['default_budget_usd'])),
                       'dry_run':env_bool('INPUT_DRY_RUN',True),'retry_failed':env_bool('INPUT_RETRY_FAILED'),
                       'requested_by':os.environ.get('GITHUB_ACTOR')}
            if selection_mode == 'downstream-recovery':
                from .downstream import validate_request
                if request.pop('retry_failed') or any(os.environ.get(key, '').strip() for key in
                        ('INPUT_LANGUAGE', 'INPUT_LANGUAGES', 'INPUT_ISSUES', 'INPUT_ISSUE_SELECTION',
                         'INPUT_ORIGINAL_CAMPAIGN', 'INPUT_PREVIOUS_TASK_IDS')):
                    raise ContractError('Downstream recovery cannot combine broad, exact or retry selectors')
                policy = config.runtime.get('automatic_downstream_recovery', {})
                request.update(max_articles=int(os.environ.get('INPUT_MAX_ARTICLES', '3')),
                    budget_usd=os.environ.get('INPUT_BUDGET_USD', ''),
                    model=os.environ.get('INPUT_MODEL') or policy.get('model', 'gpt-6.1-sol'),
                    review_model=os.environ.get('INPUT_REVIEW_MODEL') or policy.get('review_model', 'gpt-6.1-sol'))
                version = os.environ.get('TRANSLATION_CONTINUATION_VERSION')
                if version is not None:
                    if version != '2':
                        raise ContractError('Unsupported downstream continuation version')
                    from .continuation import policy as continuation_policy
                    request['continuation_policy'] = continuation_policy(
                        int(os.environ.get('INPUT_MAX_CANDIDATE_BYTES', '120000')))
                if not request['dry_run'] or os.environ.get('GITHUB_ACTIONS') == 'true':
                    request['manual_authorization'] = manual_repair_authorization(request)
                validate_request(config, request)
            elif selection_mode == 'exact-recovery':
                request['budget_usd'] = os.environ.get('INPUT_BUDGET_USD', '')
                if any(os.environ.get(key, '').strip() for key in
                       ('INPUT_LANGUAGE', 'INPUT_LANGUAGES', 'INPUT_ISSUES', 'INPUT_ISSUE_SELECTION')):
                    raise ContractError('Exact recovery cannot be combined with language or issue selectors')
                request.update(recovery_of_campaign=os.environ.get('INPUT_ORIGINAL_CAMPAIGN', '').strip(),
                               previous_task_ids=csv_values(os.environ.get('INPUT_PREVIOUS_TASK_IDS', '')))
            else:
                if any(key in os.environ for key in ('INPUT_ORIGINAL_CAMPAIGN', 'INPUT_PREVIOUS_TASK_IDS')):
                    raise ContractError('Recovery inputs require exact-recovery selection mode')
                selector = os.environ.get('INPUT_ISSUES','').strip() or os.environ.get('INPUT_ISSUE_SELECTION','next')
                if selector == 'custom':
                    raise ContractError('Provide issue UUIDs/source IDs in the issues field')
                request.update(languages=os.environ.get('INPUT_LANGUAGES','').strip() or os.environ.get('INPUT_LANGUAGE','all'),
                               issues=selector)
            result = enqueue_github(config,request,os.environ['GITHUB_REPOSITORY'],os.environ.get('GH_TOKEN',''))
        elif args.command == 'discover':
            source = SourceClient(config)
            result = source.discover()
            if not args.check_only:
                state.write('state/source.json',result); state.derive(config)
            result = {'revision':result['revision'],'issues':len(result['issues']),'articles':len(result['articles'])}
        elif args.command == 'export':
            inventory = SourceClient(config, checkout=args.source_checkout).discover()
            result = export(config,args.output,inventory,inventory['revision'],args.base)
        else:
            if os.environ.get('OPENAI_API_KEY') and not args.publish:
                raise ContractError('Real-key collection and maintenance require --publish for durable checkpoints')
            store = GitStore(config.root,publish=args.publish)
            provider = (OpenAIProvider() if os.environ.get('OPENAI_API_KEY')
                        and not getattr(args, 'discover_only', False) else None)
            engine = Engine(config,SourceClient(config),provider,store)
            if args.command == 'tick':
                window_started_at = now()
                if args.discover_only:
                    engine.tick(discover_only=True)
                    collection = {'ticks': 1, 'stop_reason': 'discovery_complete', 'submitted_batches': 0}
                else:
                    collection = collect_window(engine,wait_seconds=args.wait_seconds,poll_seconds=args.poll_seconds,
                                                discover_source=not args.no_discover)
                if plain_policy.enabled(config):
                    report = state.read('state/last-collection.json', {})
                    report.update(started_at=window_started_at, completed_at=now(),
                                  collection=collection)
                    if 'translations_published' in collection:
                        report['newly_published'] = collection['translations_published']
                    state.write('state/last-collection.json', report)
                    state.derive(config)
                    engine.checkpoint('runtime: report completed collection window')
                result = validate_repository(config)
                result['api_key_configured'] = bool(provider)
                result['collection'] = collection
            elif args.command == 'cancel':
                engine.cancel_campaign(args.campaign)
                result = {'campaign':args.campaign,'cancel_requested':True}
            else:
                import re
                if not re.fullmatch(r'[a-f0-9]{32}',args.batch):
                    raise ContractError('Invalid internal batch identity')
                batch = state.read(f'state/batches/{args.batch}/batch.json')
                if not batch or batch['status'] != 'submission_unknown':
                    raise ContractError('Only an uncertain submission may be explicitly reset')
                if not provider:
                    raise ContractError('An API key is required to verify remote batch absence')
                if provider.find(batch['id']):
                    raise ContractError('A matching OpenAI batch exists; run the collector to reconcile instead')
                batch['status'] = 'prepared'
                batch['manual_absence_confirmation'] = {'actor':os.environ.get('GITHUB_ACTOR'),
                                                        'note':'Owner confirmed no remote batch; authorizes one fresh submission.'}
                state.save_batch(batch)
                store.checkpoint('runtime: record owner-confirmed reset of an absent batch')
                result = {'batch':batch['id'],'status':batch['status']}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary,'a',encoding='utf-8') as handle:
                handle.write('### Berean translation\n\n```json\n'+json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n')
        return 0
    except (ContractError,KeyError,OSError,ValueError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
