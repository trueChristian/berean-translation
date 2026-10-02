#!/usr/bin/env python3
"""Bounded source notification and display-only deployment deduplication.

The source integrations copy this module and validate_event.py together. No token
is accepted in arguments, written to disk, included in a URL, or logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from validate_event import EVENT_TYPE, FINGERPRINT, SHA, SITE_REPOSITORY, validate_payload

PRODUCTION_MANIFEST = 'https://remnant.truechristian.church/deployment.json'
DISPATCH_ENDPOINT = f'https://api.github.com/repos/{SITE_REPOSITORY}/dispatches'
PROVENANCE_FILES = {'manifest.json'}
DEPLOYMENT_REPORTS = {'deployment.json', 'build-report.json'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirects are not permitted for notification or deployment metadata')


def request_bytes(request: urllib.request.Request, *, limit: int = 65536) -> tuple[int, bytes]:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError('Response exceeds bounded metadata size')
        return response.status, content


def canonical_display(value):
    """Remove only revision provenance, never notice/review or article data."""
    if isinstance(value, dict):
        return {key: canonical_display(item) for key, item in value.items()
                if key not in {'source_revision', 'translation_revision'}}
    if isinstance(value, list):
        return [canonical_display(item) for item in value]
    return value


def display_fingerprint(directory: Path, *, exported: bool = False) -> str:
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError('Display output must be an existing directory')
    digest = hashlib.sha256()
    included = 0
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Display output must not contain symlinks')
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in (PROVENANCE_FILES if exported else DEPLOYMENT_REPORTS):
            continue
        raw = path.read_bytes()
        if exported and path.suffix == '.json':
            raw = json.dumps(canonical_display(json.loads(raw)), sort_keys=True,
                             ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        name = relative.encode('utf-8')
        # Length-prefixing prevents filename/content boundary ambiguities.
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
        included += 1
    if not included:
        raise ValueError('Refusing to fingerprint an empty display tree')
    return digest.hexdigest()


def publication_fingerprint(exported: Path, repository: str, checkout: Path) -> str:
    fingerprint = display_fingerprint(exported, exported=True)
    if repository != 'trueChristian/berean-translation':
        return fingerprint
    # The supported article exporter omits the registry. A configured locale with
    # zero articles must still wake the website's complete-interface validation.
    registry_path = checkout / 'config/languages.json'
    if (registry_path.is_symlink() or not registry_path.is_file()
            or not registry_path.resolve().is_relative_to(checkout.resolve())
            or registry_path.stat().st_size > 1_000_000):
        raise ValueError('Fresh source language registry is required for notification')
    registry = json.loads(registry_path.read_text())
    if not isinstance(registry, dict) or any(not isinstance(item, dict) for item in registry.values()):
        raise ValueError('Language registry must map folder codes to objects')
    fields = ('name', 'native_name', 'tag', 'dir', 'aliases')
    display = {code: {key: item.get(key) for key in fields} for code, item in registry.items()}
    payload = json.dumps({'version': 1, 'export': fingerprint, 'languages': display},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def durable_revision(checkout: Path) -> str:
    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args],
                                       text=True, stderr=subprocess.PIPE).strip()
    revision = git('rev-parse', 'HEAD')
    if not SHA.fullmatch(revision) or git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Notification requires clean committed source inputs')
    remote = git('ls-remote', '--exit-code', 'origin', 'refs/heads/main').split()
    if remote != [revision, 'refs/heads/main']:
        raise ValueError('Source HEAD is not durably published current origin/main; rerun on main')
    return revision


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def notify(repository: str, checkout: Path, exported: Path, state_path: Path,
           token: str, request=request_bytes) -> dict:
    revision = durable_revision(checkout)
    manifest = json.loads((exported / 'manifest.json').read_text())
    revision_key = 'translation_revision' if repository.endswith('/berean-translation') else 'source_revision'
    if manifest.get(revision_key) != revision or not (exported / 'index.json').is_file():
        raise ValueError('Display export must be freshly validated from published HEAD')
    payload = validate_payload({'schema': 1, 'repository': repository, 'revision': revision,
                                'display_fingerprint': publication_fingerprint(exported, repository, checkout)})
    previous = read_state(state_path)
    if (previous.get('repository') == repository and
            previous.get('display_fingerprint') == payload['display_fingerprint']):
        return {'sent': False, 'reason': 'unchanged-display', **payload}
    if not token:
        raise ValueError('Owner setup required: REMNANT_DISPATCH_TOKEN is not configured')
    body = json.dumps({'event_type': EVENT_TYPE, 'client_payload': payload}).encode('utf-8')
    req = urllib.request.Request(DISPATCH_ENDPOINT, data=body, method='POST', headers={
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'User-Agent': 'remnant-publication-hook/1',
    })
    try:
        status, _ = request(req)
    except urllib.error.HTTPError as error:
        # HTTPError is also a URLError. Report only its numeric status, never
        # response bodies, request headers, URLs, or the original exception.
        advice = {
            401: 'Check that REMNANT_DISPATCH_TOKEN is valid and has not expired.',
            403: 'Check token access to the website repository and Contents: write permission.',
            404: 'Check token access to the website repository and Contents: write permission.',
            429: 'GitHub rate limited this request; retry the trusted source workflow later.',
        }.get(error.code, 'Retry the trusted source workflow after resolving the GitHub API error.')
        raise RuntimeError(f'Dispatch returned HTTP {error.code}; no success marker saved. {advice}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # Do not include response bodies, request headers, or exception URLs.
        raise RuntimeError('Dispatch delivery failed; no success marker saved. Rerun trusted source workflow.') from None
    if status != 204:
        raise RuntimeError(f'Dispatch returned HTTP {status}; no success marker saved')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, sort_keys=True) + '\n')
    return {'sent': True, 'reason': 'display-changed', **payload}


def live_deployment(request=request_bytes) -> dict | None:
    try:
        status, raw = request(urllib.request.Request(PRODUCTION_MANIFEST, headers={
            'User-Agent': 'remnant-build/1', 'Cache-Control': 'no-cache', 'Accept': 'application/json'}))
        data = json.loads(raw)
        value = data.get('display_fingerprint') if isinstance(data, dict) else None
        if status == 200 and isinstance(data, dict) and data.get('schema') == 1 and isinstance(value, str) and FINGERPRINT.fullmatch(value):
            return data
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        pass
    # Missing metadata, CDN uncertainty, or first deploy must never suppress work.
    return None


def deployment_plan(output: Path, source_report: dict, *, previous: dict | None = None,
                    force: bool = False) -> dict:
    fingerprint = display_fingerprint(output)
    revisions = {'site': source_report['site_revision']}
    revisions.update({key: value['revision'] for key, value in source_report['sources'].items()})
    if set(revisions) != {'site', 'english', 'translations', 'theme'} or any(
            not isinstance(value, str) or not SHA.fullmatch(value)
            for key, value in revisions.items() if not (key == 'translations' and value is None
                and source_report['sources'][key].get('acquisition') == 'unavailable')):
        raise ValueError('A complete fixed-revision report is required')
    translation_status = source_report.get('export', {}).get('translation_status', 'unknown')
    manifest = {'schema': 1, 'display_fingerprint': fingerprint, 'revisions': revisions,
                'translation_status': translation_status}
    (output / 'deployment.json').write_text(json.dumps(manifest, indent=2) + '\n')
    unchanged = (isinstance(previous, dict) and previous.get('schema') == 1
                 and previous.get('display_fingerprint') == fingerprint
                 and previous.get('revisions') == revisions
                 and previous.get('translation_status') == translation_status)
    return {'changed': force or not unchanged,
            'previous_fingerprint': previous.get('display_fingerprint') if previous else None,
            'reason': 'manual-recovery' if force else ('unchanged-display' if unchanged else 'display-changed'),
            **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fingerprint = commands.add_parser('fingerprint')
    fingerprint.add_argument('directory', type=Path)
    fingerprint.add_argument('--exported', action='store_true')
    sender = commands.add_parser('notify')
    sender.add_argument('--repository', required=True)
    sender.add_argument('--checkout', type=Path, default=Path('.'))
    sender.add_argument('--export', dest='exported', type=Path, required=True)
    sender.add_argument('--state', type=Path, required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('--site-output', type=Path, default=Path('dist'))
    plan.add_argument('--source-report', type=Path, default=Path('.build/source-report.json'))
    plan.add_argument('--report', type=Path, default=Path('.build/deployment-plan.json'))
    plan.add_argument('--compare-live', action='store_true')
    plan.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.command == 'fingerprint':
        print(display_fingerprint(args.directory, exported=args.exported))
        return
    if args.command == 'notify':
        if os.environ.get('GITHUB_ACTIONS') == 'true' and (
                os.environ.get('GITHUB_REF') != 'refs/heads/main' or
                os.environ.get('GITHUB_REPOSITORY') != args.repository or
                os.environ.get('GITHUB_EVENT_NAME') in {'pull_request', 'pull_request_target'}):
            raise ValueError('Publication notifications run only in the trusted source main workflow')
        result = notify(args.repository, args.checkout, args.exported, args.state,
                        os.environ.get('REMNANT_DISPATCH_TOKEN', ''))
        output = {'sent': str(result['sent']).lower()}
    else:
        result = deployment_plan(args.site_output, json.loads(args.source_report.read_text()),
                                 previous=live_deployment() if args.compare_live else None,
                                 force=args.force)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n')
        output = {'changed': str(result['changed']).lower()}
    print(json.dumps(result, indent=2))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in output.items():
                stream.write(f'{key}={value}\n')


if __name__ == '__main__':
    main()
