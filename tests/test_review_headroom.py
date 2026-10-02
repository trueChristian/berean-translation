"""Offline coverage for frozen, model-aware review completion headroom."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError, canonical, read_json
from berean_translation.config import Config
from berean_translation.requests import build_request, reserve_cost
from support import queue, setup


class ReviewHeadroomTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.engine.discover()

    def accept(self, **changes):
        request = queue(self.state, **changes)
        return self.engine.accept_request(request)

    def reviews(self, campaign):
        tasks = []
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            source = self.state.source(task)
            self.state.save_candidate(task, {'html':source['html'], **{
                key:source['article'].get(key) for key in ('title','subtitle','section')}})
            task['stage'] = 'review1'
            self.state.save_task(task)
            tasks.append(task)
        return tasks

    def test_registered_reasoning_reviewers_get_8192_nonreasoning_3000(self):
        for name, model in self.config.models.items():
            with self.subTest(model=name):
                expected = 8192 if model.get('reasoning_effort') else 3000
                self.assertEqual(self.config.review_output_limit(name), expected)

    def test_review_model_not_translation_model_selects_the_frozen_limit(self):
        campaign = self.accept(model='gpt-5-mini', review_model='gpt-4.1-mini')
        self.assertEqual(campaign['review_output_tokens'], 3000)
        # A separate language avoids overlapping tasks in this fixture.
        request = queue(self.state, 'reasoning-reviewer', languages='deu',
                        model='gpt-4.1-mini', review_model='gpt-5-mini')
        campaign = self.engine.accept_request(request)
        self.assertEqual(campaign['review_output_tokens'], 8192)
        self.assertEqual(campaign['max_output_tokens'], 16384)

    def test_floor_never_reduces_base_and_model_max_is_respected(self):
        self.config.runtime['review_output_tokens'] = 12000
        self.assertEqual(self.config.review_output_limit('gpt-5-mini'), 12000)
        self.config.models['gpt-5-mini']['max_output_tokens'] = 2048
        self.assertEqual(self.config.review_output_limit('gpt-5-mini'), 2048)

    def test_new_cap_is_positive_integer_not_boolean_or_unbounded(self):
        path = self.root/'config/runtime.json'
        original = read_json(path)
        for value in (None, True, False, 0, -1, 8192.0, '8192', float('inf')):
            with self.subTest(value=value):
                changed = copy.deepcopy(original)
                changed['reasoning_review_output_tokens'] = value
                # JSON writer deliberately permits inf to exercise the loader too.
                import json
                path.write_text(json.dumps(changed))
                with self.assertRaises(ContractError):
                    Config(self.root)
        path.write_bytes(canonical(original))

    def test_both_review_stages_use_frozen_full_cap_for_request_and_reservation(self):
        campaign = self.accept(review_model='gpt-5-mini')
        task = self.reviews(campaign)[0]
        for stage in ('review1', 'review2'):
            with self.subTest(stage=stage):
                task['stage'] = stage
                line, cost, input_bound = build_request(self.config, self.state, task)
                self.assertEqual(line['body']['max_completion_tokens'], 8192)
                self.assertEqual(line['body']['reasoning_effort'], 'low')
                self.assertEqual(cost, reserve_cost(task['models']['gpt-5-mini'], input_bound, 8192))
                self.assertTrue(line['body']['response_format']['json_schema']['strict'])

    def test_accepted_legacy_requests_and_campaign_bytes_stay_unchanged(self):
        self.config.runtime['reasoning_review_output_tokens'] = 3000
        campaign = self.accept(review_model='gpt-5-mini')
        task = self.reviews(campaign)[0]
        path = self.state.path(f'state/campaigns/{campaign["id"]}.json')
        frozen = path.read_bytes()
        before = canonical(build_request(self.config, self.state, task))
        self.config.runtime['reasoning_review_output_tokens'] = 8192
        self.config.runtime['review_output_tokens'] = 7000
        self.config.models['gpt-5-mini']['reasoning_effort'] = 'high'
        self.assertEqual(canonical(build_request(self.config, self.state, task)), before)
        accepted_again = self.engine.accept_request(self.state.read('state/queue/request-1.json'))
        self.assertEqual(accepted_again, campaign)
        self.assertEqual(path.read_bytes(), frozen)
        self.assertEqual(build_request(self.config, self.state, task)[0]['body']['max_completion_tokens'], 3000)

    def test_full_headroom_is_included_in_context_check(self):
        campaign = self.accept(review_model='gpt-5-mini')
        task = self.reviews(campaign)[0]
        _, _, input_bound = build_request(self.config, self.state, task)
        task['models']['gpt-5-mini']['context_tokens'] = input_bound + 8191
        with self.assertRaisesRegex(ContractError, 'context'):
            build_request(self.config, self.state, task)

    def test_tight_envelope_blocks_before_upload_without_raising_budget(self):
        campaign = self.accept(review_model='gpt-5-mini')
        task = self.reviews(campaign)[0]
        full_cost = build_request(self.config, self.state, task)[1]
        campaign['review_output_tokens'] = 3000
        self.state.save_campaign(campaign)
        former_cost = build_request(self.config, self.state, task)[1]
        self.assertAlmostEqual(full_cost-former_cost, 0.005192, places=6)
        campaign['review_output_tokens'] = 8192
        campaign['budget_usd'] = former_cost
        self.state.save_campaign(campaign)
        self.engine.prepare()
        self.assertTrue(all(t['status'] == 'budget_blocked' for t in self.state.tasks()))
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)
        saved = self.state.read(f'state/campaigns/{campaign["id"]}.json')
        self.assertEqual(saved['budget_usd'], former_cost)
        self.assertEqual(saved['reserved_usd'], 0)

    def test_downstream_policy_remains_separate_disabled_and_aligned(self):
        policy = copy.deepcopy(self.config.runtime['automatic_downstream_recovery'])
        self.accept(review_model='gpt-6.1-sol')
        self.assertEqual(policy, self.config.runtime['automatic_downstream_recovery'])
        self.assertFalse(policy['enabled'])
        self.assertEqual(policy['total_budget_usd'], 0)
        self.assertEqual(policy['review_output_tokens'], self.config.review_output_limit('gpt-6.1-sol'))


if __name__ == '__main__':
    unittest.main()
