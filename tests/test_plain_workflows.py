"""Operational separation, trusted writers, and retired workflow boundaries."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(filename):
    return yaml.load((ROOT / '.github/workflows' / filename).read_text(),
                     Loader=yaml.BaseLoader)


class PlainWorkflowTests(unittest.TestCase):
    def test_only_the_five_required_workflows_are_active(self):
        active = {path.name for path in (ROOT / '.github/workflows').glob('*.yml')}
        self.assertEqual(active, {'ai-translate.yml', 'ai-discover.yml', 'ai-worker.yml',
                                  'ai-review.yml', 'ci.yml'})
        for retired in ('ai-repair.yml', 'ai-scripture-components.yml', 'ai-recover.yml'):
            self.assertFalse((ROOT / 'docs/historical-workflows' / retired).exists())

    def test_discovery_and_collection_share_the_serialized_state_writer(self):
        discovery = workflow('ai-discover.yml')
        collection = workflow('ai-worker.yml')
        self.assertEqual(discovery['concurrency'], collection['concurrency'])
        self.assertEqual(discovery['concurrency'], {
            'group': 'berean-translation-state-writer', 'cancel-in-progress': 'false'})
        self.assertEqual(discovery['permissions'], {'contents': 'write'})
        self.assertEqual(discovery['jobs']['discover']['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(discovery['on']['schedule'], [{'cron': '3 * * * *'}])
        self.assertNotIn('pull_request', discovery['on'])
        self.assertNotIn('pull_request_target', discovery['on'])

    def test_discovery_neither_installs_model_clients_nor_receives_provider_credentials(self):
        discovery = workflow('ai-discover.yml')
        rendered = str(discovery)
        self.assertNotIn('OPENAI_API_KEY', rendered)
        self.assertNotIn('secrets.', rendered)
        self.assertNotIn('pip install', rendered)
        commands = '\n'.join(step.get('run', '')
                             for step in discovery['jobs']['discover']['steps'])
        self.assertIn('tick --discover-only --publish', commands)
        self.assertIn('berean_translation validate', commands)

    def test_collection_has_no_active_scripture_dependencies(self):
        collection = workflow('ai-worker.yml')
        commands = '\n'.join(step.get('run', '')
                             for step in collection['jobs']['worker']['steps'])
        self.assertIn('-r requirements.txt', commands)
        self.assertNotIn('requirements-scripture.txt', commands)
        self.assertNotIn('scripture-components', commands)
        self.assertIn('tick --no-discover --publish', commands)
        self.assertFalse((ROOT / 'requirements-scripture.txt').exists())
        self.assertNotIn('requirements-scripture', (ROOT / 'requirements-dev.txt').read_text())

    def test_improvement_uses_stronger_defaults_without_changing_translation_defaults(self):
        translation = workflow('ai-translate.yml')
        improvement = workflow('ai-review.yml')
        self.assertEqual(translation['name'], 'AI — Translate articles')
        self.assertEqual(improvement['name'], 'AI — Improve translations')
        for field in ('model', 'review_model'):
            self.assertEqual(translation['on']['workflow_dispatch']['inputs'][field]['default'],
                             'gpt-6-luna')
            self.assertEqual(improvement['on']['workflow_dispatch']['inputs'][field]['default'],
                             'gpt-6.1-sol')
        for document in (translation, improvement):
            self.assertNotIn('concurrency', document)
            self.assertNotIn('OPENAI_API_KEY', str(document))
            self.assertEqual(document['on']['workflow_dispatch']['inputs']['dry_run']['default'],
                             'true')


if __name__ == '__main__':
    unittest.main()
