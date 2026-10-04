"""Accepted publications survive English changes with verifiable historical context."""
import copy
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError, json_hash, read_json
from berean_translation.html import split_article
from berean_translation.validation import export
from support import A, B, ISSUE, ISSUE2, drive, queue, setup


class RetainedExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        queue(self.state)
        drive(self.engine, self.provider)

    def render(self):
        destination = self.root / '.build/export'
        result = export(self.config, destination, self.upstream.client.discover(),
                        self.upstream.revision, translation_revision='c' * 40)
        return result, destination, {a['id']: a for a in read_json(destination / 'index.json')['articles']}

    def test_moved_stale_source_keeps_frozen_issue_and_exact_accepted_bytes(self):
        pub = copy.deepcopy(self.state.record('afr', A)['published'])
        text = self.state.publication_candidate(pub)[2]
        self.upstream.change_source()
        self.upstream.articles[0]['issue_id'] = ISSUE2
        self.upstream.issues.append({'id': ISSUE2, 'slug': 'new-issue', 'source_id': 'new-issue',
                                     'date': {}, 'publication': 'Test'})
        self.upstream.rebuild()
        result, destination, entries = self.render()
        item = entries[A]
        self.assertEqual(result['article_count'], 2)
        self.assertEqual(result['omitted'], [])
        self.assertEqual(item['status'], 'stale')
        self.assertEqual(item['issue_id'], ISSUE)
        self.assertEqual(item['retained_source']['article']['issue_id'], ISSUE)
        self.assertEqual(item['source_translation_key'], pub['translation_key'])
        self.assertEqual(item['source_revision'], self.state.source(pub)['revision'])
        self.assertEqual((destination / item['html']).read_text(), text)
        self.assertEqual(entries[B]['status'], 'ready')
        self.assertNotIn('retained', entries[B])
        self.assertEqual(self.state.record('afr', A)['published'], pub)

    def test_human_work_stays_protected_and_exported_after_source_changes(self):
        pub = self.state.record('afr', A)['published']
        path = self.state.path(pub['html_path'])
        path.write_text(path.read_text().replace('Faith and', 'Editorial judgement and'))
        self.engine.tick()
        accepted = path.read_text()
        previous_task = self.state.record('afr', A)['latest_task']
        self.upstream.change_source()
        self.engine.tick()
        queue(self.state, 'force-review', operation='review', issues='all')
        drive(self.engine, self.provider)
        self.assertEqual(self.state.record('afr', A)['latest_task'], previous_task)
        _, destination, entries = self.render()
        self.assertTrue(entries[A]['human_reviewed'])
        self.assertEqual(entries[A]['status'], 'stale')
        self.assertEqual(split_article((destination / entries[A]['html']).read_text())[0],
                         split_article(accepted)[0])
        self.assertEqual(path.read_text(), accepted)

    def test_tampered_frozen_source_cannot_be_exported_as_retained(self):
        pub = self.state.record('afr', A)['published']
        source = self.state.source(pub)
        source['article']['title'] = 'Unverified historical metadata'
        self.state.write(pub['source_snapshot'], source)
        self.upstream.change_source()
        with self.assertRaises(ContractError):
            self.render()
        self.assertFalse((self.root / '.build/export').exists())

    def test_snapshot_proof_covers_metadata_and_exact_historical_html(self):
        pub = self.state.record('afr', A)['published']
        snapshot = self.state.source(pub)
        self.upstream.change_source()
        _, _, entries = self.render()
        proof = entries[A]['retained_source']
        rebuilt = {key: proof[key] for key in ('article', 'fingerprints', 'repository', 'revision', 'translation_key')}
        rebuilt['html'] = snapshot['html']
        self.assertEqual(json_hash(rebuilt), proof['snapshot_sha256'])
        rebuilt['article'] = dict(rebuilt['article'], title='Changed')
        self.assertNotEqual(json_hash(rebuilt), proof['snapshot_sha256'])
        self.assertNotIn('html', proof)
