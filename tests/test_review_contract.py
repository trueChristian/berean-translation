"""Offline review-contract boundaries, frozen requests, and fail-closed holds."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation import continuation
from berean_translation.common import ContractError, canonical, json_hash, loads, read_json
from berean_translation.config import Config
from berean_translation.cycle_budget import plan_cycle
from berean_translation.downstream import accept, eligible, execution_settings
from berean_translation.requests import accepted_review, build_request, reserve_cost
from berean_translation.review_contract import MAX_FINDINGS, frozen_version, validate_review
from berean_translation.validation import validate_repository
from support import drive, queue, setup


LEGACY_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'score': {'type': 'integer'}, 'passed': {'type': 'boolean'},
        'findings': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'severity': {'type': 'string', 'enum': ['minor', 'major', 'critical']},
                **{key: {'type': 'string'} for key in
                   ('location', 'source_quote', 'translation_quote', 'suggested_fix')}},
            'required': ['severity', 'location', 'source_quote', 'translation_quote', 'suggested_fix']}}},
    'required': ['score', 'passed', 'findings']}


def finding(index=0, severity='minor'):
    return {'severity': severity, 'location': f'p[{index}]', 'source_quote': 'Faith',
            'translation_quote': f'Candidate {index}', 'suggested_fix': 'Preserve source meaning'}


def review(count=0, *, version=2, complete=True, passed=True, score=100):
    result = {'score': score, 'passed': passed, 'findings': [finding(i) for i in range(count)]}
    if version == 2:
        result['findings_complete'] = complete
    return result


class ReviewValueContractTests(unittest.TestCase):
    def test_zero_and_thirty_findings_are_valid_but_thirty_one_is_not(self):
        self.assertEqual(MAX_FINDINGS, 30)
        for version in (None, 2):
            for count in (0, 30):
                with self.subTest(version=version, count=count):
                    self.assertTrue(accepted_review(review(count, version=version), contract_version=version))
            with self.subTest(version=version), self.assertRaises(ContractError):
                accepted_review(review(31, version=version), contract_version=version)

    def test_incomplete_review_cannot_pass_regardless_of_verdict(self):
        for passed in (False, True):
            with self.subTest(passed=passed):
                self.assertFalse(accepted_review(review(30, complete=False, passed=passed), contract_version=2))

    def test_quality_gates_still_apply_to_complete_reviews(self):
        self.assertTrue(accepted_review(review(passed=False), contract_version=2))
        for changes in ({'score': 94},):
            with self.subTest(changes=changes):
                self.assertFalse(accepted_review(review(**changes), contract_version=2))
        for severity in ('major', 'critical'):
            value = review(1)
            value['findings'][0]['severity'] = severity
            self.assertFalse(accepted_review(value, contract_version=2))

    def test_all_v2_fields_are_strict_even_when_the_review_is_incomplete(self):
        mutations = [
            ('missing completion flag', lambda value: value.pop('findings_complete')),
            ('extra field', lambda value: value.update(extra=True)),
            ('boolean score', lambda value: value.update(score=True)),
            ('float score', lambda value: value.update(score=99.0)),
            ('negative score', lambda value: value.update(score=-1)),
            ('overlarge score', lambda value: value.update(score=101)),
            ('string verdict', lambda value: value.update(passed='false')),
            ('integer completion flag', lambda value: value.update(findings_complete=0)),
            ('null completion flag', lambda value: value.update(findings_complete=None)),
            ('string completion flag', lambda value: value.update(findings_complete='false')),
            ('non-array findings', lambda value: value.update(findings={})),
            ('non-object finding', lambda value: value['findings'].__setitem__(0, [])),
            ('missing finding field', lambda value: value['findings'][0].pop('suggested_fix')),
            ('extra finding field', lambda value: value['findings'][0].update(extra='x')),
            ('non-text finding field', lambda value: value['findings'][0].update(source_quote=None)),
            ('unknown severity', lambda value: value['findings'][0].update(severity='warning')),
        ]
        for label, mutate in mutations:
            with self.subTest(field=label):
                value = review(1, complete=False)
                mutate(value)
                with self.assertRaises(ContractError):
                    accepted_review(value, contract_version=2)
        for value in (None, [], 'review'):
            with self.subTest(container=value), self.assertRaises(ContractError):
                accepted_review(value, contract_version=2)

    def test_unbounded_diagnostic_validation_checks_every_finding(self):
        for version in (None, 2):
            value = review(31, version=version)
            validate_review(value, version, enforce_limit=False)
            value['findings'][30]['suggested_fix'] = None
            with self.subTest(version=version), self.assertRaises(ContractError):
                validate_review(value, version, enforce_limit=False)

    def test_legacy_parser_does_not_accept_the_new_completion_field(self):
        with self.assertRaises(ContractError):
            accepted_review(review())
        with self.assertRaises(ContractError):
            accepted_review(review(version=None), contract_version=2)

    def test_unknown_versions_fail_closed(self):
        for version in (None, 0, 1, 3, True, '2', 2.0):
            with self.subTest(version=version), self.assertRaises(ContractError):
                accepted_review(review(), contract_version=version)


class FrozenReviewContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture(2)

    def fixture(self, version):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root, review_contract_version=version)
        self.upstream.articles = self.upstream.articles[:1]
        self.upstream.rebuild()

    def accept_ordinary(self, identity='ordinary', **changes):
        self.engine.discover()
        return self.engine.accept_request(queue(self.state, identity, **changes))

    def task(self, identity):
        return self.state.read(f'state/tasks/{identity}/task.json')

    def review_task(self, campaign):
        task = self.task(campaign['tasks'][0])
        source = self.state.source(task)
        self.state.save_candidate(task, {'html': source['html'], **{
            key: source['article'].get(key) for key in ('title', 'subtitle', 'section')}})
        task['stage'] = 'review1'
        self.state.save_task(task)
        return task

    def downstream_request(self, identity='repair', *, continuations=True, **changes):
        result = {'id': identity, 'operation': 'repair', 'model': 'gpt-6.1-sol',
                  'review_model': 'gpt-6.1-sol', 'budget_usd': 2, 'max_articles': 1,
                  'dry_run': False, 'requested_by': 'fixture-owner'}
        if continuations:
            result['continuation_policy'] = continuation.policy()
        result.update(changes)
        return result

    def enable_downstream(self):
        self.config.runtime['automatic_downstream_recovery'].update(enabled=True, total_budget_usd=10)

    def hold_quality_failure(self):
        queue(self.state, 'original')

        def reject(line):
            if ':review' in line['custom_id']:
                value = review(1, version=self.config.runtime.get('review_contract_version'),
                               passed=False, score=80)
                value['findings'][0]['severity'] = 'major'
                return value
            return self.provider.default_result(line)

        drive(self.engine, self.provider, reject)
        task = self.state.tasks()[0]
        self.assertEqual(task['failure_kind'], 'quality_rejection')
        return task

    def snapshot(self, paths):
        return {path: path.read_bytes() for root in paths for path in
                ([root] if root.is_file() else root.rglob('*')) if path.is_file()}

    def assert_frozen(self, snapshot):
        for path, data in snapshot.items():
            self.assertEqual(path.read_bytes(), data, str(path.relative_to(self.root)))

    def assert_attention_recovery_is_unfunded(self):
        campaign = accept(self.engine, self.downstream_request(identity='attention-probe'))
        self.assertTrue(campaign['empty_selection'])
        self.assertEqual(campaign['selection'], [])
        self.assertEqual(campaign['tasks'], [])
        self.assertEqual(campaign['downstream_allocation_usd'], 0)


    def test_new_ordinary_campaign_freezes_exact_schema_and_prompt_in_both_reviews(self):
        campaign = self.accept_ordinary()
        self.assertEqual(campaign['review_contract_version'], 2)
        task = self.review_task(campaign)
        expected = copy.deepcopy(LEGACY_SCHEMA)
        expected['properties']['score'].update(minimum=0, maximum=100)
        expected['properties']['findings']['maxItems'] = 30
        expected['properties']['findings_complete'] = {'type': 'boolean'}
        expected['required'].append('findings_complete')
        for stage in ('review1', 'review2'):
            task['stage'] = stage
            line, cost, bound = build_request(self.config, self.state, task)
            body = line['body']
            schema = body['response_format']['json_schema']
            self.assertTrue(schema['strict'])
            self.assertEqual(schema['schema'], expected)
            prompt = body['messages'][0]['content']
            self.assertTrue(prompt.startswith(campaign['prompts']['review']))
            self.assertIn('95/100', prompt)
            self.assertIn('findings_complete', prompt)
            self.assertIn('30', prompt)
            self.assertEqual(bound, len(canonical(body)) + 4096)
            self.assertEqual(cost, reserve_cost(task['models'][task['review_model']], bound,
                                               body['max_completion_tokens']))
        before = canonical(build_request(self.config, self.state, task))
        self.config.runtime.pop('review_contract_version')
        (self.root / 'prompts/review.txt').write_text('Changed live review prompt')
        self.assertNotEqual(canonical(build_request(self.config, self.state, task)), before)

    def test_new_review_uses_current_schema_without_rewriting_legacy_campaign(self):
        self.fixture(None)
        campaign = self.accept_ordinary()
        self.assertNotIn('review_contract_version', campaign)
        task = self.review_task(campaign)
        frozen = self.snapshot([self.state.path(f'state/campaigns/{campaign["id"]}.json')])
        initial = build_request(self.config, self.state, task)
        self.assertEqual(initial[0]['body']['response_format']['json_schema']['schema'], LEGACY_SCHEMA)
        self.config.runtime['review_contract_version'] = 2
        (self.root / 'prompts/review.txt').write_text('Changed current review prompt')
        for stage in ('review1', 'review2'):
            task['stage'] = stage
            built = build_request(self.config, self.state, task)
            schema = built[0]['body']['response_format']['json_schema']['schema']
            self.assertIn('findings_complete', schema['required'])
            self.assertIn('Changed current review prompt', built[0]['body']['messages'][0]['content'])
        self.assert_frozen(frozen)


    def test_unknown_runtime_and_frozen_versions_are_rejected(self):
        path = self.root / 'config/runtime.json'
        original = read_json(path)
        for version in (0, 1, 3, True, '2', 2.0):
            changed = {**original, 'review_contract_version': version}
            path.write_bytes(canonical(changed))
            with self.subTest(version=version), self.assertRaises(ContractError):
                Config(self.root)
        path.write_bytes(canonical(original))
        campaign = self.accept_ordinary()
        for version in (None, 999):
            campaign['review_contract_version'] = version
            with self.subTest(version=version), self.assertRaises(ContractError):
                frozen_version(campaign)

    def test_present_null_cannot_manufacture_a_legacy_strategy_identity(self):
        from berean_translation import continuation, downstream
        campaign = self.accept_ordinary()
        campaign.pop('review_contract_version')
        continuation.strategy(campaign, 'afr')
        downstream.execution_settings(campaign)
        campaign['review_contract_version'] = None
        with self.assertRaises(ContractError):
            continuation.strategy(campaign, 'afr')
        with self.assertRaises(ContractError):
            downstream.execution_settings(campaign)

    def test_unknown_frozen_version_blocks_preparation_before_reservations(self):
        for stage in ('translate', 'correct', 'review1', 'review2'):
            with self.subTest(stage=stage):
                self.fixture(2)
                campaign = self.accept_ordinary()
                task = self.review_task(campaign)
                task['stage'] = stage
                self.state.save_task(task)
                campaign['review_contract_version'] = 999
                self.state.save_campaign(campaign)
                snapshot = self.snapshot([self.state.path(f'state/campaigns/{campaign["id"]}.json')])
                self.engine.prepare()
                ended = self.task(task['id'])
                self.assertEqual(ended['status'], 'not_ready')
                self.assertIn('review contract version', ended['failure'])
                self.assertEqual((ended['translation_attempts'], ended['review_attempts']), (0, 0))
                self.assertEqual(self.state.batches(), [])
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assertEqual(self.state.read(f'state/campaigns/{campaign["id"]}.json')['reserved_usd'], 0)
                self.assert_frozen(snapshot)

    def test_unknown_frozen_version_blocks_already_prepared_batches_without_rewriting_history(self):
        for stage in ('translate', 'correct', 'review1', 'review2'):
            with self.subTest(stage=stage):
                self.fixture(2)
                campaign = self.accept_ordinary()
                task = self.review_task(campaign)
                task['stage'] = stage
                self.state.save_task(task)
                with patch.object(self.engine, 'submit'):
                    self.engine.prepare()
                batch = self.state.batches()[0]
                self.assertEqual(batch['status'], 'prepared')
                self.assertGreater(batch['reserved_usd'], 0)
                campaign = self.state.read(f'state/campaigns/{campaign["id"]}.json')
                campaign['review_contract_version'] = 999
                self.state.save_campaign(campaign)
                snapshot = self.snapshot([
                    self.state.path(f'state/campaigns/{campaign["id"]}.json'),
                    self.state.path(f'state/tasks/{task["id"]}'),
                    self.state.path(f'state/batches/{batch["id"]}'),
                    self.state.path('state/records')])
                with self.assertRaises(ContractError):
                    self.engine.submit(batch)
                with self.assertRaises(ContractError):
                    self.engine.collect()
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assert_frozen(snapshot)

    def test_unknown_frozen_version_still_reconciles_uncertain_submissions_read_only(self):
        for status in ('submitting', 'submission_unknown'):
            for lost_response in ('before', 'after'):
                with self.subTest(status=status, lost_response=lost_response):
                    self.fixture(2)
                    campaign = self.accept_ordinary()
                    self.provider.raise_create = lost_response
                    self.engine.prepare()
                    batch = self.state.batches()[0]
                    self.assertEqual(batch['status'], 'submission_unknown')
                    batch['status'] = status
                    self.state.save_batch(batch)
                    campaign = self.state.read(f'state/campaigns/{campaign["id"]}.json')
                    campaign['review_contract_version'] = 999
                    self.state.save_campaign(campaign)
                    snapshot = self.snapshot([
                        self.state.path(f'state/campaigns/{campaign["id"]}.json'),
                        self.state.path(f'state/tasks/{campaign["tasks"][0]}'),
                        self.state.path(f'state/batches/{batch["id"]}/input.jsonl'),
                        self.state.path('state/records')])
                    with patch.object(self.provider, 'find', wraps=self.provider.find) as find:
                        with patch.object(self.provider, 'upload') as upload:
                            with patch.object(self.provider, 'create') as create:
                                self.engine.collect()
                    find.assert_called_once_with(batch['id'])
                    upload.assert_not_called()
                    create.assert_not_called()
                    reconciled = self.state.batches()[0]
                    expected = 'submitted' if lost_response == 'after' else 'submission_unknown'
                    self.assertEqual(reconciled['status'], expected)
                    if lost_response == 'after':
                        self.assertEqual(reconciled['remote_id'], 'batch-1')
                    else:
                        self.assertEqual(reconciled['reconciliation'], 'no_match_yet_no_resubmission')
                    self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 1))
                    self.assert_frozen(snapshot)

    def test_complete_quality_failure_still_gets_exactly_one_correction(self):
        queue(self.state)

        def respond(line):
            if line['custom_id'].endswith(':review1'):
                return review(passed=False, score=94)
            return self.provider.default_result(line)

        drive(self.engine, self.provider, respond, ticks=8)
        task = self.state.tasks()[0]
        self.assertEqual(task['status'], 'complete')
        self.assertEqual((task['translation_attempts'], task['review_attempts']), (2, 2))
        self.assertEqual(self.provider.create_calls, 4)
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_incomplete_reviews_hold_at_both_stages_with_full_immutable_evidence(self):
        for stage in ('review1', 'review2'):
            for passed in (False, True):
                with self.subTest(stage=stage, passed=passed):
                    self.fixture(2)
                    queue(self.state)
                    incomplete = review(30, complete=False, passed=passed)

                    def respond(line):
                        if line['custom_id'].endswith(':' + stage):
                            return incomplete
                        if line['custom_id'].endswith(':review1'):
                            return review(passed=False, score=94)
                        return self.provider.default_result(line)

                    drive(self.engine, self.provider, respond)
                    task = self.state.tasks()[0]
                    self.assertEqual((task['status'], task['failure_kind']), ('not_ready', 'incomplete_review'))
                    self.assertEqual(task['stage'], stage)
                    attempts = 1 if stage == 'review1' else 2
                    self.assertEqual((task['translation_attempts'], task['review_attempts']), (attempts, attempts))
                    decision = self.state.read(f'state/tasks/{task["id"]}/decisions/{stage}.json')
                    self.assertEqual(decision['outcome'], 'incomplete_review')
                    self.assertEqual(decision['findings'], incomplete['findings'])
                    result = self.state.read(f'state/tasks/{task["id"]}/results/{stage}.json')
                    self.assertEqual(result['result'], incomplete)
                    self.assertEqual(task['findings'], incomplete['findings'])
                    self.assertIsNotNone(self.state.candidate(task))
                    self.assertFalse(self.state.projection(self.config)['articles'])
                    snapshot = self.snapshot([self.state.path(f'state/tasks/{task["id"]}'),
                                              self.state.path('state/records')])
                    self.engine.receive(task, {'error': {'code': 'replayed_terminal_response'}})
                    self.enable_downstream()
                    calls = self.provider.create_calls
                    self.assert_attention_recovery_is_unfunded()
                    drive(self.engine, self.provider)
                    self.assertEqual(self.provider.create_calls, calls)
                    self.assertEqual(len(self.state.tasks()), 1)
                    self.assert_frozen(snapshot)

    def test_incomplete_review_requires_attention_from_both_selection_helpers(self):
        task = self.hold_quality_failure()
        task.update(failure_kind='incomplete_review', failure='Final review failed', stage='review2')
        self.state.save_task(task)
        for settings in (None, continuation.policy()):
            reason = eligible(self.state, self.config, task, set(), self.state.tasks(),
                              continuation_policy=settings, next_strategy='changed')
            self.assertIsNotNone(reason)
            self.assertIn('incomplete', reason)
            self.assertIn('attention', reason)
        reason = continuation.next_reason(self.state, task, [], continuation.policy(), 'changed')
        self.assertIsNotNone(reason)
        self.assertIn('incomplete', reason)
        self.assertIn('attention', reason)

    def test_legacy_49_and_32_finding_results_remain_invalid_without_paid_successors(self):
        for count in (49, 32):
            with self.subTest(count=count):
                self.fixture(None)
                queue(self.state)

                def respond(line):
                    if ':review' in line['custom_id']:
                        return review(count, version=None, passed=False, score=80)
                    return self.provider.default_result(line)

                drive(self.engine, self.provider, respond)
                task = self.state.tasks()[0]
                self.assertEqual((task['status'], task['failure_kind']), ('not_ready', 'invalid_result'))
                self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
                self.assertEqual(len(task['findings']), count)
                archived = self.state.read(f'state/tasks/{task["id"]}/results/review1.json')
                self.assertEqual(len(archived['result']['findings']), count)
                snapshot = self.snapshot([self.state.path(f'state/tasks/{task["id"]}')])
                calls = self.provider.create_calls
                self.enable_downstream()
                self.assert_attention_recovery_is_unfunded()
                drive(self.engine, self.provider)
                self.assertEqual(self.provider.create_calls, calls)
                self.assertEqual(len(self.state.tasks()), 1)
                self.assert_frozen(snapshot)

    def test_incomplete_rereview_preserves_last_good_publication_and_history(self):
        queue(self.state, 'publish')
        drive(self.engine, self.provider)
        original = self.state.tasks()[0]
        published = copy.deepcopy(self.state.record(original['language'], original['article_id'])['published'])
        snapshot = self.snapshot([self.state.path('content'),
                                  self.state.path(f'state/tasks/{original["id"]}')])
        queue(self.state, 'rereview', operation='review')
        drive(self.engine, self.provider, lambda line: review(30, complete=False))
        rereview = next(task for task in self.state.tasks() if task['campaign'] == 'rereview')
        self.assertEqual((rereview['status'], rereview['failure_kind']), ('not_ready', 'incomplete_review'))
        record = self.state.record(original['language'], original['article_id'])
        self.assertEqual(record['published'], published)
        self.assertTrue(any(item['task'] == original['id'] and item['event'] == 'complete'
                            for item in record['history']))
        self.assert_frozen(snapshot)
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_new_downstream_campaign_freezes_contract_and_reserves_actual_review_schema(self):
        predecessor = self.hold_quality_failure()
        snapshot = self.snapshot([self.state.path(f'state/tasks/{predecessor["id"]}')])
        self.enable_downstream()
        campaign = accept(self.engine, self.downstream_request())
        self.assertEqual(campaign['review_contract_version'], 2)
        self.assertEqual(execution_settings(campaign)['review_contract_version'], 2)
        self.assertEqual(json_hash(execution_settings(campaign)), campaign['execution_settings_sha256'])
        child = self.task(campaign['tasks'][0])
        plan = plan_cycle(self.config, self.state, child, campaign,
                          child['cycle_budget']['max_candidate_bytes'])
        self.assertEqual(plan, child['cycle_budget'])
        self.engine.prepare()
        self.provider.complete_all()
        self.engine.collect()
        child = self.task(child['id'])
        self.assertEqual(child['stage'], 'review2')
        line, _, bound = build_request(self.config, self.state, child)
        schema = line['body']['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['findings']['maxItems'], 30)
        self.assertIn('findings_complete', schema['required'])
        skeleton = copy.deepcopy(line['body'])
        payload = loads(skeleton['messages'][1]['content'])
        payload['translation'] = None
        skeleton['messages'][1]['content'] = canonical(payload).decode('utf-8')
        ceiling = len(canonical(skeleton)) + 4096 + 2 * plan['max_candidate_bytes'] - 4
        self.assertLessEqual(bound, ceiling)
        self.assertEqual(plan['review_reserved_usd'], reserve_cost(
            child['models'][child['review_model']], ceiling, skeleton['max_completion_tokens']))
        self.engine.prepare()
        self.provider.complete_all(lambda line: review(30, complete=False))
        self.engine.collect()
        ended = self.task(child['id'])
        self.assertEqual((ended['status'], ended['failure_kind']), ('not_ready', 'incomplete_review'))
        self.assertEqual((ended['translation_attempts'], ended['review_attempts']), (1, 1))
        calls = self.provider.create_calls
        self.assert_attention_recovery_is_unfunded()
        drive(self.engine, self.provider)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(len(self.state.tasks()), 2)
        self.assertFalse(self.state.projection(self.config)['articles'])
        self.assert_frozen(snapshot)

    def test_legacy_downstream_settings_strategy_and_requests_keep_original_hashes(self):
        self.fixture(None)
        self.hold_quality_failure()
        self.enable_downstream()
        request = self.downstream_request(continuations=False)
        campaign = accept(self.engine, request)
        self.assertNotIn('review_contract_version', campaign)
        settings = execution_settings(campaign)
        self.assertNotIn('review_contract_version', settings)
        self.assertEqual(set(settings), {'model', 'review_model', 'models', 'prompts', 'prompt_version',
                                        'language_settings', 'glossaries', 'max_output_tokens',
                                        'review_output_tokens', 'quality_threshold'})
        child = self.task(campaign['tasks'][0])
        strategy = continuation.strategy(campaign, child['language'])
        frozen = self.snapshot([self.state.path(f'state/campaigns/{campaign["id"]}.json')])
        child['stage'] = 'review2'
        before = canonical(build_request(self.config, self.state, child))
        self.config.runtime['review_contract_version'] = 2
        (self.root / 'prompts/review.txt').write_text('New live review prompt')
        (self.root / 'prompts/repair.txt').write_text('New live repair prompt')
        self.assertEqual(accept(self.engine, request), campaign)
        self.assertNotEqual(canonical(build_request(self.config, self.state, child)), before)
        self.assertEqual(continuation.strategy(campaign, child['language']), strategy)
        self.assertEqual(json_hash(execution_settings(campaign)), campaign['execution_settings_sha256'])
        changed = {**campaign, 'review_contract_version': 2}
        self.assertNotEqual(continuation.strategy(changed, child['language']), strategy)
        self.assertNotEqual(json_hash(execution_settings(changed)), campaign['execution_settings_sha256'])
        self.assert_frozen(frozen)


if __name__ == '__main__':
    unittest.main()
