"""Recognition refreshes derived views without relaxing publication checks."""
import copy
import tempfile
import unittest
from pathlib import Path

from berean_translation.cli import main
from berean_translation.common import ContractError
from berean_translation.validation import export, validate_repository
from support import A, drive, queue, setup


class RecognitionReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config,self.state,self.source,self.provider,self.git,self.engine = setup(self.root)
        queue(self.state); drive(self.engine,self.provider)

    def recognize(self):
        return main(['--root',str(self.root),'validate','--recognize-human-edits'])

    def test_unchanged_records_refresh_stale_reports_and_export(self):
        records = copy.deepcopy(self.state.records())
        self.state.write('index.json', {'stale':True})
        with self.assertRaises(ContractError):
            validate_repository(self.config)
        self.assertEqual(self.recognize(),0)
        self.assertEqual(self.state.records(),records)
        self.assertEqual(validate_repository(self.config)['ready'],2)
        manifest = export(self.config,self.root/'.build/export',self.source.client.discover(),
                          self.source.revision,translation_revision='a'*40)
        self.assertEqual(manifest['article_count'],2)

    def test_backfilled_human_records_refresh_once_and_repeat_idempotently(self):
        record = self.state.record('afr',A); path=self.state.path(record['published']['html_path'])
        path.write_text(path.read_text().replace('Faith and','Human editorial change and'))
        self.state.sync_human_reviews(self.git)  # Accepted state precedes report regeneration.
        self.assertTrue(self.state.record('afr',A)['published']['human_reviewed'])
        records = copy.deepcopy(self.state.records())
        body = path.read_bytes()
        self.assertEqual(self.recognize(),0)
        reports = {name:self.state.path(name).read_bytes() for name in ('index.json','STATUS.md','RECOVERY.json')}
        self.assertEqual(self.recognize(),0)
        self.assertEqual(self.state.records(),records)
        self.assertEqual(path.read_bytes(),body)
        self.assertEqual({name:self.state.path(name).read_bytes() for name in reports},reports)
        self.assertEqual(validate_repository(self.config)['ready'],2)

    def test_corrupt_provenance_still_fails_before_report_refresh(self):
        record = self.state.record('afr',A)
        self.state.write(record['published']['source_snapshot'], {'corrupt':True})
        before = self.state.path('index.json').read_bytes()
        self.assertEqual(self.recognize(),1)
        self.assertEqual(self.state.path('index.json').read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
