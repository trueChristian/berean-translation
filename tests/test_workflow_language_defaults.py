"""Offline all-language workflow defaults, selector precedence, and spend guards."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from berean_translation.cli import main
from berean_translation.common import ContractError
from support import A, B, REPO_ROOT, drive, queue, setup


def workflow(name):
    return yaml.load((REPO_ROOT / '.github/workflows' / name).read_text(encoding='utf-8'),
                     Loader=yaml.BaseLoader)


class WorkflowLanguageDefaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, _, self.provider, _, self.engine = setup(self.root)

    def workflow_request(self, operation, **overrides):
        document = workflow(f'ai-{operation}.yml')
        inputs = document['on']['workflow_dispatch']['inputs']
        env = {f'INPUT_{name.upper()}': spec['default'] for name, spec in inputs.items()}
        env.update(GITHUB_REF='refs/heads/main', GITHUB_RUN_ID=operation,
                   GITHUB_REPOSITORY='fixture/repo', GITHUB_ACTOR='fixture-owner',
                   GH_TOKEN='fixture-no-network', TRANSLATION_OPERATION=operation)
        env.update(overrides)
        errors = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch(
                'berean_translation.cli.enqueue_github', return_value={'queued': True}) as enqueue:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                result = main(['--root', str(self.root), 'enqueue-env'])
        self.assertEqual(result, 0, errors.getvalue())
        enqueue.assert_called_once()
        return enqueue.call_args.args[1]

    def test_language_defaults_and_other_workflow_limits(self):
        for operation, budget in (('translate', '10'), ('review', '5')):
            with self.subTest(operation=operation):
                document = workflow(f'ai-{operation}.yml')
                inputs = document['on']['workflow_dispatch']['inputs']
                self.assertEqual(inputs['language']['default'], 'all')
                self.assertEqual(set(inputs['language']['options']), {'all', *self.config.languages})
                self.assertEqual(inputs['languages']['default'], '')
                self.assertEqual(inputs['issue_selection']['default'], 'next')
                self.assertEqual(inputs['issues']['default'], '')
                self.assertEqual(inputs['budget_usd']['default'], budget)
                self.assertEqual(inputs['dry_run']['default'], 'true')
                self.assertEqual(inputs['retry_failed']['default'], 'false')
                env = document['jobs']['enqueue']['steps'][-1]['env']
                for field in ('language', 'languages', 'budget_usd', 'dry_run', 'retry_failed'):
                    self.assertEqual(env[f'INPUT_{field.upper()}'], '${{ inputs.' + field + ' }}')
                request = self.workflow_request(operation)
                self.assertEqual(request['languages'], 'all')
                self.assertEqual(request['issues'], 'next')
                self.assertEqual(request['budget_usd'], budget)
                self.assertIs(request['dry_run'], True)
                self.assertIs(request['retry_failed'], False)

    def test_explicit_single_and_multiple_language_overrides_are_preserved(self):
        for operation in ('translate', 'review'):
            for single, multiple, expected in (
                    ('afr', '', ['afr']), ('deu', '  ', ['deu']),
                    ('all', 'afr,deu', ['afr', 'deu']), ('spa', 'afr,deu', ['afr', 'deu'])):
                with self.subTest(operation=operation, single=single, multiple=multiple):
                    request = self.workflow_request(operation, INPUT_LANGUAGE=single,
                                                    INPUT_LANGUAGES=multiple)
                    self.assertEqual(self.config.select_languages(request['languages']), expected)
                    self.assertIs(request['dry_run'], True)

    def assert_preview_does_not_spend(self, operation, expected_pairs):
        tasks_before = self.state.tasks()
        uploads_before = self.provider.upload_calls
        creates_before = self.provider.create_calls
        batches_before = self.state.batches()
        campaign = self.engine.accept_request(self.workflow_request(operation))
        self.engine.tick()
        self.assertEqual(set(campaign['languages']), set(self.config.languages))
        self.assertEqual({(item['language'], item['article_id']) for item in campaign['selection']},
                         expected_pairs)
        self.assertEqual(campaign['status'], 'planned')
        self.assertEqual(campaign['tasks'], [])
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(campaign['reported_usage_usd'], 0)
        self.assertEqual(campaign['budget_usd'], 10 if operation == 'translate' else 5)
        self.assertEqual(self.state.tasks(), tasks_before)
        self.assertEqual(self.state.batches(), batches_before)
        self.assertEqual(self.provider.upload_calls, uploads_before)
        self.assertEqual(self.provider.create_calls, creates_before)

    def test_default_translation_previews_all_languages_without_paid_work(self):
        self.engine.discover()
        self.assert_preview_does_not_spend('translate',
            {(language, article) for language in self.config.languages for article in (A, B)})

    def test_default_review_previews_only_eligible_pairs_without_paid_work(self):
        # Establish four publications with the offline provider. Other language
        # pairs must not become new translations through the review workflow.
        queue(self.state, 'seed-publications', languages='afr,deu')
        drive(self.engine, self.provider)
        self.assert_preview_does_not_spend('review',
            {(language, article) for language in ('afr', 'deu') for article in (A, B)})

    def test_all_language_selection_keeps_task_and_campaign_caps(self):
        self.engine.discover()
        request = self.workflow_request('translate')
        self.config.runtime['max_tasks_per_request'] = 39
        with self.assertRaisesRegex(ContractError, 'max_tasks_per_request'):
            self.engine.accept_request(request)
        self.assertEqual(self.state.campaigns(), [])
        self.config.runtime['max_tasks_per_request'] = 1000
        request['budget_usd'] = self.config.runtime['max_campaign_usd'] + 1
        with self.assertRaises(ContractError):
            self.engine.accept_request(request)
        self.assertEqual(self.state.campaigns(), [])
        self.assertEqual(self.state.tasks(), [])
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_explicit_paid_all_language_request_still_obeys_budget(self):
        request = self.workflow_request('translate', INPUT_DRY_RUN='false',
                                        INPUT_BUDGET_USD='0.000001')
        self.state.write(f'state/queue/{request["id"]}.json', request)
        drive(self.engine, self.provider)
        campaign = self.state.campaigns()[0]
        self.assertEqual(campaign['budget_usd'], 0.000001)
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(len(self.state.tasks()), 2 * len(self.config.languages))
        self.assertTrue(all(task['status'] == 'budget_blocked' for task in self.state.tasks()))
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_recovery_workflows_keep_their_bounded_non_language_selectors(self):
        for name, mode, expected_inputs in (
                ('ai-recover.yml', 'exact-recovery',
                 {'original_campaign', 'previous_task_ids', 'model', 'review_model', 'budget_usd', 'dry_run'}),
                ('ai-repair.yml', 'downstream-recovery',
                 {'model', 'review_model', 'max_articles', 'budget_usd', 'dry_run', 'max_candidate_bytes'})):
            with self.subTest(workflow=name):
                document = workflow(name)
                inputs = document['on']['workflow_dispatch']['inputs']
                self.assertEqual(set(inputs), expected_inputs)
                self.assertEqual(inputs['dry_run']['default'], 'true')
                self.assertEqual(inputs['budget_usd']['required'], 'true')
                env = document['jobs']['enqueue']['steps'][-1]['env']
                self.assertEqual(env['TRANSLATION_SELECTION'], mode)
                self.assertNotIn('INPUT_LANGUAGE', env)
                self.assertNotIn('INPUT_LANGUAGES', env)
        recover = workflow('ai-recover.yml')['on']['workflow_dispatch']['inputs']
        for field in ('original_campaign', 'previous_task_ids'):
            self.assertEqual(recover[field]['required'], 'true')
            self.assertNotIn('default', recover[field])
        repair = workflow('ai-repair.yml')['on']['workflow_dispatch']['inputs']
        self.assertEqual(repair['max_articles']['options'], ['1', '2', '3', '4', '5'])
        self.assertEqual(repair['max_articles']['default'], '3')
        self.assertEqual(repair['budget_usd']['default'], '1')


if __name__ == '__main__':
    unittest.main()
