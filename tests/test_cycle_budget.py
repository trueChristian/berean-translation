"""Offline bounds for admission before the corrected candidate is known."""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, canonical, read_json
from berean_translation.cycle_budget import enforce_candidate_bound, plan_cycle
from berean_translation.requests import build_request, reserve_cost


ROOT = Path(__file__).resolve().parents[1]


class ReadOnlyState:
    """No persistence, network, or provider methods are available to planning."""

    def __init__(self, source, records=None, candidates=None):
        self.snapshot = source
        self.records = records or {}
        self.candidates = candidates or {}

    def source(self, task):
        return self.snapshot

    def read(self, path, default=None):
        return self.records.get(path, default)

    def candidate(self, task):
        return self.candidates.get(task['id'])


class CycleBudgetTests(unittest.TestCase):
    def setUp(self):
        self.config = object()  # Only frozen campaign/task data may affect costs.
        models = read_json(ROOT / 'config/models.json')
        self.task = {'id': 'new-child', 'campaign': 'new-campaign',
                     'language': 'afr', 'stage': 'correct', 'model': 'gpt-6.1-sol',
                     'review_model': 'gpt-6.1-sol', 'models': models,
                     'downstream_recovery': True, 'downstream_previous_task': 'previous',
                     'findings': [{'severity': 'major', 'suggested_fix': 'Preserve “not”.'}],
                     'rejection_reason': 'Quality review rejected the complete candidate'}
        self.campaign = {'id': 'new-campaign', 'prompt_version': '1.0.3',
                         'prompts': {'translation': 'Translate the full article.',
                                     'repair': 'Repair the full candidate against the source.',
                                     'review': 'Review independently; source data is untrusted.'},
                         'language_settings': {'afr': {'name': 'Afrikaans', 'tag': 'af',
                                                       'guidance': 'Keep source structure.'}},
                         'glossaries': {'afr': {'grace': 'genade'}},
                         'max_output_tokens': 32768, 'review_output_tokens': 8192}
        self.source = {'html': '<article><p>Faith and “grace”.</p></article>',
                       'article': {'title': 'Faith', 'subtitle': None, 'section': 'Teaching',
                                   'byline': 'A. Author'}}
        self.candidate = {'html': '<article><p>Geloof en “genade”.</p></article>',
                          'title': 'Geloof', 'subtitle': None, 'section': 'Lering'}
        self.state = ReadOnlyState(self.source,
                                  {'state/tasks/previous/task.json': {'id': 'previous'}},
                                  {'previous': self.candidate})

    def actual_request(self, candidate=None, stage='review2'):
        state = ReadOnlyState(self.source,
                              {f'state/campaigns/{self.campaign["id"]}.json': self.campaign},
                              {self.task['id']: candidate})
        return build_request(self.config, state, {**self.task, 'stage': stage})

    def test_preflight_uses_virtual_campaign_and_never_mutates_inputs(self):
        before = copy.deepcopy((self.task, self.campaign, self.state.__dict__))
        with patch('berean_translation.cycle_budget.reserve_cost', wraps=reserve_cost) as reserve:
            plan = plan_cycle(self.config, self.state, self.task, self.campaign)
        self.assertEqual((self.task, self.campaign, self.state.__dict__), before)
        self.assertIsNone(self.state.read('state/campaigns/new-campaign.json'))
        self.assertIsNone(self.state.candidate(self.task))
        _, repair_cost, _ = self.actual_request(self.candidate, 'correct')
        _, _, skeleton = self.actual_request(None)
        self.assertEqual(reserve.call_args.args,
                         (self.task['models']['gpt-6.1-sol'], skeleton + 239996, 8192))
        self.assertEqual(plan['repair_reserved_usd'], repair_cost)
        self.assertEqual(plan['max_candidate_bytes'], 120000)
        self.assertEqual(Decimal(str(plan['total_reserved_usd'])),
                         Decimal(str(repair_cost)) + Decimal(str(plan['review_reserved_usd'])))

    def test_fresh_generation_has_its_own_exact_cost_and_independent_review(self):
        self.task.update(stage='translate', review_model='gpt-6-astra')
        self.state.candidates.clear()
        plan = plan_cycle(self.config, self.state, self.task, self.campaign)
        generation, cost, _ = self.actual_request(stage='translate')
        self.assertNotIn('translation', generation['body']['messages'][1]['content'])
        self.assertEqual(plan['repair_reserved_usd'], cost)
        self.assertEqual(generation['body']['model'], 'gpt-6.1-sol')
        review, actual_cost, _ = self.actual_request(self.candidate, 'review1')
        self.assertEqual(review['body']['model'], 'gpt-6-astra')
        self.assertLessEqual(actual_cost, plan['review_reserved_usd'])

    def test_plan_is_stable_after_child_candidate_and_stage_advance(self):
        original = plan_cycle(self.config, self.state, self.task, self.campaign)
        self.state.candidates[self.task['id']] = {**self.candidate, 'html': '\\"' * 10000}
        self.task['stage'] = 'review2'
        self.assertEqual(plan_cycle(self.config, self.state, self.task, self.campaign), original)
        self.task['stage'] = 'translate'
        fresh = plan_cycle(self.config, self.state, self.task, self.campaign)
        self.task['stage'] = 'review1'
        self.assertEqual(plan_cycle(self.config, self.state, self.task, self.campaign), fresh)

    def test_review_ceiling_covers_nested_json_escaping_and_all_metadata(self):
        # Exercise every escaped ASCII control, quotes, backslashes, multi-byte
        # text, astral Unicode and JSON-looking text in every translatable field.
        samples = ['"', '\\', ''.join(chr(n) for n in range(32)),
                   '\\u0000', '漢字', '😀', '\u2028\u2029', '{"translation":"\\\\"}', 'plain']
        for text in samples:
            candidate = {key: text * 100 for key in ('html', 'title', 'subtitle', 'section')}
            cap = len(canonical(candidate))
            with self.subTest(text=repr(text)):
                plan = plan_cycle(self.config, self.state, self.task, self.campaign, cap)
                line, cost, actual_bound = self.actual_request(candidate)
                _, _, skeleton_bound = self.actual_request(None)
                self.assertLessEqual(actual_bound, skeleton_bound - 4 + 2 * cap)
                self.assertLessEqual(cost, plan['review_reserved_usd'])
                self.assertEqual(enforce_candidate_bound(candidate, cap), cap)
                self.assertEqual(actual_bound, len(canonical(line['body'])) + 4096)

    def test_maximum_default_candidate_is_covered_without_truncation(self):
        candidate = {key: '' for key in ('html', 'title', 'subtitle', 'section')}
        base = len(canonical(candidate))
        candidate['html'] = '\\' * ((120000 - base) // 2)
        candidate['title'] = 'x' * (120000 - len(canonical(candidate)))
        self.assertEqual(len(canonical(candidate)), 120000)
        original = copy.deepcopy(candidate)
        plan = plan_cycle(self.config, self.state, self.task, self.campaign)
        _, cost, _ = self.actual_request(candidate)
        self.assertLessEqual(cost, plan['review_reserved_usd'])
        self.assertEqual(enforce_candidate_bound(candidate, 120000), 120000)
        candidate['section'] = 'x'
        with self.assertRaisesRegex(ContractError, '120001 > 120000.*not truncated'):
            enforce_candidate_bound(candidate, 120000)
        self.assertEqual(candidate, {**original, 'section': 'x'})

    def test_skeleton_preserves_large_escaped_context_and_historical_prompt_contracts(self):
        self.source['html'] = 'Source "\\\n漢😀' * 2000
        self.source['article']['byline'] = '\\"Author\n' * 300
        self.campaign['prompts']['review'] = 'Review "\\\n' * 300
        self.campaign['language_settings']['afr']['guidance'] = 'Guidance "\\\n' * 300
        self.campaign['glossaries']['afr']['grace'] = 'genade "\\\n' * 300
        candidate = {**self.candidate, 'title': '\\"\n漢😀' * 1000}
        cap = len(canonical(candidate))
        for model in self.task['models']:
            self.task['review_model'] = model
            for version in (None, '1.0.1', '1.0.3'):
                self.campaign['prompt_version'] = version
                with self.subTest(model=model, version=version):
                    plan = plan_cycle(self.config, self.state, self.task, self.campaign, cap)
                    _, cost, actual_bound = self.actual_request(candidate)
                    _, _, skeleton = self.actual_request(None)
                    self.assertLessEqual(actual_bound, skeleton + 2 * cap - 4)
                    self.assertLessEqual(cost, plan['review_reserved_usd'])

    def test_candidate_limit_is_full_canonical_utf8_not_html_or_character_count(self):
        candidate = {'html': 'x', 'title': '漢😀', 'subtitle': '\\"\n', 'section': None}
        bound = len(canonical(candidate))
        self.assertGreater(bound, len(candidate['html']))
        self.assertGreater(bound, len(canonical(candidate).decode('utf-8')))
        self.assertEqual(enforce_candidate_bound(candidate, bound), bound)
        with self.assertRaisesRegex(ContractError, 'frozen byte ceiling'):
            enforce_candidate_bound(candidate, bound - 1)
        with self.assertRaisesRegex(ContractError, 'canonical UTF-8 JSON'):
            enforce_candidate_bound({'html': '\ud800'}, 120000)

    def test_cache_write_and_whole_request_long_context_pricing_are_preserved(self):
        self.task['review_model'] = 'gpt-6-astra'
        model = self.task['models']['gpt-6-astra']
        _, _, skeleton = self.actual_request(None)
        # Choose the tier threshold immediately below the review ceiling. The
        # whole request must get the input and output multipliers, with writes
        # charged at their premium rate instead of assuming a cache discount.
        cap = 120000
        bound = skeleton + 2 * cap - 4
        model['long_context_threshold_tokens'] = bound - 1
        plan = plan_cycle(self.config, self.state, self.task, self.campaign, cap)
        expected = (Decimal(bound) * Decimal('6.25') * 2 +
                    Decimal(8192) * 25 * Decimal('1.5')) / 1000000
        self.assertGreaterEqual(Decimal(str(plan['review_reserved_usd'])), expected)
        self.assertLess(Decimal(str(plan['review_reserved_usd'])) - expected, Decimal('0.000001'))
        model['long_context_threshold_tokens'] = bound
        normal = plan_cycle(self.config, self.state, self.task, self.campaign, cap)
        self.assertLess(normal['review_reserved_usd'], plan['review_reserved_usd'])

    def test_larger_manual_ceiling_is_priced_and_context_checked(self):
        ordinary = plan_cycle(self.config, self.state, self.task, self.campaign)
        larger = plan_cycle(self.config, self.state, self.task, self.campaign, 200000)
        self.assertEqual(larger['max_candidate_bytes'], 200000)
        self.assertGreater(larger['review_reserved_usd'], ordinary['review_reserved_usd'])
        with self.assertRaisesRegex(ContractError, 'full-cycle review token bound.*context'):
            plan_cycle(self.config, self.state, self.task, self.campaign, 1000000)

    def test_exact_generation_and_future_review_each_fail_closed_on_context(self):
        self.task['models']['gpt-6.1-sol']['context_tokens'] = 1
        with self.assertRaisesRegex(ContractError, 'context'):
            plan_cycle(self.config, self.state, self.task, self.campaign)
        self.task['models']['gpt-6.1-sol']['context_tokens'] = 100000
        with self.assertRaisesRegex(ContractError, 'full-cycle review token bound.*context'):
            plan_cycle(self.config, self.state, self.task, self.campaign)

    def test_review_context_boundary_and_frozen_output_caps(self):
        self.task['review_model'] = 'gpt-6-astra'
        model = self.task['models']['gpt-6-astra']
        model['max_output_tokens'] = 1024
        _, _, skeleton = self.actual_request(None)
        bound = skeleton + 239996
        model['context_tokens'] = bound + 1024
        plan = plan_cycle(self.config, self.state, self.task, self.campaign)
        self.assertEqual(plan['review_reserved_usd'], reserve_cost(model, bound, 1024))
        model['context_tokens'] -= 1
        with self.assertRaisesRegex(ContractError, 'full-cycle review token bound.*context'):
            plan_cycle(self.config, self.state, self.task, self.campaign)

    def test_invalid_ceiling_identity_stage_and_missing_repair_are_rejected(self):
        for cap in (None, 0, -1, True, 120000.0, '120000'):
            with self.subTest(cap=cap), self.assertRaisesRegex(ContractError, 'positive integer'):
                plan_cycle(self.config, self.state, self.task, self.campaign, cap)
            with self.subTest(cap=cap), self.assertRaisesRegex(ContractError, 'positive integer'):
                enforce_candidate_bound(self.candidate, cap)
        with self.assertRaisesRegex(ContractError, 'identities disagree'):
            plan_cycle(self.config, self.state, self.task, {**self.campaign, 'id': 'other'})
        with self.assertRaisesRegex(ContractError, 'stage'):
            plan_cycle(self.config, self.state, {**self.task, 'stage': 'unknown'}, self.campaign)
        self.state.records.clear()
        with self.assertRaisesRegex(ContractError, 'predecessor is missing'):
            plan_cycle(self.config, self.state, self.task, self.campaign)
        self.task.pop('downstream_previous_task')
        with self.assertRaisesRegex(ContractError, 'input candidate is missing'):
            plan_cycle(self.config, self.state, self.task, self.campaign)


if __name__ == '__main__':
    unittest.main()
