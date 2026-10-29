"""Exercise uncapped recovery with offline, fully simulated provider calls."""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from berean_translation import continuation
from berean_translation.common import ContractError
from berean_translation.config import Config
from berean_translation.downstream import accept, funding_ledger
from berean_translation.state import TERMINAL
from berean_translation.validation import validate_repository
from support import A, B, REPO_ROOT, FakeProvider, drive, queue, setup


class ApprovedRecoveryBudgetTests(unittest.TestCase):
    def test_repository_policy_removes_money_limits_and_keeps_quality_controls(self):
        config = Config(REPO_ROOT)
        self.assertEqual(config.runtime['automatic_downstream_recovery'], {
            'enabled': True,
            'max_articles': 3, 'model': 'gpt-6.1-sol', 'review_model': 'gpt-6.1-sol',
            'max_output_tokens': 32768, 'review_output_tokens': 8192,
        })
        self.assertEqual(continuation.policy(), {
            'version': 2, 'max_cycles': 3, 'max_strategy_cycles': 2,
            'cooldown_seconds': 3600, 'max_candidate_bytes': 120000,
        })
        self.assertEqual(config.runtime['quality_threshold'], 95)
        self.assertEqual(config.runtime['max_translation_attempts'], 2)
        self.assertIs(config.runtime['automatic_new_translation'], True)
        self.assertNotIn('total_budget_usd', config.runtime['autonomous_translation'])
        self.assertNotIn('max_envelope_usd', config.runtime['autonomous_translation'])
        self.assertNotIn('max_campaign_usd', config.runtime)
        self.assertNotIn('default_budget_usd', config.runtime)

    def test_recovery_continues_past_retired_lifetime_cap_and_preserves_history(self):
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Real network access forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.config, self.state, self.source, self.provider,
         self.git, self.engine) = setup(self.root)
        self.assertIsInstance(self.provider, FakeProvider)
        self.assertIs(self.config.runtime['automatic_downstream_recovery']['enabled'], False)

        # Build real held history through generation and review, with enough
        # distinct pairs to cross the old cap before selection runs out of work.
        queue(self.state, 'original', languages='all', budget_usd=50)
        drive(self.engine, self.provider, self.reject)
        originals = {task['id']: task for task in self.state.tasks()}
        self.assertEqual({(task['language'], task['article_id']) for task in originals.values()},
                         {(language, article) for language in self.config.languages for article in (A, B)})
        self.assertTrue(all(task['status'] == 'not_ready'
                            and task['failure_kind'] == 'quality_rejection'
                            for task in originals.values()))
        self.assertFalse(self.state.projection(self.config)['articles'])

        # A previously authorized manual envelope exists before activation. Its
        # pending children also exercise the shared one-active-task-per-pair gate.
        repository = 'trueChristian/berean-translation'
        manual_request = {
            'id': 'gh-37045372894', 'operation': 'repair', 'model': 'gpt-6-luna',
            'review_model': 'gpt-6-luna', 'budget_usd': 10, 'max_articles': 3,
            'dry_run': False, 'requested_by': 'fixture-owner',
            'continuation_policy': continuation.policy(), 'manual_authorization': {
                'kind': 'github_workflow_dispatch', 'repository': repository,
                'workflow_ref': repository + '/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id': '37045372894', 'actor': 'fixture-owner',
            },
        }
        self.state.write('state/queue/' + manual_request['id'] + '.json', manual_request)
        manual = accept(self.engine, manual_request)
        self.assertTrue(manual['tasks'])
        self.assertEqual(manual['downstream_allocation_usd'], 0)
        self.assert_funding(0)

        # Use the actual repository policy without relaxing technical or quality checks.
        approved = copy.deepcopy(Config(REPO_ROOT).runtime['automatic_downstream_recovery'])
        self.config.runtime['automatic_downstream_recovery'] = copy.deepcopy(approved)
        self.state.write('config/runtime.json', self.config.runtime)
        self.assertEqual(Config(self.root).runtime['automatic_downstream_recovery'], approved)
        self.assertIs(approved['enabled'], True)
        self.assertNotIn('total_budget_usd', approved)
        self.assertNotIn('campaign_budget_usd', approved)
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        scheduled = []
        used_pairs = {(item['language'], item['article_id']) for item in manual['selection']}
        manual_snapshot = None
        for ordinal in range(1, 11):
            with self.subTest(hour=ordinal):
                at = start + timedelta(hours=ordinal)
                request = self.historical_request(at)
                identity = request['id']
                self.state.write(f'state/queue/{identity}.json', request)
                self.assertEqual(request['continuation_policy'], continuation.policy())
                self.assertEqual({key: request[key] for key in (
                    'model', 'review_model', 'max_articles', 'budget_usd')}, {
                    'model': approved['model'], 'review_model': approved['review_model'],
                    'max_articles': approved['max_articles'], 'budget_usd': 0,
                })
                with patch('berean_translation.downstream.now', return_value=at.isoformat()):
                    campaign = accept(self.engine, request)
                self.assertEqual(accept(self.engine, copy.deepcopy(request)), campaign)
                self.assertGreater(len(campaign['tasks']), 0, campaign.get('skipped'))
                self.assertLessEqual(len(campaign['tasks']), 3)
                self.assertEqual((campaign['funding_scope'], campaign['allocation_index'],
                    campaign['allocation_before_usd'], campaign['approved_total_usd'],
                    campaign['budget_usd'], campaign['downstream_allocation_usd']),
                    ('shared_policy', ordinal, 0, None, 0, 0))
                for item in campaign['selection']:
                    pair = (item['language'], item['article_id'])
                    self.assertNotIn(pair, used_pairs)
                    used_pairs.add(pair)
                    self.assertEqual(item['continuation']['cycle'], 1)
                    previous = originals[item['previous_task_id']]
                    self.assertEqual(item['source_snapshot'], previous['source_snapshot'])
                active = [(task['language'], task['article_id']) for task in self.state.tasks()
                          if task['status'] not in TERMINAL]
                self.assertEqual(len(active), len(set(active)))
                self.assert_funding(ordinal)

                if ordinal == 1:
                    calls = self.provider.create_calls
                    self.engine.cancel_campaign(identity)
                    self.assertEqual(self.provider.create_calls, calls)
                    self.assertTrue(all(self.task(task_id)['status'] == 'cancelled'
                                        for task_id in campaign['tasks']))
                    self.assertEqual(self.state.read(f'state/campaigns/{identity}.json')['reported_usage_usd'], 0)
                    self.finish(manual)
                    manual_snapshot = self.state.path(f'state/campaigns/{manual["id"]}.json').read_bytes()
                else:
                    self.finish(campaign)
                    actual = self.state.read(f'state/campaigns/{identity}.json')['reported_usage_usd']
                    self.assertGreater(actual, 0)
                    self.assertLess(actual, 1)
                scheduled.append(identity)
                self.assert_funding(ordinal)

        # Audited history survives cancellation and subsequent admission.
        # The next run still proceeds after the old ten-run lifetime boundary.
        self.assertEqual(len(scheduled), 10)
        self.assertEqual(self.state.path(f'state/campaigns/{manual["id"]}.json').read_bytes(), manual_snapshot)
        self.assertEqual(self.config.runtime['automatic_downstream_recovery'], approved)
        self.assertEqual(funding_ledger(self.state)['shared_policy_usd'], 0)
        calls = self.provider.create_calls
        before = {path.name: path.read_bytes() for path in self.state.path('state/queue').glob('*.json')}
        blocked_at = start + timedelta(hours=11)
        blocked = self.historical_request(blocked_at)
        blocked_id = blocked['id']
        self.assertIsNone(self.state.read(f'state/queue/{blocked_id}.json'))
        self.assertEqual(before, {path.name: path.read_bytes()
                                 for path in self.state.path('state/queue').glob('*.json')})
        resumed = accept(self.engine, blocked)
        self.assertTrue(resumed['tasks'])
        self.assertEqual(self.provider.create_calls, calls)
        self.finish(resumed)
        self.assert_funding(11)
        for identity, original in originals.items():
            self.assertEqual(self.task(identity), original)
        self.state.derive(self.config)
        validate_repository(self.config)

    def reject(self, line):
        if ':review' in line['custom_id']:
            # A high numerical score cannot override a major fidelity finding.
            return {'score': 97, 'passed': True, 'findings': [{
                'severity': 'major', 'location': 'p', 'source_quote': 'Faith',
                'translation_quote': 'Changed fixture wording', 'suggested_fix': 'Preserve faith',
            }]}
        result = self.provider.default_result(line)
        result['html'] = result['html'].replace('Faith and', 'Changed fixture wording and')
        return result

    def task(self, identity):
        return self.state.read(f'state/tasks/{identity}/task.json')

    def historical_request(self, at):
        policy = self.config.runtime['automatic_downstream_recovery']
        return {'id': 'downstream-' + at.strftime('%Y%m%d%H'), 'operation': 'repair',
                'model': policy['model'], 'review_model': policy['review_model'],
                'budget_usd': 0, 'max_articles': policy['max_articles'],
                'dry_run': False, 'scheduled_hour': at.strftime('%Y-%m-%dT%H'),
                'requested_by': 'historical-authority-fixture',
                'continuation_policy': continuation.policy()}

    def assert_funding(self, accepted):
        funding = funding_ledger(self.state)
        self.assertEqual((funding['shared_policy_count'], funding['shared_policy_usd']), (accepted, 0))
        self.assertEqual((funding['manual_workflow_count'], funding['manual_workflow_usd']), (1, 0))

    def finish(self, campaign):
        calls = self.provider.create_calls
        self.engine.prepare()
        self.provider.complete_all()
        self.engine.collect()
        for identity in campaign['tasks']:
            task = self.task(identity)
            self.assertEqual(task['stage'], 'review2')
            self.assertIsNone(self.state.record(task['language'], task['article_id'])['published'])
        self.engine.prepare()
        self.provider.complete_all()
        self.engine.collect()
        self.engine.prepare()
        self.assertEqual(self.provider.create_calls, calls + 2)
        for identity in campaign['tasks']:
            task = self.task(identity)
            self.assertEqual(task['status'], 'complete')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
