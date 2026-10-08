"""Read-only ownership checks over real disposable Git history, entirely offline."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from berean_translation.common import ContractError
from berean_translation.gitstore import GitStore, NoHumanEditEvidence


class GitStoreComponentOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Network forbidden in ownership tests'))
            guard.start()
            self.addCleanup(guard.stop)
        self.new_repo()

    def new_repo(self):
        self.root = self.parent / str(len(list(self.parent.iterdir())))
        self.root.mkdir()
        self.git = GitStore(self.root, publish=False)
        self.git.git('init', '-q')
        (self.root / 'baseline.txt').write_text('Disposable ownership baseline.')
        self.git.git('add', '--', 'baseline.txt')
        self.commit('github-actions[bot]', 'Collector baseline')
        self.relative = 'content/deu/articles/fixture.html'
        self.path = self.root / self.relative
        self.path.parent.mkdir(parents=True)

    def commit(self, author, message):
        self.git.git('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
            '-c', 'user.name=' + author, '-c', 'user.email=reviewer@example.test',
            'commit', '-qm', message)

    def owned_path(self, author='Human Reviewer', text='Human authored words.'):
        self.path.write_text(text)
        self.git.git('add', '--', self.relative)
        self.commit(author, 'Record editorial ownership')

    def assert_ambiguous(self):
        with self.assertRaises(ContractError) as caught:
            self.git.human_edit_evidence(self.relative, all_history=True)
        self.assertNotIsInstance(caught.exception, NoHumanEditEvidence)

    def test_missing_and_genuinely_untracked_paths_have_explicit_no_human_evidence(self):
        with self.assertRaises(NoHumanEditEvidence):
            self.git.human_edit_evidence(self.relative, all_history=True)
        self.path.write_text('Untracked collector bytes.')
        with self.assertRaises(NoHumanEditEvidence):
            self.git.human_edit_evidence(self.relative, all_history=True)

    def test_all_bot_history_has_explicit_no_human_evidence(self):
        self.owned_path('github-actions[bot]', 'First bot output.')
        self.owned_path('github-actions[bot]', 'Second bot output.')
        with self.assertRaises(NoHumanEditEvidence):
            self.git.human_edit_evidence(self.relative, all_history=True)

    def test_clean_human_commit_returns_actual_git_provenance(self):
        self.owned_path()
        expected = self.git.git('rev-parse', 'HEAD').stdout.strip()
        evidence = self.git.human_edit_evidence(self.relative, all_history=True)
        self.assertEqual(evidence['commit'], expected)
        self.assertEqual(evidence['author'], 'Human Reviewer')
        self.assertEqual(evidence['email'], 'reviewer@example.test')
        self.assertTrue(evidence['time'])
        self.assertEqual(self.git.human_edit_evidence(self.relative), evidence)

    def test_all_history_preserves_human_ownership_after_a_later_bot_commit(self):
        self.owned_path()
        original = self.git.human_edit_evidence(self.relative)
        self.owned_path('github-actions[bot]', 'Bot changed bytes later.')
        self.assertEqual(self.git.human_edit_evidence(self.relative, all_history=True), original)
        # The existing latest-commit API keeps its historical default contract.
        with self.assertRaises(NoHumanEditEvidence): self.git.human_edit_evidence(self.relative)

    def test_tracked_modified_staged_and_deleted_paths_are_ambiguous_not_no_human(self):
        for variant in ('modified', 'staged', 'deleted', 'staged_deletion'):
            with self.subTest(variant=variant):
                self.new_repo()
                self.owned_path()
                if variant in ('modified', 'staged'): self.path.write_text('Different working bytes.')
                else: self.path.unlink()
                if variant in ('staged', 'staged_deletion'):
                    self.git.git('add', '-A', '--', self.relative)
                self.assert_ambiguous()

    def test_committed_human_deletion_retains_authority_when_destination_is_absent(self):
        self.owned_path()
        self.path.unlink()
        self.git.git('add', '-u', '--', self.relative)
        self.commit('Human Reviewer', 'Remove human article')
        self.assertFalse(self.path.exists())
        evidence = self.git.human_edit_evidence(self.relative, all_history=True)
        self.assertEqual(evidence['author'], 'Human Reviewer')
        self.assertEqual(evidence['commit'], self.git.git('rev-parse', 'HEAD').stdout.strip())

    def test_untracked_recreation_cannot_erase_a_committed_human_deletion(self):
        self.owned_path()
        self.path.unlink()
        self.git.git('add', '-u', '--', self.relative)
        self.commit('Human Reviewer', 'Remove human article')
        self.path.write_text('New untracked bytes at the same historical path.')
        self.assertEqual(self.git.human_edit_evidence(self.relative, all_history=True)['author'],
                         'Human Reviewer')

    def test_unknown_lookup_errors_are_not_converted_to_no_human_evidence(self):
        for failure in (ContractError('Unexpected contract failure'), OSError('Git read failed')):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(self.git, 'git', side_effect=failure):
                    with self.assertRaises(type(failure)) as caught:
                        self.git.human_edit_evidence(self.relative, all_history=True)
                    self.assertNotIsInstance(caught.exception, NoHumanEditEvidence)

    def test_malformed_commit_history_is_an_ambiguous_hold(self):
        def result(*args, **kwargs):
            return SimpleNamespace(stdout='' if args[0] == 'status' else 'incomplete provenance\n')
        with patch.object(self.git, 'git', side_effect=result): self.assert_ambiguous()
