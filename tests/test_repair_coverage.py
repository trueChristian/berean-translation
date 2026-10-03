"""Whole-article downstream instructions, frozen requests and bounded review.

These are offline contract tests. FakeProvider does not establish whether a
model will discover an unlisted defect or improve a live acceptance rate.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, canonical, json_hash, loads
from berean_translation.downstream import accept, execution_settings, validate_history
from berean_translation.requests import build_request, reserve_cost
from berean_translation.validation import validate_repository
from support import A, setup, queue, drive


class RepairCoverageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, _, self.engine = setup(self.root)
        # Synthetic analogue of the observed repair: a listed defect and an
        # unlisted contextual mistranslation in a separate paragraph.
        for path in list(self.upstream.contents):
            if path.endswith('.html'):
                self.upstream.contents[path] = self.upstream.contents[path].replace(
                    '</article>', '<p>The followers requested a land patent.</p></article>')
        self.upstream.rebuild()
        queue(self.state)

        def initial(line):
            result = self.provider.default_result(line)
            if 'html' in result:
                result['html'] = result['html'].replace('Faith', 'Wrong').replace(
                    'The followers requested a land patent.',
                    'The offspring requested a building permit.')
            else:
                result = {'score': 85, 'passed': False, 'findings': [
                    {'severity': 'major', 'location': 'first paragraph',
                     'source_quote': 'Faith', 'translation_quote': 'Wrong',
                     'suggested_fix': 'Preserve faith'}]}
            return result

        drive(self.engine, self.provider, initial)
        self.previous = copy.deepcopy(next(t for t in self.state.tasks() if t['article_id'] == A))
        self.config.runtime['automatic_downstream_recovery'].update(enabled=True, total_budget_usd=10)

    def request(self, **changes):
        request = {'id': 'coverage-repair', 'operation': 'repair',
                   'model': 'gpt-6.1-sol', 'review_model': 'gpt-6.1-sol',
                   'budget_usd': 2, 'max_articles': 1, 'dry_run': False,
                   'requested_by': 'fixture-owner'}
        request.update(changes)
        return request

    def accepted(self, **changes):
        campaign = accept(self.engine, self.request(**changes))
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        return campaign, task

    def state_bytes(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.state.path('state').rglob('*') if path.is_file()}

    def test_repair_gets_complete_source_candidate_and_nonexhaustive_findings_instruction(self):
        campaign, task = self.accepted()
        line, _, _ = build_request(self.config, self.state, task)
        system = line['body']['messages'][0]['content']
        payload = loads(line['body']['messages'][1]['content'])
        self.assertEqual(system, campaign['prompts']['repair'])
        self.assertTrue(system.startswith(campaign['prompts']['translation'].rstrip() + '\n\n'))
        self.assertIn('including defects not mentioned in the supplied findings', system)
        self.assertIn('every source paragraph', system)
        self.assertIn('including unchanged passages', system)
        self.assertIn('Preserve correct wording and idiomatic target-language expressions', system)
        self.assertIn('Treat every source field', system)
        self.assertEqual(payload['source']['html'], self.state.source(task)['html'])
        self.assertEqual(payload['translation'], self.state.candidate(self.previous))
        self.assertIn('land patent', payload['source']['html'])
        self.assertIn('building permit', payload['translation']['html'])
        self.assertNotIn('building permit', str(payload['correction_findings']))
        self.assertEqual(payload['correction_findings'], self.previous['findings'])
        self.assertEqual(set(payload), {'target_language', 'language_tag', 'language_guidance',
                                      'terminology_glossary', 'source', 'source_context',
                                      'translation', 'correction_findings', 'rejection_reason'})
        self.assertEqual(campaign['execution_settings_sha256'], json_hash(execution_settings(campaign)))

    def test_new_and_legacy_frozen_bytes_and_reservations_survive_live_prompt_changes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                # Independent fixture state for each historical shape.
                if legacy:
                    self.setUp()
                campaign, task = self.accepted()
                if legacy:
                    del campaign['prompts']['repair']
                    campaign['execution_settings_sha256'] = json_hash(execution_settings(campaign))
                    self.state.save_campaign(campaign)
                before = build_request(self.config, self.state, task)
                expected_prompt = campaign['prompts']['translation' if legacy else 'repair']
                self.assertEqual(before[0]['body']['messages'][0]['content'], expected_prompt)
                with patch.object(self.engine, 'submit'):
                    self.engine.prepare()
                frozen = self.state_bytes()
                reserved = self.state.read(f'state/campaigns/{campaign["id"]}.json')['reserved_usd']
                self.assertEqual(reserved, before[1])
                for name in ('translation', 'review', 'repair'):
                    (self.root / 'prompts' / f'{name}.txt').write_text('Unrelated future prompt')
                self.config.runtime['prompt_version'] = 'future-version'
                accepted_again = accept(self.engine, self.request())
                after = build_request(self.config, self.state, task)
                self.assertEqual(canonical(before[0]), canonical(after[0]))
                self.assertEqual(before[1:], after[1:])
                self.assertEqual(accepted_again['reserved_usd'], reserved)
                self.assertEqual(self.state_bytes(), frozen)
                validate_history(self.config, self.state, {t['id']: t for t in self.state.tasks()})

    def test_tampered_frozen_repair_prompt_blocks_before_provider(self):
        campaign, _ = self.accepted()
        campaign['prompts']['repair'] += '\nChanged after acceptance'
        self.state.save_campaign(campaign)
        calls = self.provider.create_calls, self.provider.upload_calls
        with self.assertRaisesRegex(ContractError, 'frozen execution settings changed'):
            self.engine.prepare()
        self.assertEqual((self.provider.create_calls, self.provider.upload_calls), calls)

    def test_ordinary_correction_and_fresh_downstream_keep_translation_prompt(self):
        ordinary = copy.deepcopy(self.previous)
        ordinary['stage'] = 'correct'
        campaign = self.state.read(f'state/campaigns/{ordinary["campaign"]}.json')
        self.assertEqual(build_request(self.config, self.state, ordinary)[0]['body']['messages'][0]['content'],
                         campaign['prompts']['translation'])
        previous = copy.deepcopy(self.previous)
        previous.update(failure_kind='truncated', translation_model_actual=None)
        self.state.save_task(previous)
        self.state.path(f'state/tasks/{previous["id"]}/candidate.json').unlink()
        campaign, fresh = self.accepted()
        self.assertEqual(fresh['stage'], 'translate')
        self.assertEqual(build_request(self.config, self.state, fresh)[0]['body']['messages'][0]['content'],
                         campaign['prompts']['translation'])

    def test_added_prompt_bytes_are_in_existing_conservative_reservation(self):
        campaign, task = self.accepted()
        new_line, new_cost, new_bound = build_request(self.config, self.state, task)
        legacy = copy.deepcopy(campaign)
        del legacy['prompts']['repair']
        self.state.save_campaign(legacy)
        old_line, old_cost, old_bound = build_request(self.config, self.state, task)
        self.state.save_campaign(campaign)
        self.assertGreater(new_bound, old_bound)
        self.assertGreater(new_cost, old_cost)
        self.assertEqual(new_bound - old_bound,
                         len(canonical(new_line['body'])) - len(canonical(old_line['body'])))
        self.assertEqual(new_cost, reserve_cost(task['models'][task['model']], new_bound,
                                              new_line['body']['max_completion_tokens']))
        self.assertEqual(new_line['body']['max_completion_tokens'], old_line['body']['max_completion_tokens'])

    def test_tight_envelope_blocks_before_upload_without_extra_authorization(self):
        campaign, task = self.accepted(budget_usd=0.000001)
        calls = self.provider.create_calls, self.provider.upload_calls
        self.engine.prepare()
        self.assertEqual((self.provider.create_calls, self.provider.upload_calls), calls)
        self.assertEqual(self.state.read(f'state/tasks/{task["id"]}/task.json')['status'], 'budget_blocked')
        after = self.state.read(f'state/campaigns/{campaign["id"]}.json')
        self.assertEqual(after['budget_usd'], 0.000001)
        self.assertEqual(after['downstream_allocation_usd'], 0.000001)
        self.assertEqual(after['reserved_usd'], 0)

    def test_unlisted_remaining_error_is_independently_rejected_without_another_repair(self):
        campaign, task = self.accepted()
        old_bytes = self.state.path(f'state/tasks/{self.previous["id"]}/task.json').read_bytes()
        self.engine.prepare()

        def narrow_repair(line):
            payload = loads(line['body']['messages'][1]['content'])
            result = copy.deepcopy(payload['translation'])
            result['html'] = result['html'].replace('Wrong', 'Faith')
            return result

        self.provider.complete_all(narrow_repair)
        self.engine.collect()
        reviewing = self.state.read(f'state/tasks/{task["id"]}/task.json')
        self.assertEqual(reviewing['stage'], 'review2')
        review_line = build_request(self.config, self.state, reviewing)[0]
        self.assertEqual(review_line['body']['messages'][0]['content'], campaign['prompts']['review'])
        payload = loads(review_line['body']['messages'][1]['content'])
        self.assertEqual(payload['translation'], self.state.candidate(reviewing))
        self.assertIn('building permit', payload['translation']['html'])
        self.assertIn('land patent', payload['source']['html'])
        self.assertEqual(set(payload), {'target_language', 'language_tag', 'language_guidance',
                                      'terminology_glossary', 'source', 'source_context', 'translation'})
        self.engine.prepare()
        self.provider.complete_all(lambda _: {'score': 70, 'passed': False, 'findings': [
            {'severity': 'major', 'location': 'last paragraph', 'source_quote': 'land patent',
             'translation_quote': 'building permit', 'suggested_fix': 'Preserve the land grant meaning'}]})
        self.engine.collect()
        final = self.state.read(f'state/tasks/{task["id"]}/task.json')
        self.assertEqual(final['status'], 'not_ready')
        self.assertEqual((final['translation_attempts'], final['review_attempts']), (1, 1))
        self.assertFalse(self.state.projection(self.config)['articles'])
        calls = self.provider.create_calls, self.provider.upload_calls
        self.engine.prepare()
        self.assertEqual((self.provider.create_calls, self.provider.upload_calls), calls)
        self.assertEqual(self.state.path(f'state/tasks/{self.previous["id"]}/task.json').read_bytes(), old_bytes)
        validate_repository(self.config)


if __name__ == '__main__':
    unittest.main()
