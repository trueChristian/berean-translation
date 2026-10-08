"""Offline ordinary workflow inputs and immutable translation queue checks."""
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
from support import setup


class QueueEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, *_ = setup(self.root)
        self.env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': '101',
                    'GITHUB_REPOSITORY': 'fixture/repo', 'GITHUB_ACTOR': 'fixture-owner',
                    'GH_TOKEN': 'fixture-no-network', 'TRANSLATION_OPERATION': 'review',
                    'INPUT_MODEL': 'gpt-5-mini', 'INPUT_REVIEW_MODEL': 'gpt-5-mini',
                    'INPUT_BUDGET_USD': '0.30', 'INPUT_DRY_RUN': 'true'}

    def cli(self, env):
        with patch.dict(os.environ, env, clear=True), patch('berean_translation.cli.enqueue_github', return_value={'queued': True}) as enqueue:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = main(['--root', str(self.root), 'enqueue-env'])
            return result, enqueue


    def test_ordinary_env_preserves_selector_override_and_defaults(self):
        env = self.env
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

    def test_ordinary_env_rejects_untrusted_branch_and_invalid_dry_run(self):
        for changes in ({'GITHUB_REF': 'refs/heads/feature'}, {'INPUT_DRY_RUN': 'yes'}):
            with self.subTest(changes=changes):
                result, enqueue = self.cli({**self.env, **changes})
                self.assertEqual(result, 1)
                enqueue.assert_not_called()

    def request(self):
        return {'id': 'review-1', 'operation': 'review', 'languages': 'afr', 'issues': 'next',
                'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
                'budget_usd': 0.3, 'dry_run': True}

    def test_queue_request_is_immutable_and_idempotent(self):
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

    def test_malformed_queue_inputs_never_make_network_call(self):
        variants = [{'languages': 'unknown'}, {'model': 'unknown'}, {'review_model': 'unknown'},
                    {'budget_usd': 0}, {'operation': 'unknown'}, {'id': 'invalid identity'}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                enqueue_github(self.config, {**self.request(), **changes}, 'fixture/repo', 'fixture',
                               lambda *args: self.fail('Malformed request reached network'))


if __name__ == '__main__':
    unittest.main()
