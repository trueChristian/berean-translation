"""Offline queue/Actions wiring checks for exact recovery, never a live request."""
from __future__ import annotations
import base64
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from berean_translation.cli import main
from berean_translation.common import ContractError, canonical, loads
from berean_translation.queue import enqueue_github
from support import REPO_ROOT, setup


class RecoveryEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, *_ = setup(self.root)
        self.env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': '101',
                    'GITHUB_REPOSITORY': 'fixture/repo', 'GITHUB_ACTOR': 'fixture-owner',
                    'GH_TOKEN': 'fixture-no-network', 'TRANSLATION_OPERATION': 'review',
                    'TRANSLATION_SELECTION': 'exact-recovery',
                    'INPUT_ORIGINAL_CAMPAIGN': 'original', 'INPUT_PREVIOUS_TASK_IDS': 'a'*32 + ',' + 'b'*32,
                    'INPUT_MODEL': 'gpt-5-mini', 'INPUT_REVIEW_MODEL': 'gpt-5-mini',
                    'INPUT_BUDGET_USD': '0.30', 'INPUT_DRY_RUN': 'true'}

    def cli(self, env):
        with patch.dict(os.environ, env, clear=True), patch('berean_translation.cli.enqueue_github', return_value={'queued': True}) as enqueue:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = main(['--root', str(self.root), 'enqueue-env'])
            return result, enqueue

    def test_exact_env_never_adds_default_language_or_issue(self):
        result, enqueue = self.cli(self.env)
        self.assertEqual(result, 0)
        request = enqueue.call_args.args[1]
        self.assertEqual(request['previous_task_ids'], ['a'*32, 'b'*32])
        self.assertEqual(request['recovery_of_campaign'], 'original')
        self.assertTrue(request['dry_run'])
        self.assertEqual(request['budget_usd'], '0.30')
        self.assertEqual(request['model'], 'gpt-5-mini')
        self.assertEqual(request['review_model'], 'gpt-5-mini')
        for key in ('languages', 'issues', 'article_ids', 'source_refresh'):
            self.assertNotIn(key, request)

    def test_env_selector_conflicts_and_duplicates_fail_before_queue(self):
        for field, value in (('INPUT_LANGUAGE', 'all'), ('INPUT_LANGUAGES', 'afr'),
                             ('INPUT_ISSUES', 'anything'), ('INPUT_ISSUE_SELECTION', 'next'),
                             ('INPUT_PREVIOUS_TASK_IDS', 'a'*32 + ',' + 'a'*32),
                             ('INPUT_PREVIOUS_TASK_IDS', ''), ('TRANSLATION_SELECTION', 'typo')):
            with self.subTest(field=field, value=value):
                result, enqueue = self.cli({**self.env, field: value})
                self.assertEqual(result, 1)
                enqueue.assert_not_called()

    def test_ordinary_env_preserves_selector_override_and_defaults(self):
        env = {key: value for key, value in self.env.items() if key not in
               ('TRANSLATION_SELECTION', 'INPUT_ORIGINAL_CAMPAIGN', 'INPUT_PREVIOUS_TASK_IDS')}
        result, enqueue = self.cli(env)
        self.assertEqual(result, 0)
        request = enqueue.call_args.args[1]
        self.assertEqual((request['languages'], request['issues']), ('all', 'next'))
        self.assertNotIn('previous_task_ids', request)
        result, enqueue = self.cli({**env, 'INPUT_LANGUAGE': 'all', 'INPUT_LANGUAGES': 'afr,deu',
                                    'INPUT_ISSUE_SELECTION': 'custom', 'INPUT_ISSUES': 'issue-1,issue-2'})
        self.assertEqual(result, 0)
        request = enqueue.call_args.args[1]
        self.assertEqual((request['languages'], request['issues']), ('afr,deu', 'issue-1,issue-2'))
        for key in ('INPUT_ORIGINAL_CAMPAIGN', 'INPUT_PREVIOUS_TASK_IDS'):
            result, enqueue = self.cli({**env, key: ''})
            self.assertEqual(result, 1)
            enqueue.assert_not_called()

    def request(self):
        return {'id': 'recovery-1', 'operation': 'review', 'recovery_of_campaign': 'original',
                'previous_task_ids': ['a'*32], 'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
                'budget_usd': 0.3, 'dry_run': True}

    def test_queue_exact_request_is_immutable_and_idempotent(self):
        request = self.request()
        calls = []
        def transport(method, url, token, body=None):
            calls.append((method, url, body))
            if method == 'PUT':
                self.assertEqual(loads(base64.b64decode(body['content'])), request)
                raise HTTPError(url, 422, 'already exists', {}, None)
            return {'content': base64.b64encode(canonical(request)).decode()}
        result = enqueue_github(self.config, request, 'fixture/repo', 'fixture', transport)
        self.assertTrue(result['already_queued'])
        self.assertEqual([call[0] for call in calls], ['PUT', 'GET'])
        def changed(method, url, token, body=None):
            if method == 'PUT':
                raise HTTPError(url, 422, 'already exists', {}, None)
            return {'content': base64.b64encode(canonical({**request, 'budget_usd': 0.4})).decode()}
        with self.assertRaisesRegex(ContractError, 'different inputs'):
            enqueue_github(self.config, request, 'fixture/repo', 'fixture', changed)

    def test_malformed_exact_queue_inputs_never_make_network_call(self):
        variants = [{'languages': 'all'}, {'issues': 'next'}, {'previous_task_ids': []},
                    {'previous_task_ids': ['bad']}, {'recovery_of_campaign': ''},
                    {'operation': 'translate'}, {'source_refresh': False}, {'dry_run': 'true'}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                enqueue_github(self.config, {**self.request(), **changes}, 'fixture/repo', 'fixture',
                               lambda *args: self.fail('Malformed request reached network'))

    def test_pending_exact_recovery_cannot_suppress_unrelated_source_refreshes(self):
        from berean_translation.refresh import _pending_covers
        article = {'id': 'fixture-article', 'issue_id': 'fixture-issue'}
        source = {'issues': []}
        for dry_run in (True, False):
            request = {**self.request(), 'dry_run': dry_run}
            self.assertFalse(_pending_covers(self.config, source, request, 'afr', article))
            self.assertFalse(_pending_covers(self.config, source, request, 'deu', article))
        # Ordinary all/next review precedence is unchanged.
        self.assertTrue(_pending_covers(self.config, source,
            {'operation': 'review', 'languages': 'all', 'issues': 'next'}, 'afr', article))

    def test_workflow_exact_fields_and_trusted_collector_pickup(self):
        import yaml
        # BaseLoader avoids YAML 1.1 treating GitHub's `on` key as boolean True.
        workflow = yaml.load((REPO_ROOT / '.github/workflows/ai-recover.yml').read_text(), Loader=yaml.BaseLoader)
        inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs), {'original_campaign', 'previous_task_ids', 'model', 'review_model', 'budget_usd', 'dry_run'})
        self.assertLessEqual(len(inputs), 10)
        self.assertEqual(inputs['dry_run']['default'], 'true')
        self.assertEqual(inputs['model']['default'], 'gpt-5-mini')
        self.assertEqual(inputs['review_model']['default'], 'gpt-5-mini')
        self.assertNotIn('concurrency', workflow)
        job = workflow['jobs']['enqueue']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(job['steps'][0]['with']['ref'], 'main')
        env = job['steps'][1]['env']
        self.assertEqual(env['TRANSLATION_SELECTION'], 'exact-recovery')
        self.assertEqual(env['TRANSLATION_OPERATION'], 'review')
        self.assertNotIn('OPENAI_API_KEY', env)
        worker = yaml.load((REPO_ROOT / '.github/workflows/ai-worker.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertIn(workflow['name'], worker['on']['workflow_run']['workflows'])
        self.assertEqual(worker['concurrency']['group'], 'berean-translation-state-writer')
        self.assertEqual(worker['concurrency']['cancel-in-progress'], 'false')
        for guard in ("conclusion == 'success'", "head_branch == 'main'", 'head_repository.full_name == github.repository'):
            self.assertIn(guard, worker['jobs']['worker']['if'])


if __name__ == '__main__':
    unittest.main()
