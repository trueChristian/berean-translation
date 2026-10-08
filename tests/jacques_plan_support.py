"""Disposable Jacques fixtures. Synthetic chapters are never readiness evidence.

Only the existing, hash-verified English article is reused. All chapter envelopes
and all Batch results are explicitly synthetic, offline test data. No complete
real target Bible chapter is stored here or fetched by these helpers.
"""
from __future__ import annotations

import copy
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from berean_translation import manual_admission
from berean_translation.common import json_hash, read_json
from berean_translation.gitstore import NoHumanEditEvidence
from berean_translation.scripture_provider import GetBibleMCP
from support import REPO_ROOT, queue, setup

CAMPAIGN = 'gh-37439892859'
ARTICLE = '069fc797-01e4-44d3-8b71-1c07f0821965'
SOURCE_HASH = 'ad31a29abcaa9fc4133d05837032668a19ddc92690fa0c75fc335b4adf6d6cb3'
LANGUAGES = ('afr', 'deu', 'ell', 'fra', 'heb', 'ita', 'kor', 'nld', 'nob', 'por', 'rus', 'swe')
CHAPTER_LENGTHS = {(45, 2): 29, (40, 5): 48, (44, 24): 27,
                   (60, 3): 22, (45, 7): 25, (41, 16): 20}
BOOK_NAMES = {45: 'Romans', 40: 'Matthew', 44: 'Acts', 60: '1 Peter', 41: 'Mark'}
ROMANS_2_4 = ('Or despisest thou the riches of his goodness and forbearance and '
              'longsuffering; not knowing that the goodness of God leadeth thee to repentance?')


class FrozenJacquesSource:
    """Fake read-only upstream preserving the exact existing source snapshot."""
    def __init__(self):
        self.source = read_json(REPO_ROOT / f'state/sources/{SOURCE_HASH}.json')
        assert json_hash(self.source) == SOURCE_HASH
        source_index = read_json(REPO_ROOT / 'state/source.json')
        self.inventory = {key: copy.deepcopy(value) for key, value in source_index.items()
                          if key not in ('articles', 'issues')}
        self.inventory['revision'] = self.source['revision']
        self.inventory['articles'] = {ARTICLE: copy.deepcopy(source_index['articles'][ARTICLE])}
        self.inventory['issues'] = [copy.deepcopy(next(issue for issue in source_index['issues']
            if issue['id'] == self.source['article']['issue_id']))]
        self.discover_calls = 0

    def discover(self):
        self.discover_calls += 1
        return copy.deepcopy(self.inventory)

    def snapshot(self, article_id):
        assert article_id == ARTICLE
        return copy.deepcopy(self.source)


class SyntheticCompleteChapters:
    """Complete-shaped fake envelopes, never genuine approved-provider receipts."""
    def __init__(self):
        self.calls = []
        self.mutations = {}
        self.failures = {}
        self.adapter = GetBibleMCP(self.call)

    def call(self, name, args):
        assert name == 'get_scripture'
        identity = f'{args["translation"]}/{args["book"]}/{args["chapter"]}'
        self.calls.append(identity)
        if identity in self.failures:
            raise self.failures[identity]
        book, chapter, edition = args['book'], args['chapter'], args['translation']
        timestamp = datetime.now(timezone.utc)
        verses = [{'chapter': chapter, 'verse': number,
                   'name': f'{BOOK_NAMES[book]} {chapter}:{number}',
                   # Tiny unmistakable fake text keeps positive controls below
                   # the unchanged whole-source association-work safety bound.
                   'text': 'SYNTHETIC.'}
                  for number in range(1, CHAPTER_LENGTHS[book, chapter] + 1)]
        if (book, chapter) == (45, 2):
            # Deliberately reuse this English string in fake target editions.
            # This is structural simulator data, not a target-language Bible.
            verses[3]['text'] = ROMANS_2_4
        result = {'scope': {'kind': 'chapter', **args},
                  'data': {'translation': 'SYNTHETIC OFFLINE FIXTURE, NOT SCRIPTURE EVIDENCE',
                           'abbreviation': edition, 'lang': 'en', 'language': 'Synthetic',
                           'book_nr': book, 'book_name': BOOK_NAMES[book], 'chapter': chapter,
                           'name': f'{BOOK_NAMES[book]} {chapter}', 'verses': verses},
                  'source': {'url': f'https://api.getbible.net/v2/{edition}/{book}/{chapter}.json',
                             'api_version': 'v2', 'status_code': 200,
                             'fetched_at': timestamp.isoformat()},
                  'hash': 'f' * 40, 'consistency_checked': True,
                  'cache': {'cacheable': True,
                            'expires_at': (timestamp + timedelta(days=1)).isoformat()}}
        mutation = self.mutations.get(identity)
        if mutation:
            mutation(result)
        return {'structuredContent': result, 'isError': False, 'resultType': 'complete'}

    def chapter(self, edition, book, chapter):
        return self.adapter.chapter(edition, book, chapter)


def fixture(parent: Path, *, languages=LANGUAGES, gate=False, budget=10):
    root = parent / 'repository'
    root.mkdir()
    config, state, _, provider, git, engine = setup(root, review_contract_version=2)
    shutil.copytree(REPO_ROOT / 'data', root / 'data')
    shutil.copytree(REPO_ROOT / 'docs/third-party', root / 'docs/third-party')
    config.runtime['scripture_quotes_enabled'] = True
    config.runtime['scripture_components_runtime_enabled'] = gate
    source = FrozenJacquesSource()
    chapters = SyntheticCompleteChapters()
    engine.source_client = source
    engine.scripture_provider = chapters
    git.bot = True
    def no_human_edits(*args, **kwargs):
        raise NoHumanEditEvidence('Untracked/collector-owned disposable test fixture')
    git.human_edit_evidence = no_human_edits
    engine.discover()
    request = queue(state, identity=CAMPAIGN, languages=','.join(languages), budget_usd=budget,
                    model='gpt-6-luna', review_model='gpt-6-luna')
    campaign = engine.accept_request(request)
    ledger = state.read(manual_admission.path(CAMPAIGN))
    assert not campaign['tasks']
    assert all(entry['status'] == 'attention' for entry in ledger['entries'].values())
    assert not chapters.calls
    return SimpleNamespace(root=root, config=config, state=state, source=source,
        provider=provider, git=git, engine=engine, chapters=chapters, request=request,
        campaign=campaign, ledger=ledger)


def authorization(run_id='123456'):
    repository = 'fixture-owner/fixture-repository'
    return {'kind': 'github_workflow_dispatch', 'repository': repository,
            'workflow_ref': f'{repository}/.github/workflows/ai-scripture-components.yml@refs/heads/main',
            'run_id': run_id, 'actor': 'fixture-owner'}
