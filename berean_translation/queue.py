"""Persist one immutable manual request without serializing/dropping enqueuers."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from .common import ContractError, canonical, loads, positive_money
from .recovery import recovery_selector


def github_request(method, url, token, data=None):
    body = canonical(data) if data is not None else None
    request = Request(url,data=body,method=method,headers={
        'Authorization':'Bearer '+token,'User-Agent':'berean-translation/1.0',
        'Accept':'application/vnd.github+json','Content-Type':'application/json',
        'X-GitHub-Api-Version':'2022-11-28'})
    with urlopen(request,timeout=60) as response:
        return loads(response.read())


def enqueue_github(config, request, repository: str, token: str, transport=github_request):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):
        raise ContractError('Invalid target repository')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',request['id']):
        raise ContractError('Invalid queue identity')
    component = request.get('operation') == 'scripture-components'
    if component:
        from .scripture_component_plans import validate_request
        validate_request(config, request)
        if request['authorization']['repository'] != repository:
            raise ContractError('Component selection belongs to a different repository')
    elif request.get('operation') == 'repair':
        from .downstream import validate_request
        validate_request(config, request)
        if (request.get('manual_authorization') and
                request['manual_authorization']['repository'] != repository):
            raise ContractError('Manual repair authorization belongs to a different repository')
    elif not recovery_selector(request, config.runtime['max_tasks_per_request']):
        config.select_languages(request['languages'])
    if not component:
        config.model(request['model']); config.model(request['review_model'])
        positive_money(request['budget_usd'],config.runtime['max_campaign_usd'])
    if not token:
        raise ContractError('GITHUB_TOKEN is required to enqueue work')
    path = f'state/queue/{request["id"]}.json'
    url = f'https://api.github.com/repos/{repository}/contents/{path}'
    payload = canonical(request) + b'\n'
    body = {'message':f'queue: {request["operation"]} request {request["id"]}',
            'branch':'main','content':base64.b64encode(payload).decode('ascii')}
    for attempt in range(5):
        try:
            return transport('PUT',url,token,body)
        except HTTPError as exc:
            if exc.code not in (409,422):
                raise ContractError(f'Queue publication rejected by GitHub ({exc.code})') from exc
            try:
                existing = transport('GET',url+'?ref=main',token)
            except HTTPError as lookup:
                if lookup.code != 404:
                    raise
            else:
                # The Contents API omits inline content above 1 MB. Complete
                # component evidence can exceed that threshold; replay its
                # exact Git blob instead of refreshing or replacing the plan.
                if component and existing.get('encoding') == 'none':
                    sha = existing.get('sha')
                    expected_sha = hashlib.sha1(b'blob ' + str(len(payload)).encode('ascii') + b'\0' + payload).hexdigest()
                    if sha != expected_sha or existing.get('size') != len(payload):
                        raise ContractError('Existing component queue blob differs from the immutable request')
                    existing = transport('GET', f'https://api.github.com/repos/{repository}/git/blobs/{sha}', token)
                    content = existing.get('content')
                    if (existing.get('sha') != sha or existing.get('encoding') != 'base64'
                            or existing.get('size') != len(payload) or not isinstance(content, str)
                            or len(content) > 2 * len(payload) + 128):
                        raise ContractError('Existing component queue blob identity changed')
                    if base64.b64decode(content) != payload:
                        raise ContractError('Existing component queue blob bytes changed')
                recorded = loads(base64.b64decode(existing['content']))
                if recorded != request:
                    raise ContractError('Queue identity already exists with different inputs')
                return {'already_queued':True,'path':path}
            time.sleep(attempt+1)
    raise ContractError('Concurrent queue writes did not settle; rerun this same workflow to retry without duplicate work')
