"""Offline immutable repair queue and funding checks for retained paid history."""
from __future__ import annotations
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from berean_translation.common import ContractError, canonical, loads
from berean_translation.queue import enqueue_github
from support import setup


class DownstreamEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, *_ = setup(self.root)


    def request(self, **changes):
        request = {
            'id': 'repair-1', 'operation': 'repair', 'max_articles': 3,
            'model': 'gpt-6.1-sol', 'review_model': 'gpt-6.1-sol',
            'budget_usd': '1', 'dry_run': True,
        }
        request.update(changes)
        return request


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


if __name__ == '__main__':
    unittest.main()
