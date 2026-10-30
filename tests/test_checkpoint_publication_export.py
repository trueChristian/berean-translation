"""Intermediate publication checkpoints remain exportable during paid work.

Provider calls are simulated; the Git integration uses only a temporary local
bare repository. Exports use the same accepted-publication contract as Remnant.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, read_json, write_json
from berean_translation.engine import Engine
from berean_translation.gitstore import BOT_IDENTITY, GitStore
from berean_translation.spending import POLICY, POLICY_PATH
from berean_translation.validation import export, validate_publications
from support import A, B, drive, queue, setup


class CheckpointPublicationExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.root = self.workspace / 'worker'
        self.root.mkdir()
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root)

    def git_command(self, *args, root=None):
        return subprocess.run(['git', '-C', str(root or self.root), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def enable_local_publication(self):
        remote = self.workspace / 'origin.git'
        self.git_command('init', '--bare', str(remote), root=self.workspace)
        self.git_command('init', '-b', 'main')
        self.git_command('add', '-A')
        self.git_command(*BOT_IDENTITY, 'commit', '-m', 'Offline fixture baseline')
        self.git_command('remote', 'add', 'origin', str(remote))
        self.git_command('push', '-u', 'origin', 'main')
        self.engine.gitstore = GitStore(self.root, publish=True)
        return self.engine.gitstore

    def reports(self):
        return {name: self.state.path(name).read_bytes()
                for name in ('STATUS.md', 'RECOVERY.json', 'state/automatic-status.json')}

    def publish(self):
        queue(self.state)
        drive(self.engine, self.provider)
        self.assertEqual(len(validate_publications(self.config)['articles']), 2)

    def test_clean_intermediate_commit_exports_accepted_article_while_other_review_runs(self):
        # Separate batches let one independent review pass while the other
        # remains genuinely in progress at the fake provider.
        self.config.runtime['max_batch_requests'] = 1
        write_json(self.root / 'config/runtime.json', self.config.runtime)
        store = self.enable_local_publication()
        queue(self.state)
        self.engine.tick()
        self.provider.complete_all()
        self.engine.tick(discover_source=False)
        reviews = [batch['remote_id'] for batch in self.state.batches()
                   if batch['stage'] == 'review1' and batch['status'] == 'submitted']
        self.assertEqual(len(reviews), 2)
        self.provider.complete_all()
        self.provider.batches[reviews[1]]['status'] = 'in_progress'
        before_reports = self.reports()
        checkpoints = []
        original_checkpoint = store.checkpoint

        def observe_committed_publication(message):
            original_checkpoint(message)
            if (message != 'runtime: collect batch results and bounded quality decisions'
                    or not any(record.get('published') for record in self.state.records())):
                return
            self.assertEqual(self.git_command('status', '--porcelain'), '')
            self.assertEqual(self.reports(), before_reports)
            accepted = validate_publications(self.config)
            self.assertEqual(len(accepted['articles']), 1)
            self.assertEqual(sum(task['status'] == 'in_batch' for task in self.state.tasks()), 1)
            head = self.git_command('rev-parse', 'HEAD')
            self.assertEqual(self.git_command('rev-parse', 'origin/main'), head)
            output = self.workspace / 'exports' / head
            manifest = export(self.config, output, self.upstream.client.discover(),
                              self.upstream.revision, translation_revision=head)
            self.assertEqual(manifest['article_count'], 1)
            self.assertEqual(manifest['translation_revision'], head)
            public_ids = {item['id'] for item in read_json(output / 'index.json')['articles']}
            self.assertEqual(public_ids, {accepted['articles'][0]['id']})
            self.assertEqual(self.git_command('status', '--porcelain'), '')
            checkpoints.append(head)

        store.checkpoint = observe_committed_publication
        self.engine.tick(discover_source=False)
        self.assertEqual(len(checkpoints), 1)
        self.assertNotEqual(self.reports(), before_reports)
        self.assertEqual(self.provider.create_calls, 4)
        store.checkpoint = original_checkpoint
        self.provider.complete_all()
        self.engine.tick(discover_source=False)
        self.assertEqual(len(validate_publications(self.config)['articles']), 2)
        self.assertEqual(self.provider.create_calls, 4)

    def test_first_checkpoint_repairs_a_pre_existing_stale_index_without_full_reports(self):
        self.publish()
        index = self.state.read('index.json')
        index['articles'] = []
        self.state.write('index.json', index)
        before_reports = self.reports()
        fresh_engine = Engine(self.config, self.upstream.client, self.provider, self.git)
        with patch.object(fresh_engine.state, 'derive',
                          side_effect=AssertionError('Checkpoint must not rebuild aggregate reports')):
            fresh_engine.checkpoint('Resume an interrupted processing pass')
        self.assertEqual(len(validate_publications(self.config)['articles']), 2)
        self.assertEqual(self.reports(), before_reports)

    def test_source_change_refreshes_index_freshness_before_any_replacement_is_ready(self):
        self.publish()
        self.engine.checkpoint('Warm the publication index')
        before_reports = self.reports()
        self.upstream.change_source()
        self.engine.discover()
        self.engine.checkpoint('Record new English discovery')
        index = validate_publications(self.config)
        self.assertEqual(index['observed_source_revision'], self.upstream.revision)
        self.assertEqual({item['id']: item['status'] for item in index['articles']},
                         {A: 'stale', B: 'ready'})
        self.assertEqual(self.reports(), before_reports)

    def test_human_review_synchronization_refreshes_index_without_new_provider_work(self):
        self.publish()
        self.engine.checkpoint('Warm the publication index')
        before_reports = self.reports()
        calls = self.provider.create_calls
        publication = self.state.record('afr', A)['published']
        html_path = self.state.path(publication['html_path'])
        html_path.write_text(html_path.read_text().replace('Faith', 'Human editorial wording', 1))
        self.engine.state.sync_human_reviews(self.git)
        self.engine.checkpoint('Preserve recognized human editorial work')
        accepted = next(item for item in validate_publications(self.config)['articles'] if item['id'] == A)
        self.assertTrue(accepted['human_reviewed'])
        self.assertEqual(accepted['html_sha256'], self.state.record('afr', A)['published']['html_sha256'])
        self.assertEqual(self.reports(), before_reports)
        self.assertEqual(self.provider.create_calls, calls)

    def test_first_policy_checkpoint_recognizes_human_edits_before_resuming_provider_work(self):
        self.publish()
        # Simulate a fresh worker activating the owner policy in a checkout
        # which also contains an authorized human edit to accepted content.
        self.state.path(POLICY_PATH).unlink()
        publication = self.state.record('afr', A)['published']
        html = self.state.path(publication['html_path'])
        html.write_text(html.read_text().replace('Faith', 'Human editorial wording', 1))
        self.engine = Engine(self.config, self.upstream.client, self.provider, self.git)
        queue(self.state, 'unrelated-translation', languages='deu')
        calls = self.provider.create_calls
        observed_policy_checkpoint = []
        original_checkpoint = self.git.checkpoint

        def observe_policy_checkpoint(message):
            if message == 'runtime: record owner-managed spending policy without monetary limits':
                publication = self.state.record('afr', A)['published']
                self.assertTrue(publication['human_reviewed'])
                accepted = validate_publications(self.config)
                self.assertTrue(next(item for item in accepted['articles'] if item['id'] == A)['human_reviewed'])
                observed_policy_checkpoint.append(message)
            original_checkpoint(message)

        self.git.checkpoint = observe_policy_checkpoint
        self.engine.tick(discover_source=False)
        self.assertEqual(len(observed_policy_checkpoint), 1)
        self.assertEqual(self.state.read(POLICY_PATH), POLICY)
        self.assertGreater(self.provider.create_calls, calls)
        self.assertTrue(self.state.record('afr', A)['published']['human_reviewed'])
        self.assertIn('Human editorial wording', html.read_text())

    def test_cancellation_recognizes_human_edits_before_its_durable_provider_cancel(self):
        self.publish()
        queue(self.state, 'pending-improvement', operation='review', languages='afr', issues='all')
        self.engine.tick(discover_source=False)
        batches = [batch for batch in self.state.batches()
                   if batch['campaign'] == 'pending-improvement' and batch['status'] == 'submitted']
        self.assertEqual(len(batches), 1)
        remote_id = batches[0]['remote_id']
        self.assertEqual(self.provider.batches[remote_id]['status'], 'in_progress')
        publication = self.state.record('afr', A)['published']
        html = self.state.path(publication['html_path'])
        html.write_text(html.read_text().replace('Faith', 'Human editorial wording', 1))
        self.engine = Engine(self.config, self.upstream.client, self.provider, self.git)
        calls = self.provider.create_calls
        with patch.object(self.provider, 'cancel', wraps=self.provider.cancel) as cancel:
            self.engine.cancel_campaign('pending-improvement')
        cancel.assert_called_once_with(remote_id)
        self.assertTrue(self.state.read('state/campaigns/pending-improvement.json')['cancel_requested'])
        self.assertEqual(self.state.read(f'state/batches/{batches[0]["id"]}/batch.json')['status'], 'cancelling')
        self.assertTrue(self.state.record('afr', A)['published']['human_reviewed'])
        self.assertIn('Human editorial wording', html.read_text())
        self.assertEqual(self.provider.create_calls, calls)
        validate_publications(self.config)

    def test_cached_index_never_allows_tampered_accepted_bytes_into_a_git_checkpoint(self):
        self.publish()
        self.enable_local_publication()
        self.engine.checkpoint('Warm the publication index')
        previous_head = self.git_command('rev-parse', 'origin/main')
        publication = self.state.record('afr', A)['published']
        path = self.state.path(publication['html_path'])
        path.write_text(path.read_text().replace('Faith', 'Unrecognized changed bytes', 1))
        with self.assertRaises(ContractError):
            self.engine.checkpoint('Must not publish unrecognized accepted bytes')
        self.assertEqual(self.git_command('rev-parse', 'origin/main'), previous_head)
        with self.assertRaises(ContractError):
            validate_publications(self.config)


if __name__ == '__main__':
    unittest.main()
