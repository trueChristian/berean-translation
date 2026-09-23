"""Regression coverage for actual archive markup and whole-checkout CI validation."""
from __future__ import annotations
import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from berean_translation.check_source import main, validate_checkout
from berean_translation.common import ContractError
from berean_translation.html import Fragment, split_article, validate_translation
from support import A, setup


class SourceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.state, self.upstream, *_ = setup(self.root)
        self.upstream.client.discover()
        self.source = self.upstream.client.snapshot(A)
        self.source['html'] = self.source['html'].replace('</article>',
            '<aside class="sidebar"><p>A source sidebar.</p></aside>'
            '<table><thead><tr><th scope="col">Heading</th></tr></thead>'
            '<tbody><tr><th scope="row">Row</th></tr></tbody></table>'
            '<footer>Original author sign-off.</footer></article>')
        self.candidate = {key: self.source['article'].get(key) for key in ('title', 'subtitle', 'section')}
        self.candidate['html'] = self.source['html']

    def test_source_sidebars_footers_and_table_scopes_are_preserved(self):
        result = validate_translation(self.source, self.candidate)
        self.assertIn('source sidebar', result.text)
        self.assertEqual(split_article(self.source['html']), (self.source['html'], ''))

    def test_source_sidebar_is_not_the_external_translation_notice(self):
        external = '<aside data-translation-notice="ai">AI notice</aside>'
        body, tail = split_article(self.source['html'] + '\n' + external)
        self.assertIn('<aside class="sidebar">', body)
        self.assertEqual(tail, external)
        with self.assertRaises(ContractError):
            Fragment(body + external, A)

    def test_sidebar_footer_or_scope_cannot_be_silently_removed_or_changed(self):
        for before, after in (('A source sidebar.', ''), ('Original author sign-off.', ''),
                              ('scope="col"', 'scope="row"'),
                              ('class="sidebar"', 'class="sidebar" onclick="alert(1)"')):
            candidate = copy.deepcopy(self.candidate)
            candidate['html'] = candidate['html'].replace(before, after)
            with self.subTest(before=before), self.assertRaises(ContractError):
                validate_translation(self.source, candidate)
        with self.assertRaises(ContractError):
            Fragment(self.source['html'].replace('scope="col"', 'scope="invalid"'), A)

    def checkout(self):
        directory = self.root / 'english'
        directory.mkdir()
        for relative, text in self.upstream.contents.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode('utf-8'))
        def git(*args):
            return subprocess.check_output(['git', '-C', str(directory), *args],
                                           stderr=subprocess.PIPE, text=True).strip()
        git('init', '-b', 'main')
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'fixture')
        return directory, git('rev-parse', 'HEAD'), git

    def test_whole_checkout_checks_every_snapshot_without_openai(self):
        directory, revision, _ = self.checkout()
        with patch('berean_translation.provider.OpenAIProvider') as provider:
            result = validate_checkout(self.config, directory, revision)
            self.assertEqual(result['article_snapshots_verified'], 2)
            self.assertEqual(result['revision'], revision)
            self.assertEqual(main(['--root', str(self.root), '--checkout', str(directory),
                                   '--revision', revision]), 0)
            provider.assert_not_called()
        self.assertEqual(self.config.runtime['source_branch'], 'main')
        self.assertFalse((self.root / '.cache/source' / revision).exists())

    def test_wrong_revision_and_dirty_source_are_rejected(self):
        directory, revision, _ = self.checkout()
        with self.assertRaises(ContractError):
            validate_checkout(self.config, directory, 'f' * 40)
        (directory / 'index.json').write_text('{}')
        with self.assertRaises(ContractError):
            validate_checkout(self.config, directory, revision)

    def test_committed_tampering_cannot_hide_behind_a_discovery_cache(self):
        directory, _, git = self.checkout()
        target = directory / f'content/articles/{A}.html'
        target.write_text(target.read_text().replace('Faith', 'Changed'))
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'tampering')
        revision = git('rev-parse', 'HEAD')
        stale_cache = self.root / f'.cache/source/{revision}/content/articles/{A}.html'
        stale_cache.parent.mkdir(parents=True)
        stale_cache.write_text(self.upstream.contents[f'content/articles/{A}.html'])
        with self.assertRaisesRegex(ContractError, 'Source HTML hash'):
            validate_checkout(self.config, directory, revision)
