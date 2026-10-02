"""Offline verification of affordable defaults, frozen requests, and Luna costs."""
from __future__ import annotations

import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from berean_translation.cli import main
from berean_translation.common import ContractError, read_json
from berean_translation.requests import build_request, reserve_cost, usage_cost
from support import REPO_ROOT, queue, setup


LUNA = 'gpt-6-luna'


def workflow_inputs(name):
    document = yaml.load((REPO_ROOT / '.github/workflows' / name).read_text(),
                         Loader=yaml.BaseLoader)
    return document['on']['workflow_dispatch']['inputs']


class LunaRegistryTests(unittest.TestCase):
    def setUp(self):
        self.models = read_json(REPO_ROOT / 'config/models.json')
        self.model = self.models[LUNA]

    def test_verified_luna_contract_and_batch_rates(self):
        self.assertEqual(self.model['api_model'], LUNA)
        self.assertEqual(self.model['context_tokens'], 1050000)
        self.assertEqual(self.model['max_output_tokens'], 128000)
        self.assertEqual(self.model['reasoning_effort'], 'low')
        self.assertEqual(self.model['pricing_verified_on'], '2026-10-02')
        self.assertEqual(tuple(self.model[key] for key in (
            'input_batch_usd_per_million', 'cached_input_batch_usd_per_million',
            'cache_write_batch_usd_per_million', 'output_batch_usd_per_million')),
            (0.05, 0.005, 0.0625, 0.25))
        self.assertEqual(self.model['long_context_threshold_tokens'], 272000)
        self.assertEqual(self.model['long_context_input_multiplier'], 2)
        self.assertEqual(self.model['long_context_output_multiplier'], 1.5)

    def test_reservation_covers_premium_cache_writes_and_is_affordable(self):
        self.assertEqual(reserve_cost(self.model, 100000, 32768), 0.014442)
        for older in ('gpt-4.1-mini', 'gpt-5-mini', 'gpt-6.1-sol'):
            with self.subTest(model=older):
                self.assertLess(reserve_cost(self.model, 100000, 32768),
                                reserve_cost(self.models[older], 100000, 32768))

    def test_long_tier_applies_to_whole_request_only_above_boundary(self):
        self.assertEqual(reserve_cost(self.model, 272000, 8192), 0.019048)
        self.assertEqual(reserve_cost(self.model, 272001, 8192), 0.037073)
        for tokens, expected in ((272000, 0.015648), (272001, 0.0302721)):
            with self.subTest(input_tokens=tokens):
                self.assertEqual(usage_cost(self.model, {
                    'prompt_tokens': tokens, 'completion_tokens': 8192,
                    'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}),
                    expected)

    def test_cache_categories_are_disjoint_and_reasoning_is_not_added_twice(self):
        self.assertEqual(usage_cost(self.model, {
            'prompt_tokens': 100000, 'completion_tokens': 10000,
            'prompt_tokens_details': {'cached_tokens': 30000, 'cache_write_tokens': 20000},
            'completion_tokens_details': {'reasoning_tokens': 8000}}), 0.0064)
        self.assertEqual(usage_cost(self.model, {
            'prompt_tokens': 300000, 'completion_tokens': 10000,
            'prompt_tokens_details': {'cached_tokens': 250000, 'cache_write_tokens': 50000}}),
            0.0125)

    def test_missing_cache_counts_keep_conservative_cost_and_missing_usage_is_unknown(self):
        self.assertEqual(usage_cost(self.model, {
            'prompt_tokens': 100000, 'completion_tokens': 10000}), 0.00875)
        self.assertEqual(usage_cost(self.model, {
            'prompt_tokens': 100000, 'completion_tokens': 10000,
            'prompt_tokens_details': {'cached_tokens': 90000, 'cache_write_tokens': 20000}}),
            0.00875)
        self.assertIsNone(usage_cost(self.model, {'prompt_tokens': 100000}))

    def test_every_workflow_picker_matches_registry_and_preserves_recovery_defaults(self):
        for filename, default in (
            ('ai-translate.yml', LUNA), ('ai-review.yml', LUNA),
            ('ai-recover.yml', 'gpt-5-mini'), ('ai-repair.yml', 'gpt-6.1-sol'),
        ):
            inputs = workflow_inputs(filename)
            for field in ('model', 'review_model'):
                with self.subTest(workflow=filename, field=field):
                    self.assertEqual(set(inputs[field]['options']), set(self.models))
                    self.assertEqual(len(inputs[field]['options']), len(self.models))
                    self.assertEqual(inputs[field]['default'], default)
            self.assertEqual(inputs['dry_run']['default'], 'true')

    def test_runtime_defaults_do_not_expand_standing_automatic_work(self):
        runtime = read_json(REPO_ROOT / 'config/runtime.json')
        self.assertEqual(runtime['default_model'], LUNA)
        self.assertEqual(runtime['default_review_model'], LUNA)
        self.assertFalse(runtime['automatic_new_translation'])
        self.assertEqual(runtime['automatic_source_refresh'], {
            'enabled': True, 'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
            'budget_usd': 10, 'max_campaigns_per_tick': 5})
        self.assertEqual(runtime['automatic_downstream_recovery'], {
            'enabled': False, 'total_budget_usd': 0, 'campaign_budget_usd': 1,
            'max_articles': 3, 'model': 'gpt-6.1-sol', 'review_model': 'gpt-6.1-sol',
            'max_output_tokens': 32768, 'review_output_tokens': 8192})
        self.assertEqual(runtime['max_translation_attempts'], 2)
        self.assertEqual(runtime['quality_threshold'], 95)


class LunaRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, _, self.provider, _, self.engine = setup(self.root)
        self.engine.discover()

    def accept(self, **overrides):
        request = queue(self.state, model=None, review_model=None, **overrides)
        campaign = self.engine.accept_request(request)
        return campaign, self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')

    def test_omitted_models_select_luna_and_freeze_reasoning_headroom(self):
        campaign, task = self.accept()
        self.assertEqual((campaign['model'], campaign['review_model']), (LUNA, LUNA))
        self.assertEqual((task['model'], task['review_model']), (LUNA, LUNA))
        self.assertEqual(campaign['max_output_tokens'], 16384)
        self.assertEqual(campaign['review_output_tokens'], 8192)
        self.assertEqual(campaign['budget_usd'], 5)
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_enqueue_env_default_and_explicit_override_keep_dry_run_and_budget(self):
        env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': 'luna-test',
               'GITHUB_REPOSITORY': 'fixture/repo', 'GITHUB_ACTOR': 'fixture-owner',
               'GH_TOKEN': 'fixture-no-network', 'TRANSLATION_OPERATION': 'translate',
               'INPUT_LANGUAGE': 'afr', 'INPUT_BUDGET_USD': '0.5'}
        for overrides, expected in (({}, (LUNA, LUNA)),
                                    ({'INPUT_MODEL': 'gpt-5-mini',
                                      'INPUT_REVIEW_MODEL': 'gpt-4.1-mini'},
                                     ('gpt-5-mini', 'gpt-4.1-mini'))):
            with self.subTest(overrides=overrides):
                with patch.dict(os.environ, {**env, **overrides}, clear=True), patch(
                        'berean_translation.cli.enqueue_github', return_value={'queued': True}) as enqueue:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(['--root', str(self.root), 'enqueue-env']), 0)
                request = enqueue.call_args.args[1]
                self.assertEqual((request['model'], request['review_model']), expected)
                self.assertEqual(request['budget_usd'], '0.5')
                self.assertIs(request['dry_run'], True)
                self.assertIs(request['retry_failed'], False)

    def test_translation_and_both_reviews_use_frozen_batch_contract_and_cost(self):
        campaign, task = self.accept()
        source = self.state.source(task)
        self.state.save_candidate(task, {'html': source['html'], **{
            key: source['article'].get(key) for key in ('title', 'subtitle', 'section')}})
        for stage, output_limit in (('translate', 16384), ('review1', 8192), ('review2', 8192)):
            with self.subTest(stage=stage):
                task['stage'] = stage
                line, cost, bound = build_request(self.config, self.state, task)
                body = line['body']
                self.assertEqual(line['url'], '/v1/chat/completions')
                self.assertEqual(body['model'], LUNA)
                self.assertEqual(body['reasoning_effort'], 'low')
                self.assertEqual(body['max_completion_tokens'], output_limit)
                self.assertEqual(body['response_format']['type'], 'json_schema')
                self.assertIs(body['response_format']['json_schema']['strict'], True)
                self.assertNotIn('temperature', body)
                self.assertNotIn('tools', body)
                self.assertEqual(cost, reserve_cost(task['models'][LUNA], bound, output_limit))
        before = build_request(self.config, self.state, task)
        self.config.models[LUNA]['cache_write_batch_usd_per_million'] = 999
        self.config.models[LUNA]['reasoning_effort'] = 'high'
        self.config.runtime['reasoning_review_output_tokens'] = 3000
        self.assertEqual(build_request(self.config, self.state, task), before)
        self.assertEqual(self.state.read(f'state/campaigns/{campaign["id"]}.json'), campaign)

    def test_accepted_legacy_campaign_and_task_are_not_rewritten(self):
        luna = self.config.models.pop(LUNA)
        self.config.runtime['default_model'] = 'gpt-4.1-mini'
        self.config.runtime['default_review_model'] = 'gpt-4.1-mini'
        campaign, task = self.accept()
        paths = [self.state.path(f'state/campaigns/{campaign["id"]}.json'),
                 self.state.path(f'state/tasks/{task["id"]}/task.json')]
        original = [path.read_bytes() for path in paths]
        request_before = build_request(self.config, self.state, task)
        self.config.models[LUNA] = luna
        self.config.runtime['default_model'] = LUNA
        self.config.runtime['default_review_model'] = LUNA
        self.assertEqual(self.engine.accept_request(self.state.read('state/queue/request-1.json')), campaign)
        self.assertEqual([path.read_bytes() for path in paths], original)
        self.assertEqual(build_request(self.config, self.state, task), request_before)
        self.assertNotIn(LUNA, task['models'])

    def test_luna_still_blocks_before_upload_when_budget_is_too_small(self):
        queue(self.state, model=LUNA, review_model=LUNA, budget_usd=0.000001)
        self.engine.tick()
        self.assertTrue(self.state.tasks())
        self.assertTrue(all(task['status'] == 'budget_blocked' for task in self.state.tasks()))
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_luna_context_check_includes_full_review_output_cap(self):
        _, task = self.accept()
        source = self.state.source(task)
        self.state.save_candidate(task, {'html': source['html'], **{
            key: source['article'].get(key) for key in ('title', 'subtitle', 'section')}})
        task['stage'] = 'review1'
        _, _, bound = build_request(self.config, self.state, task)
        task = copy.deepcopy(task)
        task['models'][LUNA]['context_tokens'] = bound + 8191
        with self.assertRaisesRegex(ContractError, 'context'):
            build_request(self.config, self.state, task)


if __name__ == '__main__':
    unittest.main()
