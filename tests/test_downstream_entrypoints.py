"""Offline manual repair queue and Actions wiring checks; no real API calls."""
from __future__ import annotations
import base64
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from berean_translation.cli import main
from berean_translation.common import ContractError, canonical, loads
from berean_translation.queue import enqueue_github
from support import REPO_ROOT, setup


class DownstreamEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, *_ = setup(self.root)
        self.env = {
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': '202',
            'GITHUB_REPOSITORY': 'fixture/repo', 'GITHUB_ACTOR': 'fixture-owner',
            'GH_TOKEN': 'fixture-no-network', 'TRANSLATION_OPERATION': 'repair',
            'TRANSLATION_SELECTION': 'downstream-recovery',
            'INPUT_MAX_ARTICLES': '3', 'INPUT_MODEL': 'gpt-6.1-sol',
            'INPUT_REVIEW_MODEL': 'gpt-6.1-sol', 'INPUT_BUDGET_USD': '1',
            'INPUT_DRY_RUN': 'true',
        }

    def cli(self, env):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch(
                'berean_translation.cli.enqueue_github', return_value={'queued': True}) as enqueue:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = main(['--root', str(self.root), 'enqueue-env'])
        return result, enqueue, errors.getvalue()

    def request(self, **changes):
        request = {
            'id': 'repair-1', 'operation': 'repair', 'max_articles': 3,
            'model': 'gpt-6.1-sol', 'review_model': 'gpt-6.1-sol',
            'budget_usd': '1', 'dry_run': True,
        }
        request.update(changes)
        return request

    def test_manual_env_enqueues_bounded_repair_without_broad_defaults(self):
        result, enqueue, errors = self.cli(self.env)
        self.assertEqual(result, 0, errors)
        enqueue.assert_called_once()
        request = enqueue.call_args.args[1]
        self.assertEqual(request['id'], 'gh-202')
        self.assertEqual(request['operation'], 'repair')
        self.assertEqual(request['max_articles'], 3)
        self.assertEqual(request['model'], 'gpt-6.1-sol')
        self.assertEqual(request['review_model'], 'gpt-6.1-sol')
        self.assertEqual(str(request['budget_usd']), '1')
        self.assertIs(request['dry_run'], True)
        self.assertEqual(request['requested_by'], 'fixture-owner')
        for field in ('languages', 'issues', 'article_ids', 'source_refresh',
                      'source_translation_keys', 'recovery_of_campaign', 'previous_task_ids'):
            self.assertNotIn(field, request)
        self.assertEqual(enqueue.call_args.args[2:], ('fixture/repo', 'fixture-no-network'))

    def test_missing_optional_inputs_default_to_safe_preview(self):
        env = {key: value for key, value in self.env.items() if key not in
               ('INPUT_DRY_RUN', 'INPUT_MAX_ARTICLES', 'INPUT_MODEL', 'INPUT_REVIEW_MODEL')}
        result, enqueue, errors = self.cli(env)
        self.assertEqual(result, 0, errors)
        request = enqueue.call_args.args[1]
        self.assertIs(request['dry_run'], True)
        self.assertEqual(request['max_articles'], 3)
        self.assertEqual(request['model'], 'gpt-6.1-sol')
        self.assertEqual(request['review_model'], 'gpt-6.1-sol')
        self.assertNotIn('languages', request)
        self.assertNotIn('issues', request)

    def test_manual_env_accepts_only_bounded_max_articles(self):
        for limit in ('1', '2', '3', '4', '5'):
            with self.subTest(limit=limit):
                result, enqueue, errors = self.cli({**self.env, 'INPUT_MAX_ARTICLES': limit})
                self.assertEqual(result, 0, errors)
                self.assertEqual(enqueue.call_args.args[1]['max_articles'], int(limit))
        for limit in ('', '0', '6', '-1', '1.5', 'true', 'all', '1,2'):
            with self.subTest(limit=limit):
                result, enqueue, _ = self.cli({**self.env, 'INPUT_MAX_ARTICLES': limit})
                self.assertEqual(result, 1)
                enqueue.assert_not_called()

    def test_env_refuses_conflicting_selectors_and_operation(self):
        conflicts = {
            'INPUT_LANGUAGE': 'all', 'INPUT_LANGUAGES': 'afr,deu',
            'INPUT_ISSUES': 'fixture-issue', 'INPUT_ISSUE_SELECTION': 'next',
            'INPUT_ORIGINAL_CAMPAIGN': 'original', 'INPUT_PREVIOUS_TASK_IDS': 'a' * 32,
            'INPUT_RETRY_FAILED': 'true', 'TRANSLATION_OPERATION': 'translate',
            'TRANSLATION_SELECTION': 'typo', 'GITHUB_REF': 'refs/heads/feature/untrusted',
        }
        for field, value in conflicts.items():
            with self.subTest(field=field):
                result, enqueue, _ = self.cli({**self.env, field: value})
                self.assertEqual(result, 1)
                enqueue.assert_not_called()

    def test_env_requires_explicit_positive_budget(self):
        for budget in ('', '0', '-1', 'NaN', 'Infinity', 'invalid'):
            with self.subTest(budget=budget):
                result, enqueue, _ = self.cli({**self.env, 'INPUT_BUDGET_USD': budget})
                self.assertEqual(result, 1)
                enqueue.assert_not_called()
        env = {key: value for key, value in self.env.items() if key != 'INPUT_BUDGET_USD'}
        result, enqueue, _ = self.cli(env)
        self.assertEqual(result, 1)
        enqueue.assert_not_called()

    def test_env_rejects_non_boolean_dry_run(self):
        for value in ('', '0', 'yes'):
            with self.subTest(value=value):
                result, enqueue, _ = self.cli({**self.env, 'INPUT_DRY_RUN': value})
                self.assertEqual(result, 1)
                enqueue.assert_not_called()

    def test_disabled_policy_allows_preview_but_rejects_paid_queue(self):
        policy = self.config.runtime['automatic_downstream_recovery']
        self.assertIs(policy['enabled'], False)
        self.assertEqual(policy['total_budget_usd'], 0)
        transport = Mock(return_value={'queued': True})
        result = enqueue_github(self.config, self.request(), 'fixture/repo', 'fixture', transport)
        self.assertEqual(result, {'queued': True})
        transport.assert_called_once()
        transport.reset_mock()
        with self.assertRaisesRegex(ContractError, '(?i)(disabled|enabled|budget|configured)'):
            enqueue_github(self.config, self.request(dry_run=False), 'fixture/repo', 'fixture', transport)
        transport.assert_not_called()

    def test_paid_cli_without_dispatch_provenance_rejects_before_enqueue(self):
        result, enqueue, errors = self.cli({**self.env, 'INPUT_DRY_RUN': 'false'})
        self.assertEqual(result, 1)
        self.assertIn('workflow_dispatch context', errors)
        enqueue.assert_not_called()

    def test_manual_authorization_cannot_be_published_to_another_repository(self):
        request = self.request(id='gh-202', dry_run=False, requested_by='fixture-owner',
            manual_authorization={'kind':'github_workflow_dispatch', 'repository':'fixture/repo',
                'workflow_ref':'fixture/repo/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id':'202', 'actor':'fixture-owner'})
        transport = Mock()
        with self.assertRaisesRegex(ContractError, 'different repository'):
            enqueue_github(self.config, request, 'fixture/other', 'fixture', transport)
        transport.assert_not_called()

    def test_queue_request_is_immutable_and_duplicate_is_idempotent(self):
        request, calls = self.request(), []

        def transport(method, url, token, body=None):
            calls.append((method, url, body))
            if method == 'PUT':
                self.assertEqual(loads(base64.b64decode(body['content'])), request)
                self.assertEqual(body['branch'], 'main')
                self.assertIn('/state/queue/repair-1.json', url)
                raise HTTPError(url, 422, 'already exists', {}, None)
            return {'content': base64.b64encode(canonical(request)).decode()}

        result = enqueue_github(self.config, request, 'fixture/repo', 'fixture', transport)
        self.assertTrue(result['already_queued'])
        self.assertEqual([call[0] for call in calls], ['PUT', 'GET'])

        def different(method, url, token, body=None):
            if method == 'PUT':
                raise HTTPError(url, 422, 'already exists', {}, None)
            return {'content': base64.b64encode(canonical({**request, 'max_articles': 4})).decode()}

        with self.assertRaisesRegex(ContractError, 'different inputs'):
            enqueue_github(self.config, request, 'fixture/repo', 'fixture', different)

    def test_invalid_queue_inputs_fail_before_network(self):
        changes = [
            {'max_articles': value} for value in (0, 6, -1, 1.5, True, None, [], '3', 'all')
        ] + [
            {'languages': 'all'}, {'issues': 'next'}, {'article_ids': ['fixture-article']},
            {'source_refresh': False}, {'source_translation_keys': {}},
            {'recovery_of_campaign': 'original'}, {'previous_task_ids': ['a' * 32]},
            {'retry_failed': True}, {'dry_run': 'true'}, {'model': 'unknown'},
            {'review_model': 'unknown'}, {'budget_usd': 0}, {'budget_usd': 'NaN'},
        ]
        for variant in changes:
            with self.subTest(variant=variant):
                transport = Mock()
                with self.assertRaises(ContractError):
                    enqueue_github(self.config, self.request(**variant), 'fixture/repo', 'fixture', transport)
                transport.assert_not_called()

    def test_pending_repair_does_not_suppress_unrelated_source_refresh(self):
        from berean_translation.refresh import _pending_covers
        article = {'id': 'fixture-article', 'issue_id': 'fixture-issue'}
        for dry_run in (True, False):
            for language in ('afr', 'deu'):
                with self.subTest(dry_run=dry_run, language=language):
                    self.assertFalse(_pending_covers(self.config, {'issues': []},
                        self.request(dry_run=dry_run), language, article))

    def test_workflow_exposes_only_safe_repair_inputs(self):
        import yaml
        workflow = yaml.load((REPO_ROOT / '.github/workflows/ai-repair.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(workflow['name'], 'AI — Repair held translations')
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs), {'model', 'review_model', 'max_articles', 'budget_usd', 'dry_run', 'max_candidate_bytes'})
        choices = {'gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5-mini',
                   'gpt-4.1-mini', 'gpt-4.1-nano', 'gpt-4.1'}
        for field in ('model', 'review_model'):
            self.assertEqual(inputs[field]['type'], 'choice')
            self.assertEqual(set(inputs[field]['options']), choices)
            self.assertEqual(inputs[field]['default'], 'gpt-6.1-sol')
        self.assertEqual(inputs['max_articles']['options'], ['1', '2', '3', '4', '5'])
        self.assertEqual(inputs['max_articles']['default'], '3')
        self.assertEqual(inputs['budget_usd']['type'], 'string')
        self.assertEqual(inputs['budget_usd']['required'], 'true')
        self.assertEqual(inputs['budget_usd']['default'], '1')
        self.assertEqual(inputs['dry_run']['type'], 'boolean')
        self.assertEqual(inputs['dry_run']['default'], 'true')

    def test_workflow_preserves_queue_and_trusted_collector_boundaries(self):
        import yaml
        workflow = yaml.load((REPO_ROOT / '.github/workflows/ai-repair.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.assertNotIn('concurrency', workflow)
        self.assertEqual(workflow['permissions'], {'contents': 'write'})
        job = workflow['jobs']['enqueue']
        self.assertEqual(job['if'], "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'")
        self.assertEqual(job['steps'][0]['with'], {'ref': 'main', 'persist-credentials': 'false'})
        enqueue = job['steps'][1]
        self.assertEqual(enqueue['run'], 'python3 -m berean_translation enqueue-env')
        self.assertEqual(enqueue['env'], {
            'GH_TOKEN': '${{ github.token }}', 'TRANSLATION_OPERATION': 'repair',
            'TRANSLATION_SELECTION': 'downstream-recovery',
            'TRANSLATION_CONTINUATION_VERSION': '2',
            'INPUT_MAX_CANDIDATE_BYTES': '${{ inputs.max_candidate_bytes }}',
            'INPUT_MAX_ARTICLES': '${{ inputs.max_articles }}',
            'INPUT_MODEL': '${{ inputs.model }}', 'INPUT_REVIEW_MODEL': '${{ inputs.review_model }}',
            'INPUT_BUDGET_USD': '${{ inputs.budget_usd }}', 'INPUT_DRY_RUN': '${{ inputs.dry_run }}',
        })
        worker = yaml.load((REPO_ROOT / '.github/workflows/ai-worker.yml').read_text(),
                           Loader=yaml.BaseLoader)
        self.assertIn(workflow['name'], worker['on']['workflow_run']['workflows'])
        self.assertEqual(worker['concurrency'], {
            'group': 'berean-translation-state-writer', 'cancel-in-progress': 'false'})
        for guard in ("conclusion == 'success'", "head_branch == 'main'",
                      'head_repository.full_name == github.repository'):
            self.assertIn(guard, worker['jobs']['worker']['if'])


if __name__ == '__main__':
    unittest.main()
