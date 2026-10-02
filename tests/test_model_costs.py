"""Offline verification of frozen model pricing and hard request reservations."""
from __future__ import annotations
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from berean_translation.common import ContractError, read_json, write_json
from berean_translation.config import Config
from berean_translation.requests import build_request, reserve_cost, usage_cost
from support import REPO_ROOT, queue, setup


class ModelCostTests(unittest.TestCase):
    def setUp(self):
        self.models = read_json(REPO_ROOT/'config/models.json')

    def test_verified_new_model_registry(self):
        for name, rates in (
            ('gpt-6.1-sol', (1.0, 0.05, 1.25, 5.0)),
            ('gpt-6-astra', (5.0, 0.5, 6.25, 25.0)),
        ):
            with self.subTest(model=name):
                model = self.models[name]
                self.assertEqual(model['api_model'], name)
                self.assertEqual(tuple(model[key] for key in (
                    'input_batch_usd_per_million', 'cached_input_batch_usd_per_million',
                    'cache_write_batch_usd_per_million', 'output_batch_usd_per_million')), rates)
                self.assertEqual(model['context_tokens'], 1050000)
                self.assertEqual(model['max_output_tokens'], 128000)
                self.assertEqual(model['reasoning_effort'], 'low')
                self.assertEqual(model['long_context_threshold_tokens'], 272000)
                self.assertEqual(model['long_context_input_multiplier'], 2)
                self.assertEqual(model['long_context_output_multiplier'], 1.5)

    def test_reservations_cover_cache_writes_without_double_charging(self):
        self.assertEqual(reserve_cost(self.models['gpt-6.1-sol'], 100000, 32768), 0.28884)
        self.assertEqual(reserve_cost(self.models['gpt-6-astra'], 100000, 32768), 1.4442)

    def test_whole_request_long_context_tier_starts_above_272000(self):
        model = self.models['gpt-6.1-sol']
        self.assertEqual(reserve_cost(model, 272000, 8192), 0.38096)
        self.assertEqual(reserve_cost(model, 272001, 8192), 0.741443)
        self.assertEqual(usage_cost(model, {
            'prompt_tokens':272000, 'completion_tokens':8192,
            'prompt_tokens_details':{'cached_tokens':0, 'cache_write_tokens':0}}), 0.31296)
        self.assertEqual(usage_cost(model, {
            'prompt_tokens':272001, 'completion_tokens':8192,
            'prompt_tokens_details':{'cached_tokens':0, 'cache_write_tokens':0}}), 0.605442)

    def test_cache_read_and_write_usage_is_disjoint(self):
        usage = {'prompt_tokens':100000, 'completion_tokens':10000,
                 'prompt_tokens_details':{'cached_tokens':30000, 'cache_write_tokens':20000},
                 'completion_tokens_details':{'reasoning_tokens':8000}}
        # 50K ordinary + 30K cached + 20K written, and reasoning is already
        # included in the 10K completion total.
        self.assertEqual(usage_cost(self.models['gpt-6.1-sol'], usage), 0.1265)
        self.assertEqual(usage_cost(self.models['gpt-6-astra'], usage), 0.64)

    def test_long_tier_uses_total_prompt_including_cached_tokens(self):
        usage = {'prompt_tokens':300000, 'completion_tokens':10000,
                 'prompt_tokens_details':{'cached_tokens':250000, 'cache_write_tokens':50000}}
        self.assertEqual(usage_cost(self.models['gpt-6.1-sol'], usage), 0.225)
        self.assertEqual(usage_cost(self.models['gpt-6-astra'], usage), 1.25)

    def test_incomplete_cache_usage_remains_conservative(self):
        model = self.models['gpt-6.1-sol']
        for details, expected in (
            (None, 0.125), ({}, 0.125),
            ({'cached_tokens':30000}, 0.089),
            ({'cache_write_tokens':20000}, 0.105),
            ({'cached_tokens':0, 'cache_write_tokens':0}, 0.1),
            ({'cached_tokens':True, 'cache_write_tokens':0}, 0.125),
            ({'cached_tokens':-1, 'cache_write_tokens':0}, 0.125),
            ({'cached_tokens':'30000', 'cache_write_tokens':0}, 0.125),
            ({'cached_tokens':90000, 'cache_write_tokens':20000}, 0.125),
            (['unexpected'], 0.125),
        ):
            with self.subTest(details=details):
                self.assertEqual(usage_cost(model, {
                    'prompt_tokens':100000, 'completion_tokens':0,
                    'prompt_tokens_details':details}), expected)

    def test_missing_or_invalid_usage_is_unknown_not_zero(self):
        model = self.models['gpt-6.1-sol']
        for usage in (None, {}, {'prompt_tokens':0}, {'completion_tokens':0},
                      {'prompt_tokens':True, 'completion_tokens':10},
                      {'prompt_tokens':10, 'completion_tokens':-1},
                      {'prompt_tokens':10.0, 'completion_tokens':10},
                      {'prompt_tokens':'10', 'completion_tokens':10}):
            with self.subTest(usage=usage):
                self.assertIsNone(usage_cost(model, usage))
        self.assertEqual(usage_cost(model, {'prompt_tokens':0, 'completion_tokens':0}), 0)

    def test_legacy_frozen_pricing_still_works_without_new_fields(self):
        for name in ('gpt-4.1-mini', 'gpt-4.1-nano', 'gpt-4.1', 'gpt-5-mini'):
            with self.subTest(model=name):
                model = self.models[name]
                expected = (100000*model['input_batch_usd_per_million'] +
                            10000*model['output_batch_usd_per_million'])/1000000
                self.assertAlmostEqual(reserve_cost(model, 100000, 10000), expected)
                self.assertAlmostEqual(usage_cost(model, {
                    'prompt_tokens':100000, 'completion_tokens':10000,
                    'prompt_tokens_details':{'cached_tokens':50000, 'cache_write_tokens':20000}}), expected)
        self.assertEqual(self.models['gpt-5-mini']['api_model'], 'gpt-5-mini-2025-08-07')

    def test_every_valid_cache_mix_is_covered_by_the_reservation(self):
        for name in self.models:
            model = self.models[name]
            for bound in (1, 100000, 272000, 272001, 400000):
                reserved = reserve_cost(model, bound, 32768)
                for count in (0, bound//2, bound):
                    for cached in (0, count//2, count):
                        for written in (0, (count-cached)//2, count-cached):
                            reported = usage_cost(model, {
                                'prompt_tokens':count, 'completion_tokens':32768,
                                'prompt_tokens_details':{'cached_tokens':cached, 'cache_write_tokens':written}})
                            with self.subTest(model=name, bound=bound, count=count, cached=cached, written=written):
                                self.assertLessEqual(reported, reserved)

    def test_reservation_rounds_up_to_microdollars(self):
        for name in self.models:
            model = self.models[name]
            for count in (1, 3, 7, 1001):
                expected = (Decimal(str(max(model['input_batch_usd_per_million'],
                                           model.get('cache_write_batch_usd_per_million', 0)))) * count +
                            Decimal(str(model['output_batch_usd_per_million']))) / 1000000
                actual = Decimal(str(reserve_cost(model, count, 1)))
                self.assertGreaterEqual(actual, expected)
                self.assertLess(actual-expected, Decimal('0.000001'))
        for value in (-1, True, 1.5):
            with self.assertRaises(ContractError):
                reserve_cost(self.models['gpt-6.1-sol'], value, 10)


class ModelRequestCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)

    def task(self, model='gpt-6.1-sol'):
        queue(self.state, model=model, review_model='gpt-6-astra')
        self.engine.provider = None
        self.engine.tick()
        return self.state.tasks()[0]

    def test_request_prices_use_frozen_model_and_output_caps(self):
        task = self.task()
        campaign = self.state.campaigns()[0]
        campaign['max_output_tokens'], campaign['review_output_tokens'] = 32768, 8192
        self.state.save_campaign(campaign)
        line, cost, bound = build_request(self.config, self.state, task)
        self.assertEqual(line['url'], '/v1/chat/completions')
        self.assertEqual(line['body']['model'], 'gpt-6.1-sol')
        self.assertEqual(line['body']['reasoning_effort'], 'low')
        self.assertEqual(line['body']['max_completion_tokens'], 32768)
        self.assertTrue(line['body']['response_format']['json_schema']['strict'])
        self.assertNotIn('tools', line['body'])
        self.assertNotIn('temperature', line['body'])
        self.assertEqual(cost, reserve_cost(task['models']['gpt-6.1-sol'], bound, 32768))
        self.config.models['gpt-6.1-sol']['cache_write_batch_usd_per_million'] = 999
        self.config.models['gpt-6.1-sol']['long_context_input_multiplier'] = 999
        self.config.runtime['max_output_tokens'] = 1
        self.assertEqual(build_request(self.config, self.state, task), (line, cost, bound))
        source = self.state.source(task)
        self.state.save_candidate(task, {'html':source['html'],
                                        **{key:source['article'].get(key) for key in ('title','subtitle','section')}})
        task['stage'] = 'review1'
        review, review_cost, review_bound = build_request(self.config, self.state, task)
        self.assertEqual(review['body']['model'], 'gpt-6-astra')
        self.assertEqual(review['body']['max_completion_tokens'], 8192)
        self.assertEqual(review_cost, reserve_cost(task['models']['gpt-6-astra'], review_bound, 8192))
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_context_bound_still_fails_closed(self):
        task = self.task()
        task['models']['gpt-6.1-sol']['context_tokens'] = 1
        with self.assertRaisesRegex(ContractError, 'context'):
            build_request(self.config, self.state, task)

    def test_new_models_cannot_bypass_pre_submission_budget(self):
        queue(self.state, model='gpt-6-astra', review_model='gpt-6-astra', budget_usd=0.01)
        self.engine.tick()
        self.assertTrue(all(task['status'] == 'budget_blocked' for task in self.state.tasks()))
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_model_billing_metadata_is_validated_before_submission(self):
        for field, bad_values in (
            ('cached_input_batch_usd_per_million', (-1, True, '0.05', None)),
            ('cache_write_batch_usd_per_million', (-1, True, '1.25', None)),
            ('long_context_threshold_tokens', (0, -1, True, 272000.0, 1050000)),
            ('long_context_input_multiplier', (0, -1, 0.5, True, '2')),
            ('long_context_output_multiplier', (0, -1, 0.5, True, '1.5')),
        ):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    models = copy.deepcopy(self.config.models)
                    models['gpt-6.1-sol'][field] = value
                    write_json(self.root/'config/models.json', models)
                    with self.assertRaises(ContractError):
                        Config(self.root)
        for field in ('cached_input_batch_usd_per_million', 'cache_write_batch_usd_per_million',
                      'long_context_threshold_tokens', 'long_context_input_multiplier', 'long_context_output_multiplier'):
            with self.subTest(missing=field):
                models = copy.deepcopy(self.config.models)
                models['gpt-6.1-sol'].pop(field)
                write_json(self.root/'config/models.json', models)
                with self.assertRaises(ContractError):
                    Config(self.root)


if __name__ == '__main__':
    unittest.main()
