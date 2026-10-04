"""Offline accepted-copy integrity and independent article edit handling."""
from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, digest, json_hash, loads, read_json
from berean_translation.gitstore import GitStore
from berean_translation.html import split_article
from berean_translation.state import State
from berean_translation.validation import export, validate_repository
from support import A, B, drive, queue, setup


class PublicationEditIsolationTests(unittest.TestCase):
    def setUp(self):
        self.prepare()

    def prepare(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        queue(self.state)
        drive(self.engine, self.provider)
        self.before = {identity: copy.deepcopy(self.publication(identity)) for identity in (A, B)}
        self.html = {identity: self.path(identity).read_text() for identity in (A, B)}
        self.metadata = {identity: self.state.read(self.publication(identity)['metadata_path'])
                         for identity in (A, B)}

    def publication(self, identity=A):
        return self.state.record('afr', identity)['published']

    def path(self, identity=A, field='html_path'):
        return self.state.path(self.publication(identity)[field])

    def quarantine(self):
        edited = self.html[A].replace('</article>', '<script>alert(1)</script></article>')
        self.path().write_text(edited)
        self.engine.tick()
        self.assertTrue(self.publication().get('edit_issue'))
        return edited

    def export_articles(self, name='export'):
        destination = self.root / '.build' / name
        result = export(self.config, destination, self.upstream.client.discover(),
                        self.upstream.revision, translation_revision='d' * 40)
        self.assertEqual(result['article_count'], 2)
        entries = read_json(destination / 'index.json')['articles']
        return destination, {entry['id']: entry for entry in entries}

    def assert_accepted_identity(self, actual, expected):
        for key in ('html_sha256', 'metadata_sha256', 'human_reviewed', 'human_review',
                    'model', 'review_model', 'source_snapshot', 'translation_key', 'notice_html'):
            self.assertEqual(actual[key], expected[key], key)

    def run_git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def commit(self, paths, *, human=True):
        name = 'Fixture editor' if human else 'github-actions[bot]'
        self.run_git('add', '--', *paths)
        self.run_git('-c', 'user.name=' + name, '-c', 'user.email=fixture@example.test',
                     'commit', '-m', 'Offline publication fixture')
        return self.run_git('rev-parse', 'HEAD')

    def test_bad_article_does_not_block_an_independent_human_edit_or_export(self):
        broken = self.html[A].replace('</article>', '<script>bad()</script></article>')
        self.path(A).write_text(broken)
        corrected = self.html[B].replace('Faith and', 'A human correction and')
        self.path(B).write_text(corrected)
        calls = self.provider.create_calls
        self.engine.tick()
        self.assertTrue(self.publication(A).get('edit_issue'))
        self.assert_accepted_identity(self.publication(A), self.before[A])
        self.assertIs(self.publication(B)['human_reviewed'], True)
        self.assertFalse(self.publication(B).get('edit_issue'))
        self.assertEqual(split_article(self.path(B).read_text())[0], split_article(corrected)[0])
        self.assertEqual(self.path(A).read_text(), broken)
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        destination, entries = self.export_articles()
        self.assertEqual((destination / entries[A]['html']).read_text(), self.html[A])
        self.assertEqual(split_article((destination / entries[B]['html']).read_text())[0], split_article(corrected)[0])
        self.assertEqual(self.provider.create_calls, calls)
        records = copy.deepcopy(self.state.records())
        self.engine.tick()
        self.assertEqual(self.state.records(), records)

    def test_missing_html_or_sidecar_stays_exported_from_accepted_copy(self):
        for field in ('html_path', 'metadata_path'):
            with self.subTest(field=field):
                self.prepare()
                path = self.path(field=field)
                path.unlink()
                self.engine.tick()
                publication = self.publication()
                self.assertIsNone(publication['edit_issue']['observed_files'][field])
                self.assert_accepted_identity(publication, self.before[A])
                self.assertFalse(path.exists())
                self.assertEqual(self.publication(B), self.before[B])
                self.assertEqual(validate_repository(self.config)['ready'], 2)
                destination, entries = self.export_articles()
                self.assertEqual((destination / entries[A]['html']).read_text(), self.html[A])
                self.assertEqual(read_json(destination / entries[A]['metadata']), self.metadata[A])

    def test_directory_or_symlink_working_path_does_not_get_followed_or_block_peers(self):
        for kind in ('directory', 'symlink'):
            with self.subTest(kind=kind):
                self.prepare()
                path = self.path()
                path.unlink()
                if kind == 'directory':
                    path.mkdir()
                else:
                    target = self.root / 'unrelated.txt'
                    target.write_text('Do not read or replace as a publication')
                    path.symlink_to(target)
                self.engine.tick()
                self.assertTrue(self.publication()['edit_issue']['observed_files']['html_path']['unavailable'])
                self.assertEqual(validate_repository(self.config)['ready'], 2)
                destination, entries = self.export_articles()
                self.assertEqual((destination / entries[A]['html']).read_text(), self.html[A])
                self.assertEqual(self.publication(B), self.before[B])
                if kind == 'directory':
                    self.assertTrue(path.is_dir())
                else:
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(target.read_text(), 'Do not read or replace as a publication')

    def test_changed_working_files_need_fresh_isolation_before_export(self):
        self.quarantine()
        changed = self.path().read_text().replace('alert(1)', 'alert(2)')
        self.path().write_text(changed)
        with self.assertRaises(ContractError):
            self.state.projection(self.config)
        with self.assertRaises(ContractError):
            self.export_articles()
        self.engine.tick()
        self.assertEqual(self.path().read_text(), changed)
        destination, entries = self.export_articles('resynchronized')
        self.assertEqual((destination / entries[A]['html']).read_text(), self.html[A])

    def test_tampered_or_missing_snapshot_fails_without_rewriting_records_or_working_files(self):
        for damage in ('content', 'missing', 'renamed-content'):
            with self.subTest(damage=damage):
                self.prepare()
                edited = self.quarantine()
                publication = self.publication()
                snapshot_path = self.state.path(publication['accepted_snapshot'])
                if damage == 'missing':
                    snapshot_path.unlink()
                else:
                    payload = read_json(snapshot_path)
                    payload['html'] = payload['html'].replace('Faith and', 'Corrupted snapshot and')
                    if damage == 'renamed-content':
                        # A valid payload filename alone cannot authorize new bytes.
                        publication['accepted_snapshot'] = f'state/publications/{json_hash(payload)}.json'
                        record = self.state.record('afr', A)
                        record['published'] = publication
                        self.state.save_record(record)
                        snapshot_path = self.state.path(publication['accepted_snapshot'])
                    self.state.write(publication['accepted_snapshot'], payload)
                before_records = copy.deepcopy(self.state.records())
                snapshot_bytes = snapshot_path.read_bytes() if snapshot_path.exists() else None
                with self.assertRaises(ContractError):
                    self.engine.tick()
                with self.assertRaises(ContractError):
                    validate_repository(self.config, check_index=False)
                self.assertEqual(self.state.records(), before_records)
                self.assertEqual(self.path().read_text(), edited)
                self.assertEqual(self.path(B).read_text(), self.html[B])
                self.assertEqual(snapshot_path.read_bytes() if snapshot_path.exists() else None, snapshot_bytes)

    def test_corrupt_source_or_human_attribution_does_not_become_an_editorial_issue(self):
        for damage in ('source', 'human-attribution'):
            with self.subTest(damage=damage):
                self.prepare()
                if damage == 'source':
                    publication = self.publication()
                    payload = self.state.read(publication['source_snapshot'])
                    payload['html'] += '<!-- corrupt provenance -->'
                    self.state.write(publication['source_snapshot'], payload)
                else:
                    self.path().write_text(self.html[A].replace('Faith and', 'A human edit and'))
                    self.engine.tick()
                    record = self.state.record('afr', A)
                    record['published']['human_review'] = None
                    self.state.save_record(record)
                before_records = copy.deepcopy(self.state.records())
                before_files = {identity: self.path(identity).read_bytes() for identity in (A, B)}
                with self.assertRaises(ContractError):
                    self.engine.tick()
                self.assertEqual(self.state.records(), before_records)
                self.assertFalse(self.publication().get('edit_issue'))
                self.assertEqual({identity: self.path(identity).read_bytes() for identity in (A, B)}, before_files)

    def test_legacy_human_snapshot_recovers_independently_committed_html_and_metadata(self):
        self.run_git('init', '-b', 'main')
        self.commit(['.'], human=False)
        body = split_article(self.html[A])[0].replace('Faith and', 'Legacy human text and')
        legacy_html = body + '\n'
        self.path().write_text(legacy_html)
        html_commit = self.commit([self.publication()['html_path']])
        metadata = {**self.metadata[A], 'title': 'A separately committed human title'}
        self.state.write(self.publication()['metadata_path'], metadata)
        metadata_commit = self.commit([self.publication()['metadata_path']])
        self.assertNotEqual(html_commit, metadata_commit)
        record = self.state.record('afr', A)
        publication = record['published']
        evidence = {'commit': html_commit, 'author': 'Fixture editor',
                    'email': 'fixture@example.test', 'time': '2026-01-01T00:00:00Z'}
        publication.update(html_sha256=digest(legacy_html), metadata_sha256=json_hash(metadata),
                           human_reviewed=True, human_review=evidence)
        publication.pop('accepted_snapshot', None)
        publication.pop('human_review_notice_html', None)
        record['history'].append({'event': 'human_review', 'evidence': evidence,
                                  'html_sha256': digest(legacy_html), 'metadata_sha256': json_hash(metadata)})
        self.state.save_record(record)
        self.state.derive(self.config)
        self.commit(['state', 'index.json', 'STATUS.md', 'RECOVERY.json'], human=False)
        accepted = copy.deepcopy(publication)
        broken = legacy_html.replace('</article>', '<script>bad()</script></article>')
        self.path().write_text(broken)
        self.commit([publication['html_path']])
        self.engine.gitstore = GitStore(self.root)
        self.engine.tick()
        publication = self.publication()
        self.assert_accepted_identity(publication, accepted)
        self.assertTrue(publication.get('edit_issue'))
        self.assertTrue(publication.get('accepted_snapshot'))
        candidate, tail, text = self.state.publication_candidate(publication)
        self.assertEqual(text, legacy_html)
        self.assertEqual(tail, '')
        self.assertEqual({key: candidate[key] for key in metadata}, metadata)
        self.assertEqual(self.path().read_text(), broken)
        self.assertEqual(validate_repository(self.config)['ready'], 2)

    def test_legacy_unreviewed_human_history_is_promoted_without_new_paid_work(self):
        self.run_git('init', '-b', 'main')
        self.commit(['.'], human=False)
        legacy_html = self.html[A].replace('Faith and', 'Previously accepted human wording and')
        self.path().write_text(legacy_html)
        commit = self.commit([self.publication()['html_path']])
        record = self.state.record('afr', A)
        evidence = {'commit': commit, 'author': 'Fixture editor',
                    'email': 'fixture@example.test', 'time': '2026-01-01T00:00:00Z'}
        record['published'].update(html_sha256=digest(legacy_html), human_reviewed=False, human_review=None)
        record['history'].append({'event': 'human_edit_unreviewed', 'evidence': evidence,
                                  'html_sha256': digest(legacy_html),
                                  'metadata_sha256': record['published']['metadata_sha256']})
        self.state.save_record(record)
        self.state.derive(self.config)
        self.engine.gitstore = GitStore(self.root)
        calls = self.provider.create_calls
        self.engine.tick()
        publication = self.publication()
        self.assertIs(publication['human_reviewed'], True)
        self.assertEqual(publication['human_review'], evidence)
        body, tail = split_article(self.path().read_text())
        self.assertEqual(body, split_article(legacy_html)[0])
        self.assertIn('data-translation-notice="human-reviewed"', tail)
        self.assertNotIn('gpt-', tail)
        self.assertEqual(self.provider.create_calls, calls)
        article = self.state.read('state/source.json')['articles'][A]
        self.assertFalse(self.engine.eligible(article, 'afr', 'review', True)[0])
        self.assertFalse(self.engine.eligible(article, 'afr', 'translate', True)[0])
        self.assertEqual(validate_repository(self.config)['ready'], 2)

    def test_legacy_reviewed_footer_is_standardized_without_requiring_a_new_edit(self):
        record = self.state.record('afr', A)
        evidence = {'commit': 'c' * 40, 'author': 'Fixture editor',
                    'email': 'fixture@example.test', 'time': '2026-01-01T00:00:00Z'}
        old_footer = record['published']['notice_html'].replace(
            'en is nog nie deur ’n mens nagegaan nie.', 'en is een keer deur ’n mens nagegaan.')
        body = split_article(self.html[A])[0]
        legacy_html = body + '\n\n' + old_footer + '\n'
        self.path().write_text(legacy_html)
        record['published'].update(html_sha256=digest(legacy_html), human_reviewed=True,
                                   human_review=evidence, human_review_notice_html=old_footer)
        self.state.save_record(record)
        self.state.derive(self.config)
        calls = self.provider.create_calls
        self.engine.tick()
        publication = self.publication()
        self.assertEqual(publication['human_review'], evidence)
        actual_body, tail = split_article(self.path().read_text())
        self.assertEqual(actual_body, body)
        self.assertIn('data-translation-notice="human-reviewed"', tail)
        self.assertNotIn('gpt-', tail)
        self.assertNotEqual(tail, old_footer)
        self.assertEqual(self.provider.create_calls, calls)
        records = copy.deepcopy(self.state.records())
        self.engine.tick()
        self.assertEqual(self.state.records(), records)
        self.assertEqual(validate_repository(self.config)['ready'], 2)

    def test_export_does_not_mix_a_new_publication_with_old_projected_metadata(self):
        real_projection = State.projection
        calls = []

        def project_then_change(state, config):
            projected = real_projection(state, config)
            calls.append(True)
            # validate_repository projects first; export's separate projection is
            # the later view whose hashes must bind every written payload.
            if len(calls) == 2:
                self.path().write_text(self.html[A].replace('Faith and', 'Concurrent human edit and'))
                self.state.sync_human_reviews(self.git)
            return projected

        before = self.publication()
        with patch.object(State, 'projection', project_then_change):
            with self.assertRaises(ContractError):
                self.export_articles('racing-export')
        self.assertFalse((self.root / '.build/racing-export').exists())
        self.assertNotEqual(self.publication()['html_sha256'], before['html_sha256'])
        self.assertIs(self.publication()['human_reviewed'], True)

    def prepare_mixed_review(self):
        queue(self.state, 'mixed-review', operation='review', issues='all')
        self.provider.raise_upload = True
        self.engine.tick()
        batch = next(item for item in self.state.batches() if item['campaign'] == 'mixed-review')
        self.assertEqual(batch['status'], 'prepared')
        self.assertIsNone(batch['remote_id'])
        self.provider.raise_upload = False
        return batch

    def assert_partition(self, original, original_payload, reserved, attempts):
        parent = self.state.read(f'state/batches/{original["id"]}/batch.json')
        self.assertEqual(parent['status'], 'cancelled_before_submission')
        self.assertEqual(self.state.path(f'state/batches/{parent["id"]}/input.jsonl').read_bytes(), original_payload)
        child = self.state.read(f'state/batches/{parent["replacement_batch"]}/batch.json')
        self.assertEqual(child['reservation_reused_from'], parent['id'])
        self.assertEqual(child['reserved_usd'], parent['reserved_usd'])
        self.assertEqual(self.state.read('state/campaigns/mixed-review.json')['reserved_usd'], reserved)
        task_ids = {identity: self.state.record('afr', identity)['latest_task'] for identity in (A, B)}
        self.assertEqual(child['tasks'], [task_ids[B]])
        self.assertEqual(parent['excluded_task_ids'], [task_ids[A]])
        child_rows = [loads(line) for line in self.state.path(f'state/batches/{child["id"]}/input.jsonl').read_bytes().splitlines()]
        original_rows = [loads(line) for line in original_payload.splitlines()]
        self.assertEqual(child_rows, [row for row in original_rows if row['custom_id'].startswith(task_ids[B] + ':')])
        for identity, task_id in task_ids.items():
            task = self.state.read(f'state/tasks/{task_id}/task.json')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), attempts[task_id])
            if identity == A:
                self.assertEqual(task['status'], 'cancelled')
            else:
                self.assertEqual(task['batch'], child['id'])
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        return child

    def test_mixed_prepared_batch_excludes_human_work_without_new_attempts_or_reservation(self):
        original = self.prepare_mixed_review()
        original_payload = self.state.path(f'state/batches/{original["id"]}/input.jsonl').read_bytes()
        reserved = self.state.read('state/campaigns/mixed-review.json')['reserved_usd']
        attempts = {task['id']: (task['translation_attempts'], task['review_attempts'])
                    for task in self.state.tasks() if task['campaign'] == 'mixed-review'}
        calls = self.provider.create_calls
        self.path().write_text(self.html[A].replace('Faith and', 'A human edit and'))
        self.engine.tick()
        child = self.assert_partition(original, original_payload, reserved, attempts)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertIs(self.publication(A)['human_reviewed'], True)
        human_html = self.path().read_bytes()
        # Pausing collection does not create another child or reset reservations.
        before_batches = copy.deepcopy(self.state.batches())
        self.engine.provider = None
        self.engine.tick()
        self.assertEqual(self.state.batches(), before_batches)
        self.engine.provider = self.provider
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, calls + 1)
        remote = next(batch for batch in self.provider.batches.values() if batch['metadata']['campaign'] == 'mixed-review')
        sent = [loads(line) for line in self.provider.files[remote['input_file_id']].splitlines()]
        self.assertEqual([row['custom_id'] for row in sent], child['custom_ids'])
        drive(self.engine, self.provider)
        self.assertEqual(self.provider.create_calls, calls + 1)
        self.assertEqual(self.path().read_bytes(), human_html)
        self.assertEqual(self.state.read('state/campaigns/mixed-review.json')['reserved_usd'], reserved)
        self.assertEqual(len([batch for batch in self.state.batches() if batch.get('reservation_reused_from') == original['id']]), 1)
        self.assertEqual(validate_repository(self.config)['ready'], 2)

    def test_concurrent_human_edit_at_upload_or_intent_checkpoint_is_excluded_before_create(self):
        checkpoints = ('runtime: persist OpenAI input file before creating a batch',
                       'runtime: record batch submission intent before the billable request')
        for checkpoint_message in checkpoints:
            with self.subTest(checkpoint=checkpoint_message):
                self.prepare()
                old_checkpoint = self.engine.checkpoint
                changed = []

                def checkpoint(message):
                    old_checkpoint(message)
                    if message == checkpoint_message and not changed:
                        self.path().write_text(self.html[A].replace('Faith and', 'Concurrent human work and'))
                        changed.append(True)

                self.engine.checkpoint = checkpoint
                calls = self.provider.create_calls
                queue(self.state, 'mixed-review', operation='review', issues='all')
                self.engine.tick()
                self.assertTrue(changed)
                self.assertEqual(self.provider.create_calls, calls)
                original = next(batch for batch in self.state.batches()
                                if batch['campaign'] == 'mixed-review' and batch.get('replacement_batch'))
                payload = self.state.path(f'state/batches/{original["id"]}/input.jsonl').read_bytes()
                reserved = self.state.read('state/campaigns/mixed-review.json')['reserved_usd']
                attempts = {task['id']: (task['translation_attempts'], task['review_attempts'])
                            for task in self.state.tasks() if task['campaign'] == 'mixed-review'}
                child = self.assert_partition(original, payload, reserved, attempts)
                self.assertIs(self.publication()['human_reviewed'], True)
                self.engine.tick()
                self.assertEqual(self.provider.create_calls, calls + 1)
                sent = [loads(line) for line in self.provider.files[
                    next(batch for batch in self.provider.batches.values()
                         if batch['metadata']['campaign'] == 'mixed-review')['input_file_id']].splitlines()]
                self.assertEqual([row['custom_id'] for row in sent], child['custom_ids'])

    def test_partition_reservation_link_cannot_be_repointed(self):
        original = self.prepare_mixed_review()
        self.path().write_text(self.html[A].replace('Faith and', 'A human edit and'))
        self.engine.tick()
        original = self.state.read(f'state/batches/{original["id"]}/batch.json')
        child = self.state.read(f'state/batches/{original["replacement_batch"]}/batch.json')
        child['reservation_reused_from'] = 'f' * 32
        self.state.save_batch(child)
        before_records = copy.deepcopy(self.state.records())
        before_files = {identity: self.path(identity).read_bytes() for identity in (A, B)}
        with self.assertRaises(ContractError):
            validate_repository(self.config, check_index=False)
        self.assertEqual(self.state.records(), before_records)
        self.assertEqual({identity: self.path(identity).read_bytes() for identity in (A, B)}, before_files)
        child.pop('reservation_reused_from')
        self.state.save_batch(child)
        with self.assertRaises(ContractError):
            validate_repository(self.config, check_index=False)
        child['reservation_reused_from'] = original['id']
        self.state.save_batch(child)
        original['replacement_batch'] = 'e' * 32
        self.state.save_batch(original)
        with self.assertRaises(ContractError):
            validate_repository(self.config, check_index=False)


if __name__ == '__main__':
    unittest.main()
