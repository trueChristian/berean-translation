"""Source collection remains independent of the website's polling workflow."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowPublicationTests(unittest.TestCase):
    def setUp(self):
        self.document = yaml.load(
            (ROOT / '.github/workflows/ai-worker.yml').read_text(encoding='utf-8'),
            Loader=yaml.BaseLoader)
        self.worker = self.document['jobs']['worker']
        self.steps = {step['name']: step for step in self.worker['steps']}

    def test_collector_keeps_trusted_main_success_and_repository_guards(self):
        condition = self.worker['if']
        for guard in ("github.ref == 'refs/heads/main'",
                      "github.event_name != 'workflow_run'",
                      "github.event.workflow_run.conclusion == 'success'",
                      "github.event.workflow_run.head_branch == 'main'",
                      'github.event.workflow_run.head_repository.full_name == github.repository'):
            self.assertIn(guard, condition)
        self.assertNotIn('always()', condition)
        self.assertNotIn('pull_request_target', self.document['on'])
        self.assertEqual(self.document['permissions'], {'contents': 'write'})
        checkout = self.steps['Check out trusted main and review history']['with']
        self.assertEqual(checkout['ref'], 'main')
        self.assertEqual(checkout['fetch-depth'], '0')
        self.assertEqual(checkout['persist-credentials'], 'true')

    def test_collection_and_maintenance_keep_the_serialized_durable_runtime(self):
        self.assertNotIn('concurrency', self.document)
        self.assertEqual(self.worker['concurrency'], {
            'group': 'berean-translation-state-writer', 'cancel-in-progress': 'false'})
        self.assertEqual(self.document['on']['schedule'], [{'cron': '7,22,37,52 * * * *'}])
        self.assertEqual(self.document['on']['workflow_run'], {
            'workflows': ['AI — Translate articles', 'AI — Improve translations',
                          'AI — Discover English changes'], 'types': ['completed']})
        self.assertEqual(self.document['on']['push'], {
            'branches': ['main'],
            'paths': ['content/**', 'berean_translation/**', 'config/**', 'prompts/**',
                      '.github/workflows/ai-worker.yml']})
        operation = self.document['on']['workflow_dispatch']['inputs']['operation']
        self.assertEqual(operation['options'], ['collect', 'cancel', 'resolve-absent'])
        self.assertEqual(operation['default'], 'collect')
        collect = self.steps['Collect results or perform the requested maintenance']
        for command in ('tick --no-discover --publish --wait-seconds 600 --poll-seconds 60',
                        'cancel --campaign "$CAMPAIGN" --publish',
                        'resolve-absent --batch "$BATCH_ID" --confirmed-no-remote-batch --publish'):
            self.assertIn('.venv/bin/python -m berean_translation ' + command, collect['run'])
        self.assertIn('test "$CONFIRMED_ABSENT" = true', collect['run'])
        self.assertIn('set -euo pipefail', collect['run'])
        self.assertEqual(collect['env']['OPENAI_API_KEY'], '${{ secrets.OPENAI_API_KEY }}')
        self.assertEqual(collect['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertNotIn('continue-on-error', collect)

    def test_automatic_handoff_runs_only_after_successful_collection_and_validation(self):
        handoff = self.document['jobs']['continue']
        self.assertEqual(handoff['needs'], 'worker')
        condition = handoff['if']
        for guard in ('success()', "needs.worker.result == 'success'",
                      "github.ref == 'refs/heads/main'",
                      "github.event_name != 'workflow_dispatch' || inputs.operation == 'collect'"):
            self.assertIn(guard, condition)
        self.assertNotIn('always()', condition)
        self.assertNotIn('continue-on-error', handoff)
        self.assertEqual(handoff['timeout-minutes'], '10')
        # A job dependency covers the worker's required post-collection validation.
        self.assertIn('Validate the resulting runtime records', self.steps)
        self.assertNotIn('continue-on-error', self.worker)
        self.assertNotIn('AI — Collect and continue',
                         self.document['on']['workflow_run']['workflows'])

    def test_handoff_releases_writer_and_only_has_actions_permission_in_its_own_job(self):
        handoff = self.document['jobs']['continue']
        self.assertEqual(self.document['permissions'], {'contents': 'write'})
        self.assertNotIn('actions', self.worker.get('permissions', {}))
        self.assertEqual(handoff['permissions'], {'contents': 'read', 'actions': 'write'})
        self.assertEqual(handoff['concurrency'], {
            'group': 'berean-translation-collector-continuation',
            'cancel-in-progress': 'false'})
        self.assertNotEqual(handoff['concurrency']['group'], self.worker['concurrency']['group'])
        self.assertNotIn('concurrency', self.document)
        checkout, dispatch = handoff['steps']
        self.assertEqual(checkout['with'], {
            'ref': 'main', 'fetch-depth': '0', 'persist-credentials': 'false'})
        self.assertEqual(dispatch['run'], 'python3 -m berean_translation continue-collection')
        self.assertEqual(dispatch['env'], {
            'GH_TOKEN': '${{ github.token }}',
            'OPERATION': "${{ inputs.operation || 'collect' }}"})
        self.assertNotIn('OPENAI_API_KEY', str(handoff))
        self.assertNotIn('secrets.', str(handoff))
        self.assertNotIn('pip install', str(handoff))

    def test_english_checkout_only_runs_for_collection(self):
        source = self.steps['Read current English main once for this collection cycle']
        self.assertEqual(source['if'],
                         "github.event_name != 'workflow_dispatch' || inputs.operation == 'collect'")
        self.assertEqual(source['with'], {
            'repository': 'trueChristian/berean-voice', 'ref': 'main',
            'path': '.build/source', 'sparse-checkout': 'content/articles',
            'persist-credentials': 'false'})

    def test_validation_and_recovery_evidence_still_follow_collection(self):
        names = list(self.steps)
        self.assertLess(names.index('Collect results or perform the requested maintenance'),
                        names.index('Validate the resulting runtime records'))
        validation = self.steps['Validate the resulting runtime records']
        self.assertEqual(validation['run'], '.venv/bin/python -m berean_translation validate')
        self.assertNotIn('if', validation)
        self.assertNotIn('continue-on-error', validation)
        evidence = self.steps['Retain recovery evidence after a failed checkpoint']
        self.assertEqual(evidence['if'], 'failure()')
        self.assertEqual(evidence['with']['path'].splitlines(),
                         ['state/', 'content/', 'index.json', 'STATUS.md', 'RECOVERY.json'])

    def test_source_automation_does_not_notify_or_deploy_the_website(self):
        for path in (ROOT / '.github').rglob('*'):
            if path.suffix not in ('.yml', '.yaml', '.py'):
                continue
            text = path.read_text(encoding='utf-8')
            for obsolete in ('REMNANT_', 'remnant-content-updated', '.github/remnant',
                             'remnant-notif', 'remnant-export', 'repository-dispatch',
                             'repository_dispatch', '/dispatches', 'gh workflow run',
                             'actions/deploy-pages'):
                with self.subTest(path=path.relative_to(ROOT), obsolete=obsolete):
                    self.assertNotIn(obsolete, text)
        self.assertFalse((ROOT / '.github/remnant/dispatch.py').exists())
        self.assertFalse((ROOT / '.github/remnant/validate_event.py').exists())

    def test_website_setup_documentation_has_no_obsolete_prerequisites(self):
        self.assertFalse((ROOT / 'docs/remnant-notifications.md').exists())
        for path in [ROOT / 'README.md', *(ROOT / 'docs').rglob('*.md')]:
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('REMNANT_', text, str(path))
            self.assertNotIn('remnant-notifications.md', text, str(path))
        self.assertIn('hourly', (ROOT / 'README.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
