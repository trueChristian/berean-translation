"""Whole-target reporting and accepted-only export remain offline and cost-free."""
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError, digest
from berean_translation.validation import export, validate_publications, validate_repository
from support import A, B, drive, queue, setup


class PlainStatusExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.engine.discover()

    def publish(self):
        queue(self.state)
        drive(self.engine, self.provider)

    def render(self):
        destination = self.root / '.build/export'
        return export(self.config, destination, self.upstream.client.discover(),
                      self.upstream.revision, translation_revision='c' * 40), destination

    def test_untouched_target_pairs_are_explicit_and_categories_sum_to_target(self):
        summary = self.state.work_summary(self.config)
        self.assertEqual(summary['target_pairs'], 2 * len(self.config.languages))
        self.assertEqual(summary['counts']['unstarted'], summary['target_pairs'])
        self.assertEqual(sum(summary['counts'].values()), summary['target_pairs'])
        self.state.derive(self.config)
        report = self.state.path('STATUS.md').read_text()
        self.assertIn('Unstarted', report)
        self.assertIn('Held without publication', report)
        self.assertIn(f'unstarted: {summary["target_pairs"]}', report)

    def test_queued_active_and_held_are_disjoint_and_do_not_hide_missing_pairs(self):
        self.engine.accept_request(queue(self.state))
        tasks = self.state.tasks()
        tasks[0]['status'] = 'in_batch'
        tasks[1]['status'] = 'not_ready'
        for task in tasks:
            self.state.save_task(task)
        summary = self.state.work_summary(self.config)
        self.assertEqual(summary['counts'], {'published': 0, 'unstarted': 38,
            'queued': 0, 'active': 1, 'held_without_publication': 1})
        tasks[1]['status'] = 'queued'
        self.state.save_task(tasks[1])
        summary = self.state.work_summary(self.config)
        self.assertEqual(summary['counts']['queued'], 1)
        self.assertEqual(sum(summary['counts'].values()), 40)

    def test_failed_replacement_keeps_publication_count_and_has_separate_outcome(self):
        self.publish()
        accepted = self.state.work_summary(self.config)
        self.assertEqual(accepted['replacement_counts'], {'queued': 0, 'active': 0, 'held': 0})
        for task in self.state.tasks():
            task['status'] = 'not_ready'
            self.state.save_task(task)
        summary = self.state.work_summary(self.config)
        self.assertEqual(summary['counts']['published'], 2)
        self.assertEqual(summary['counts']['held_without_publication'], 0)
        self.assertEqual(summary['replacement_counts']['held'], 2)
        self.assertEqual(sum(summary['counts'].values()), 40)

    def test_admission_without_task_is_reported_and_source_errors_are_held(self):
        self.state.save_campaign({'id': 'admission-report', 'operation': 'translate'})
        self.state.write('state/manual-admissions/admission-report.json', {'entries': {
            'pending': {'task_id': 'pending', 'item': {'language': 'afr', 'article_id': A},
                        'status': 'pending', 'reason': 'prefetch_wait_budget'},
            'attention': {'task_id': 'attention', 'item': {'language': 'afr', 'article_id': B},
                          'status': 'attention', 'reason': 'missing_edition'}}})
        summary = self.state.work_summary(self.config)
        self.assertEqual(summary['counts']['queued'], 1)
        self.assertEqual(summary['counts']['held_without_publication'], 1)
        self.assertEqual(summary['counts']['unstarted'], 38)
        source = self.state.read('state/source.json')
        source['source_errors'] = {A: 'Unreadable English HTML'}
        self.state.write('state/source.json', source)
        summary = self.state.work_summary(self.config)
        self.assertEqual(summary['source_attention'], len(self.config.languages))
        self.assertEqual(summary['counts']['held_without_publication'], len(self.config.languages) + 1)
        self.assertEqual(sum(summary['counts'].values()), 40)

    def test_report_uses_collection_stamp_and_is_stable_between_unchanged_derivations(self):
        self.state.write('state/last-collection.json', {'completed_at': '2026-10-08T09:17:31Z',
            'newly_published': 3, 'collection': {'stop_reason': 'wait_budget_exhausted',
                                             'submitted_batches': 4}})
        self.state.write('state/report-generation.json', {'generated_at': '2026-10-08T09:17:31Z'})
        self.state.derive(self.config)
        before = self.state.path('STATUS.md').read_bytes()
        self.state.derive(self.config)
        self.assertEqual(before, self.state.path('STATUS.md').read_bytes())
        self.assertIn(b'Generated: 2026-10-08T09:17:31Z', before)
        self.assertIn(b'newly published: 3', before)
        self.assertIn(b'wait_budget_exhausted', before)

    def test_automatic_status_uses_final_authoritative_frontier_ledger(self):
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.state.write('state/automatic-status.json', {'allocated_usd': 99,
            'approved_total_usd': 30, 'pending_requests': 999, 'attention': []})
        self.state.derive(self.config)
        automatic = self.state.read('state/automatic-status.json')
        recovery = self.state.read('RECOVERY.json')
        self.assertEqual(automatic['allocated_usd'], recovery['committed_usd'])
        self.assertEqual(automatic['pending_requests'], 0)
        self.assertEqual(automatic['counts']['unstarted'], 40)
        self.assertEqual(automatic['target_pairs'], 40)

    def test_corrupt_unpublished_batch_history_blocks_runtime_but_not_good_export(self):
        self.publish()
        batch = self.state.batches()[0]
        batch['payload_sha256'] = '0' * 64
        self.state.save_batch(batch)
        with self.assertRaisesRegex(ContractError, 'Batch input payload changed'):
            validate_repository(self.config)
        manifest, destination = self.render()
        self.assertEqual(manifest['article_count'], 2)
        self.assertTrue((destination / 'index.json').is_file())

    def test_export_still_rejects_tampered_accepted_bytes_and_wrong_source_provenance(self):
        self.publish()
        record = self.state.record('afr', A)
        publication = record['published']
        path = self.state.path(publication['html_path'])
        original = path.read_text()
        path.write_text(original.replace('Faith', 'Changed accepted bytes'))
        with self.assertRaises(ContractError):
            self.render()
        path.write_text(original)
        publication['source_revision'] = 'd' * 40
        self.state.save_record(record)
        with self.assertRaisesRegex(ContractError, 'source provenance mismatch'):
            self.render()
        self.assertFalse((self.root / '.build/export').exists())

    def test_unknown_current_english_fingerprint_holds_export_without_losing_publication(self):
        self.publish()
        before = self.state.publication_candidate(self.state.record('afr', A)['published'])[2]
        inventory = self.upstream.client.discover()
        inventory['articles'][A].update(source_error='Unreadable English HTML', translation_key=None)
        destination = self.root / '.build/export'
        with self.assertRaisesRegex(ContractError, f'current English fingerprint.*{A}.*last deployed site'):
            export(self.config, destination, inventory, self.upstream.revision,
                   translation_revision='c' * 40)
        self.assertFalse(destination.exists())
        self.assertEqual(self.state.publication_candidate(self.state.record('afr', A)['published'])[2], before)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)

    def test_plain_publication_skips_scripture_number_gate_but_legacy_keeps_it(self):
        self.publish()
        record = self.state.record('afr', A)
        publication = record['published']
        path = self.state.path(publication['html_path'])
        text = path.read_text().replace('3:16', '3:17')
        path.write_text(text)
        publication['html_sha256'] = digest(text)
        publication['plain_translation_policy_version'] = 1
        self.state.save_record(record)
        self.state.derive(self.config)
        validate_publications(self.config)
        publication.pop('plain_translation_policy_version')
        self.state.save_record(record)
        with self.assertRaisesRegex(ContractError, 'Scripture chapter/verse'):
            validate_publications(self.config)

