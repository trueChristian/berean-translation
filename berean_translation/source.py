"""Read-only, commit-pinned access to the authoritative format-2.0 archive."""
from __future__ import annotations
import copy
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from .common import ContractError, digest, json_hash, loads, read_json, safe_path, uuid, write_json
from .html import Fragment


class SourceClient:
    def __init__(self, config, fetch=None):
        self.config = config
        self.repository = config.runtime['source_repository']
        self.fetch = fetch or self._get
        self.revision = None
        self.index = None
        self.catalogue = None
        self.manifest = None

    @staticmethod
    def _get(url: str) -> bytes:
        headers = {'User-Agent': 'berean-translation/1.0', 'Accept': 'application/vnd.github+json'}
        token = os.environ.get('SOURCE_GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
        if token and url.startswith('https://api.github.com/'):
            headers['Authorization'] = 'Bearer ' + token
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers=headers), timeout=60) as response:
                    data = response.read(32000001)
                if len(data) > 32000000:
                    raise ContractError('Source resource exceeds the 32 MB safety limit')
                return data
            except HTTPError as exc:
                if exc.code not in (429,500,502,503,504) or attempt == 2:
                    raise ContractError(f'Source HTTP error {exc.code}; no source changes accepted') from exc
            except (URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise ContractError('Source network request failed; previous source state is retained') from exc
            time.sleep(attempt + 1)
        raise AssertionError('Unreachable')

    def raw(self, revision: str, path: str) -> bytes:
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ContractError('Source revision must be a complete commit SHA')
        if path not in ('index.json','catalogue.json','manifest.json') and not re.fullmatch(
                r'content/articles/[a-f0-9-]{36}\.html', path):
            raise ContractError(f'Disallowed source file: {path}')
        cache = safe_path(self.config.root, f'.cache/source/{revision}/{path}')
        if cache.exists():
            return cache.read_bytes()
        result = self.fetch(f'https://raw.githubusercontent.com/{self.repository}/{revision}/{path}')
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(result)
        return result

    def discover(self) -> dict:
        ref = quote(self.config.runtime['source_branch'], safe='')
        commit = loads(self.fetch(f'https://api.github.com/repos/{self.repository}/commits/{ref}'))
        revision = commit.get('sha','')
        index_raw = self.raw(revision, 'index.json')
        catalogue_raw = self.raw(revision, 'catalogue.json')
        index = loads(index_raw)
        catalogue = loads(catalogue_raw)
        manifest = loads(self.raw(revision, 'manifest.json'))
        if any(item.get('format_version') != '2.0' for item in (index,catalogue,manifest)):
            raise ContractError('Unsupported core archive contract; format 2.0 is required')
        if manifest.get('index_sha256') != digest(index_raw) or manifest.get('catalogue_sha256') != digest(catalogue_raw):
            raise ContractError('Core index/catalogue hashes do not match the manifest')
        issues = {}
        for issue in catalogue['issues']:
            identity = uuid(issue['id'])
            if identity in issues:
                raise ContractError('Duplicate source issue')
            issues[identity] = {k: copy.deepcopy(issue.get(k)) for k in ('id','slug','source_id','date','publication')}
        articles = {}
        for article in index['articles']:
            identity = uuid(article['id'])
            if identity in articles or article['issue_id'] not in issues:
                raise ContractError('Duplicate article or missing source issue')
            if article.get('language') != 'en' or article['html']['repository_path'] != f'content/articles/{identity}.html':
                raise ContractError('Source language or article path is invalid')
            rights = article.get('rights', {})
            if rights.get('status') != 'eligible' or rights.get('article_specific_permission_notice_detected') is not False:
                raise ContractError('The included source array contains an ineligible article')
            fp = manifest['articles'].get(identity)
            if not isinstance(fp, dict) or any(not re.fullmatch(r'[0-9a-f]{64}', fp.get(k,'')) for k in (
                    'html_sha256','text_sha256','structure_sha256','translation_metadata_sha256')):
                raise ContractError('Source article fingerprints are missing or invalid')
            articles[identity] = {'id':identity,'issue_id':article['issue_id'],'sequence':article['sequence'],
                                  'title':article.get('title'),'fingerprints':copy.deepcopy(fp),
                                  'translation_key':translation_key(fp)}
        if set(articles) != set(manifest['articles']):
            raise ContractError('Manifest and index article inventories differ')
        self.revision, self.index, self.catalogue, self.manifest = revision,index,catalogue,manifest
        return {'format_version':'1.0','repository':self.repository,'revision':revision,
                'content_sha256':manifest.get('content_sha256'),'issues':list(issues.values()),'articles':articles}

    def snapshot(self, article_id: str) -> dict:
        if self.index is None:
            raise ContractError('Discover the current source before taking a snapshot')
        article = next((a for a in self.index['articles'] if a['id'] == article_id), None)
        if not article:
            raise ContractError('Article is not present in the eligible source index')
        raw = self.raw(self.revision, article['html']['repository_path'])
        if len(raw) > self.config.runtime['max_source_html_bytes']:
            raise ContractError('Article exceeds the configured source size; no paid request was made')
        fp = self.manifest['articles'][article_id]
        if digest(raw) != fp['html_sha256']:
            raise ContractError('Source HTML hash differs from the pinned source manifest')
        text = raw.decode('utf-8')
        parsed = Fragment(text, article_id)
        if [i['src'] for i in parsed.images] != [i['public_path'] for i in article['images']]:
            raise ContractError('Source image references disagree with their index metadata')
        snapshot = {'repository':self.repository,'revision':self.revision,'fingerprints':copy.deepcopy(fp),
                    'translation_key':translation_key(fp),'html':text,
                    'article':{k:copy.deepcopy(article.get(k)) for k in (
                        'id','issue_id','sequence','title','subtitle','section','byline','categories','topics',
                        'series','images','rights','source_pages')}}
        return snapshot


def translation_key(fingerprints: dict) -> str:
    # Image pixel replacements and unrelated catalogue edits do not invalidate language work.
    return json_hash({k:fingerprints[k] for k in ('text_sha256','structure_sha256','translation_metadata_sha256')})
