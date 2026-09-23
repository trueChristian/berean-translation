"""Read English main/index and compute change tracking in the translation runtime."""
from __future__ import annotations
import copy
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from .common import ContractError, digest, json_hash, loads, safe_path, uuid
from .fingerprints import article_fingerprints
from .html import Fragment


class SourceClient:
    def __init__(self, config, fetch=None, checkout=None):
        self.config = config
        self.repository = config.runtime['source_repository']
        self.fetch = fetch or self._get
        configured = checkout or (os.environ.get('BEREAN_SOURCE_CHECKOUT') if fetch is None else None)
        self.checkout = Path(configured).resolve() if configured else None
        self.revision = None
        self.index = None
        self.catalogue = None
        self.snapshots = {}

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

    def checkout_revision(self) -> str:
        """Freeze one main checkout per scan, without a user-managed revision pin."""
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.checkout), *args],
                                           stderr=subprocess.PIPE, text=True).strip()
        try:
            revision = git('rev-parse', 'HEAD')
            dirty = git('status', '--porcelain', '--untracked-files=all', '--',
                        'index.json', 'catalogue.json', 'content/articles')
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ContractError('A readable Git source checkout is required') from exc
        if dirty or not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ContractError('Source checkout must be clean before scanning; commit the English edits')
        selected = self.config.runtime['source_branch']
        if re.fullmatch(r'[0-9a-f]{40}', selected) and selected != revision:
            raise ContractError('Source checkout does not match the internally selected revision')
        return revision

    def raw(self, revision: str, path: str) -> bytes:
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ContractError('Source revision must be a complete commit SHA')
        if path not in ('index.json', 'catalogue.json') and not re.fullmatch(
                r'content/articles/[a-f0-9-]{36}\.html', path):
            raise ContractError(f'Disallowed source file: {path}')
        if self.checkout:
            return safe_path(self.checkout, path).read_bytes()
        # Each scan reads real source bytes, not an independently editable hash
        # cache. Production uses one sparse checkout rather than N HTTP calls.
        return self.fetch(f'https://raw.githubusercontent.com/{self.repository}/{revision}/{path}')

    def discover(self) -> dict:
        if self.checkout:
            revision = self.checkout_revision()
        else:
            ref = quote(self.config.runtime['source_branch'], safe='')
            commit = loads(self.fetch(f'https://api.github.com/repos/{self.repository}/commits/{ref}'))
            revision = commit.get('sha', '')
        index_raw = self.raw(revision, 'index.json')
        catalogue_raw = self.raw(revision, 'catalogue.json')
        index, catalogue = loads(index_raw), loads(catalogue_raw)
        if any(item.get('format_version') != '2.0' for item in (index, catalogue)):
            raise ContractError('Unsupported core archive contract; format 2.0 is required')
        if not isinstance(index.get('articles'), list) or not isinstance(catalogue.get('issues'), list):
            raise ContractError('Source index articles and catalogue issues must be arrays')
        issues = {}
        for issue in catalogue['issues']:
            identity = uuid(issue['id'])
            if identity in issues:
                raise ContractError('Duplicate source issue')
            issues[identity] = {k: copy.deepcopy(issue.get(k)) for k in ('id','slug','source_id','date','publication')}
        seen, sequences = set(issues), set()
        for article in index['articles']:
            identity = uuid(article['id'])
            if identity in seen or article['issue_id'] not in issues:
                raise ContractError('Duplicate article or missing source issue')
            seen.add(identity)
            if article.get('language') != 'en' or article['html']['repository_path'] != f'content/articles/{identity}.html':
                raise ContractError('Source language or article path is invalid')
            sequence = article.get('sequence')
            if type(sequence) is not int or sequence < 1 or (article['issue_id'], sequence) in sequences:
                raise ContractError('Invalid or duplicate issue article sequence')
            sequences.add((article['issue_id'], sequence))
            rights = article.get('rights', {})
            if rights.get('status') != 'eligible' or rights.get('article_specific_permission_notice_detected') is not False:
                raise ContractError('The included source array contains an ineligible article')

        def inspect(article):
            identity = article['id']
            try:
                raw = self.raw(revision, article['html']['repository_path'])
                if len(raw) > self.config.runtime['max_source_html_bytes']:
                    raise ContractError('Article exceeds the configured source size; no paid request was made')
                text = raw.decode('utf-8')
                parsed = Fragment(text, identity)
                if [i['src'] for i in parsed.images] != [i['public_path'] for i in article['images']]:
                    raise ContractError('Source image references disagree with their index metadata')
                fp = article_fingerprints(article, raw)
                snapshot = {'repository': self.repository, 'revision': revision, 'fingerprints': fp,
                            'translation_key': translation_key(fp), 'html': text,
                            'article': {k: copy.deepcopy(article.get(k)) for k in (
                                'id','issue_id','sequence','title','subtitle','section','byline','categories','topics',
                                'series','images','rights','source_pages')}}
                item = {'id': identity, 'issue_id': article['issue_id'], 'sequence': article['sequence'],
                        'title': article.get('title'), 'fingerprints': copy.deepcopy(fp),
                        'translation_key': snapshot['translation_key']}
                return identity, item, snapshot
            except (ContractError, OSError, ValueError) as exc:
                raise ContractError(f'Source article {identity}: {exc}') from exc

        # Bounded concurrency is only a transport optimization. Publication is
        # still atomic: an invalid scan never replaces the last good inventory.
        with ThreadPoolExecutor(max_workers=8) as pool:
            inspected = list(pool.map(inspect, index['articles']))
        articles = {identity: item for identity, item, _ in inspected}
        snapshots = {identity: snapshot for identity, _, snapshot in inspected}
        inventory = {'format_version': '1.0', 'fingerprint_origin': 'translation-runtime',
                     'repository': self.repository, 'revision': revision,
                     'content_sha256': json_hash({'index': digest(index_raw), 'catalogue': digest(catalogue_raw),
                                                'articles': articles}),
                     'issues': list(issues.values()), 'articles': articles}
        self.revision, self.index, self.catalogue, self.snapshots = revision, index, catalogue, snapshots
        return inventory

    def snapshot(self, article_id: str) -> dict:
        if self.index is None:
            raise ContractError('Discover the current source before taking a snapshot')
        if article_id not in self.snapshots:
            raise ContractError('Article is not present in the eligible source index')
        return copy.deepcopy(self.snapshots[article_id])


def translation_key(fingerprints: dict) -> str:
    # Image pixel replacements and unrelated catalogue edits do not invalidate language work.
    return json_hash({k: fingerprints[k] for k in ('text_sha256','structure_sha256','translation_metadata_sha256')})
