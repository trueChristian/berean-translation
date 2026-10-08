"""Keep full offline CI gates within a measured, bounded runner window."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowCITests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.load((ROOT / '.github/workflows/ci.yml').read_text(),
                                  Loader=yaml.BaseLoader)
        self.job = self.workflow['jobs']['test']
        self.steps = {step['name']: step for step in self.job['steps']}

    def test_measured_full_suite_has_bounded_validation_headroom(self):
        self.assertEqual(self.job['timeout-minutes'], '25')
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        self.assertNotIn('pull_request_target', self.workflow['on'])
        self.assertEqual(self.workflow['concurrency']['cancel-in-progress'], 'true')

    def test_no_validation_gate_is_skipped_or_softened(self):
        commands = '\n'.join(step.get('run', '') for step in self.job['steps'])
        for command in ('python -m unittest discover -s tests -v',
                        'python -m berean_translation validate --recognize-human-edits',
                        'python -m berean_translation discover --check-only',
                        'python -m berean_translation.check_source --checkout .build/source'):
            self.assertIn(command, commands)
        self.assertNotIn('continue-on-error', self.job)
        for step in self.job['steps']:
            self.assertNotIn('continue-on-error', step)
            if 'run' in step:
                self.assertNotIn('if', step)
        self.assertIn('set -euo pipefail',
                      self.steps['Run the offline regression and SDK contract tests']['run'])

    def test_evidence_survives_failures_without_provider_credentials(self):
        upload = self.steps['Upload test evidence']
        self.assertEqual(upload['if'], 'always()')
        self.assertEqual(upload['with']['path'], '.build/ci/')
        self.assertEqual(upload['with']['retention-days'], '14')
        self.assertNotIn('OPENAI_API_KEY', str(self.workflow))
        self.assertNotIn('secrets.', str(self.workflow))


if __name__ == '__main__':
    unittest.main()
