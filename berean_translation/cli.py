"""Command-line entry points used identically by local runs and GitHub Actions."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4
from .common import ContractError, loads, read_json
from .config import Config
from .engine import Engine
from .gitstore import GitStore
from .provider import OpenAIProvider
from .queue import enqueue_github
from .source import SourceClient
from .state import State
from .validation import export, validate_repository


def env_bool(name, default=False):
    raw = os.environ.get(name,str(default)).lower()
    if raw not in ('true','false'):
        raise ContractError(f'{name} must be true or false')
    return raw == 'true'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    commands = parser.add_subparsers(dest='command',required=True)
    validation = commands.add_parser('validate')
    validation.add_argument('--recognize-human-edits',action='store_true')
    commands.add_parser('derive')
    commands.add_parser('enqueue-env')
    discover = commands.add_parser('discover')
    discover.add_argument('--check-only',action='store_true')
    worker = commands.add_parser('tick')
    worker.add_argument('--publish',action='store_true')
    cancel = commands.add_parser('cancel')
    cancel.add_argument('--campaign',required=True)
    cancel.add_argument('--publish',action='store_true')
    resolve = commands.add_parser('resolve-absent')
    resolve.add_argument('--batch',required=True)
    resolve.add_argument('--confirmed-no-remote-batch',action='store_true',required=True)
    resolve.add_argument('--publish',action='store_true')
    output = commands.add_parser('export')
    output.add_argument('--output',type=Path,required=True)
    output.add_argument('--source-manifest',type=Path,required=True)
    output.add_argument('--source-revision',required=True)
    output.add_argument('--base',default='/')
    try:
        args = parser.parse_args(argv)
        config = Config(args.root.resolve())
        state = State(config.root)
        if args.command == 'validate':
            if args.recognize_human_edits:
                before = json.dumps(state.records(),sort_keys=True)
                state.sync_human_reviews(GitStore(config.root))
                if before != json.dumps(state.records(),sort_keys=True):
                    state.derive(config)
            result = validate_repository(config)
        elif args.command == 'derive':
            result = state.derive(config)
        elif args.command == 'enqueue-env':
            if os.environ.get('GITHUB_REF') != 'refs/heads/main':
                raise ContractError('Manual paid-work requests must run from main')
            selector = os.environ.get('INPUT_ISSUES','').strip() or os.environ.get('INPUT_ISSUE_SELECTION','next')
            if selector == 'custom':
                raise ContractError('Provide issue UUIDs/source IDs in the issues field')
            request = {'id':'gh-'+os.environ['GITHUB_RUN_ID'],
                       'operation':os.environ['TRANSLATION_OPERATION'],
                       'languages':os.environ.get('INPUT_LANGUAGES','').strip() or os.environ.get('INPUT_LANGUAGE','all'),
                       'issues':selector,'model':os.environ.get('INPUT_MODEL') or config.runtime['default_model'],
                       'review_model':os.environ.get('INPUT_REVIEW_MODEL') or config.runtime['default_review_model'],
                       'budget_usd':os.environ.get('INPUT_BUDGET_USD','5'),
                       'dry_run':env_bool('INPUT_DRY_RUN',True),'retry_failed':env_bool('INPUT_RETRY_FAILED'),
                       'requested_by':os.environ.get('GITHUB_ACTOR')}
            result = enqueue_github(config,request,os.environ['GITHUB_REPOSITORY'],os.environ.get('GH_TOKEN',''))
        elif args.command == 'discover':
            source = SourceClient(config)
            result = source.discover()
            if not args.check_only:
                state.write('state/source.json',result); state.derive(config)
            result = {'revision':result['revision'],'issues':len(result['issues']),'articles':len(result['articles'])}
        elif args.command == 'export':
            result = export(config,args.output,read_json(args.source_manifest),args.source_revision,args.base)
        else:
            store = GitStore(config.root,publish=args.publish)
            provider = OpenAIProvider() if os.environ.get('OPENAI_API_KEY') else None
            engine = Engine(config,SourceClient(config),provider,store)
            if args.command == 'tick':
                engine.tick()
                result = validate_repository(config)
                result['api_key_configured'] = bool(provider)
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
