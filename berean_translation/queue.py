"""Persist one immutable manual request without serializing/dropping enqueuers."""
from __future__ import annotations
import base64
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from .common import ContractError, canonical, loads, positive_money


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
    config.select_languages(request['languages'])
    config.model(request['model']); config.model(request['review_model'])
    positive_money(request['budget_usd'],config.runtime['max_campaign_usd'])
    if not token:
        raise ContractError('GITHUB_TOKEN is required to enqueue work')
    path = f'state/queue/{request["id"]}.json'
    url = f'https://api.github.com/repos/{repository}/contents/{path}'
    body = {'message':f'queue: {request["operation"]} request {request["id"]}',
            'branch':'main','content':base64.b64encode(canonical(request)+b'\n').decode('ascii')}
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
                recorded = loads(base64.b64decode(existing['content']))
                if recorded != request:
                    raise ContractError('Queue identity already exists with different inputs')
                return {'already_queued':True,'path':path}
            time.sleep(attempt+1)
    raise ContractError('Concurrent queue writes did not settle; rerun this same workflow to retry without duplicate work')
