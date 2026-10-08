"""Replacement backups are auditable without coupling them to current export."""
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError, digest, json_hash
from berean_translation.validation import export, validate_publication_history, validate_repository
from support import A, B, drive, queue, setup


class PublicationHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.config.runtime['plain_translation_policy_version'] = 1
        self.config.runtime['upgrade_quality_threshold'] = 98
        queue(self.state)
        drive(self.engine, self.provider)
        self.previous = {identity: self.state.record('afr', identity)['published'] for identity in (A, B)}
        queue(self.state, 'improve', operation='review', languages='afr', issues='all')

        def accepted(line):
            response = self.provider.default_result(line)
            if ':review' in line['custom_id']:
                response['score'] = 98
            return response

        drive(self.engine, self.provider, accepted)

    def replacement(self, identity=A):
        record = self.state.record('afr', identity)
        event = next(event for event in record['history'] if event.get('event') == 'publication_replaced')
        return record, event

    def accepted_export(self):
        destination = self.root / '.build/export'
        result = export(self.config, destination, self.upstream.client.discover(),
                        self.upstream.revision, translation_revision='c' * 40)
        self.assertEqual(result['article_count'], 2)
        self.assertTrue((destination / 'index.json').is_file())

    def rewrite_archive(self, mutate):
        record, event = self.replacement()
        original = event['archive']
        archived = self.state.read(original)
        mutate(archived)
        updated = str(Path(original).with_name(json_hash(archived) + '.json'))
        self.state.write(updated, archived)
        self.state.path(original).unlink()
        event['archive'] = updated
        self.state.save_record(record)

    def test_valid_backups_preserve_exact_html_and_metadata_semantics(self):
        self.assertEqual(validate_publication_history(self.config), 2)
        self.assertEqual(validate_repository(self.config)['published'], 2)
        for identity in (A, B):
            _, event = self.replacement(identity)
            archived = self.state.read(event['archive'])
            self.assertEqual(archived['publication'], self.previous[identity])
            self.assertEqual(digest(archived['html']), self.previous[identity]['html_sha256'])
            self.assertEqual(json_hash(archived['metadata']), self.previous[identity]['metadata_sha256'])
        self.accepted_export()

    def test_changed_archive_bytes_fail_full_validation_but_current_export_works(self):
        _, event = self.replacement()
        archived = self.state.read(event['archive'])
        archived['html'] += '\nChanged backup'
        self.state.write(event['archive'], archived)
        with self.assertRaisesRegex(ContractError, 'content-address hash changed'):
            validate_repository(self.config)
        self.accepted_export()

    def test_missing_referenced_archive_fails_full_validation_but_current_export_works(self):
        _, event = self.replacement()
        self.state.path(event['archive']).unlink()
        with self.assertRaisesRegex(ContractError, 'archive is missing'):
            validate_repository(self.config)
        self.accepted_export()

    def test_repointed_archive_to_another_article_is_rejected(self):
        record, event = self.replacement()
        _, other = self.replacement(B)
        event['archive'] = other['archive']
        self.state.save_record(record)
        with self.assertRaisesRegex(ContractError, 'language/article identity'):
            validate_repository(self.config)
        self.accepted_export()

    def test_previous_and_replacement_task_links_must_match_same_article(self):
        record, event = self.replacement()
        event['previous_task'] = self.previous[B]['task']
        self.state.save_record(record)
        with self.assertRaisesRegex(ContractError, 'replacement task linkage changed'):
            validate_repository(self.config)
        self.accepted_export()

    def test_rehashed_archive_still_checks_original_publication_hashes(self):
        self.rewrite_archive(lambda archived: archived['metadata'].update(title='Changed historical title'))
        with self.assertRaisesRegex(ContractError, 'HTML or metadata hashes changed'):
            validate_repository(self.config)
        self.accepted_export()

    def test_rehashed_archive_still_checks_frozen_source_provenance(self):
        self.rewrite_archive(lambda archived: archived['publication'].update(source_revision='d' * 40))
        with self.assertRaisesRegex(ContractError, 'source identity or provenance changed'):
            validate_repository(self.config)
        self.accepted_export()

    def test_unreferenced_backup_is_not_silently_accepted_as_audited_history(self):
        record, event = self.replacement()
        record['history'].remove(event)
        self.state.save_record(record)
        with self.assertRaisesRegex(ContractError, 'archives and replacement history references disagree'):
            validate_repository(self.config)
        self.accepted_export()
