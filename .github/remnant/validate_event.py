#!/usr/bin/env python3
"""Validate notification metadata, never choose executable refs from a payload."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

SITE_REPOSITORY = 'trueChristian/remnant.truechristian.church'
SOURCE_REPOSITORIES = frozenset({'trueChristian/berean-voice', 'trueChristian/berean-translation'})
EVENT_TYPE = 'remnant-content-updated'
SHA = re.compile(r'[0-9a-f]{40}\Z')
FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')


def validate_payload(payload: object) -> dict:
    expected = {'schema', 'repository', 'revision', 'display_fingerprint'}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError('Dispatch payload must contain only the four versioned contract fields')
    if type(payload['schema']) is not int or payload['schema'] != 1:
        raise ValueError('Unsupported dispatch schema')
    if not isinstance(payload['repository'], str) or payload['repository'] not in SOURCE_REPOSITORIES:
        raise ValueError('Unrecognized source repository')
    if not isinstance(payload['revision'], str) or not SHA.fullmatch(payload['revision']):
        raise ValueError('Revision must be a lowercase full 40-character commit SHA')
    if not isinstance(payload['display_fingerprint'], str) or not FINGERPRINT.fullmatch(payload['display_fingerprint']):
        raise ValueError('Display fingerprint must be SHA-256')
    return dict(payload)


def validate_event(event_name: str, event: object, ref: str, repository: str) -> dict:
    if repository != SITE_REPOSITORY:
        raise ValueError('This workflow is restricted to the website repository')
    if not isinstance(event, dict):
        raise ValueError('Event must be an object')
    if event_name == 'pull_request':
        return {'event': event_name, 'production': False, 'notification': None}
    if event_name not in {'push', 'workflow_dispatch', 'repository_dispatch'} or ref != 'refs/heads/main':
        raise ValueError('Production builds require a supported event on trusted main')
    payload = None
    if event_name == 'repository_dispatch':
        if event.get('action') != EVENT_TYPE:
            raise ValueError('Unsupported repository_dispatch type')
        payload = validate_payload(event.get('client_payload'))
    if event_name == 'push' and (event.get('deleted') or event.get('ref') != 'refs/heads/main'):
        raise ValueError('Only a non-deletion main push is supported')
    return {'event': event_name, 'production': True, 'notification': payload}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', default=os.environ.get('GITHUB_EVENT_PATH'))
    parser.add_argument('--output', default='.build/event-report.json')
    args = parser.parse_args()
    if not args.event:
        parser.error('GitHub event path is required')
    path = Path(args.event)
    if path.stat().st_size > 2_000_000:
        raise ValueError('Event exceeds bounded input limit')
    result = validate_event(os.environ.get('GITHUB_EVENT_NAME', ''), json.loads(path.read_text()),
                            os.environ.get('GITHUB_REF', ''), os.environ.get('GITHUB_REPOSITORY', ''))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
