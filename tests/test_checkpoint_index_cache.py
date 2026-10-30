"""Publication changes refresh the index without rescanning for provider work."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from berean_translation.common import ContractError
from berean_translation.engine import Engine
from support import A, B, drive, queue, setup


class CheckpointIndexCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.engine.discover()

    def watch_projection(self):
        projection = Mock(wraps=self.engine.state.projection)
        self.engine.state.projection = projection
        return projection

    def test_provider_and_unpublished_work_reuse_index_and_restore_stale_file(self):
        projection = self.watch_projection()
        self.engine.checkpoint('initial publication snapshot')
        expected = self.state.read('index.json')
        self.state.write('state/batches/poll.json', {'remote_status': 'in_progress'})
        self.state.save_record({'language': 'afr', 'article_id': B,
                                'published': None, 'history': [{'event': 'requested'}]})
        self.engine.checkpoint('provider and new unpublished task')
        self.state.write('index.json', {'stale': True})
        self.engine.checkpoint('restore the cached publication index')
        self.assertEqual(projection.call_count, 1)
        self.assertEqual(self.state.read('index.json'), expected)
        self.assertEqual(len(self.git.checkpoints), 3)

    def test_source_language_and_repository_changes_refresh_index(self):
        projection = self.watch_projection()
        self.engine.checkpoint('initial snapshot')
        self.upstream.change_source()
        self.engine.discover()
        self.engine.checkpoint('changed English')
        self.assertEqual(self.state.read('index.json')['observed_source_revision'], self.upstream.revision)
        self.config.languages['afr']['tag'] = 'af-NA'
        self.engine.checkpoint('changed language configuration')
        self.config.runtime['source_repository'] = 'fixture/english'
        self.engine.checkpoint('changed repository configuration')
        self.assertEqual(self.state.read('index.json')['source_repository'], 'fixture/english')
        self.assertEqual(projection.call_count, 4)

    def test_publication_metadata_change_refreshes_index(self):
        queue(self.state)
        drive(self.engine, self.provider)
        self.engine = Engine(self.config, self.upstream.client, self.provider, self.git)
        projection = self.watch_projection()
        self.engine.checkpoint('read accepted archive')
        record = self.state.record('afr', A)
        record['published']['model'] = 'recorded-model-change'
        self.state.save_record(record)
        self.engine.checkpoint('refresh changed publication metadata')
        self.engine.checkpoint('unchanged provider bookkeeping')
        entry = next(item for item in self.state.read('index.json')['articles'] if item['id'] == A)
        self.assertEqual(entry['translation_model'], 'recorded-model-change')
        self.assertEqual(projection.call_count, 2)

    def test_failed_projection_blocks_checkpoint_and_retries_uncached(self):
        projection = self.engine.state.projection
        watched = Mock(side_effect=[ContractError('Invalid publication'), projection(self.config)])
        self.engine.state.projection = watched
        with self.assertRaisesRegex(ContractError, 'Invalid publication'):
            self.engine.checkpoint('must not become durable')
        self.assertEqual(self.git.checkpoints, [])
        self.engine.checkpoint('retry after publication repair')
        self.assertEqual(watched.call_count, 2)
        self.assertEqual(self.git.checkpoints, ['retry after publication repair'])


if __name__ == '__main__':
    unittest.main()
