"""Offline manual dispatch funding is separate from the paused hourly policy."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from berean_translation.common import ContractError, json_hash
from berean_translation.downstream import (
    accept, eligible, enqueue_hour, funding_ledger, funding_settings, ledger,
    submission_enabled, validate_history, validate_request,
)
from berean_translation.recovery import money
from berean_translation.validation import validate_repository
from support import setup, queue, drive


class ManualRecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        queue(self.state)
        drive(self.engine, self.provider, self.reject)
        self.originals = copy.deepcopy(self.state.tasks())
        self.original_campaign = self.state.read('state/campaigns/request-1.json')
        self.policy = self.config.runtime['automatic_downstream_recovery']
        self.policy_before = copy.deepcopy(self.policy)

    def reject(self, line):
        if ':review' in line['custom_id']:
            return {'score':80, 'passed':False, 'findings':[{'severity':'major','location':'p',
                'source_quote':'Faith', 'translation_quote':'Wrong', 'suggested_fix':'Preserve faith'}]}
        return self.provider.default_result(line)

    def manual(self, run_id='37045372894', **changes):
        repository = 'trueChristian/berean-translation'
        request = {'id':'gh-' + run_id, 'operation':'repair', 'model':'gpt-6-luna',
            'review_model':'gpt-6-luna', 'budget_usd':10, 'max_articles':3,
            'dry_run':False, 'requested_by':'fixture-owner', 'manual_authorization':{
                'kind':'github_workflow_dispatch', 'repository':repository,
                'workflow_ref':repository + '/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id':run_id, 'actor':'fixture-owner'}}
        request.update(changes)
        return request

    def shared(self, identity='shared-1', **changes):
        request = {'id':identity, 'operation':'repair', 'model':self.policy['model'],
            'review_model':self.policy['review_model'], 'budget_usd':1, 'max_articles':1,
            'dry_run':False, 'requested_by':'fixture-owner'}
        request.update(changes)
        return request

    def finish(self, responder=None):
        for _ in range(3):
            self.engine.prepare(); self.provider.complete_all(responder); self.engine.collect()
        self.state.derive(self.config)

    def validate(self):
        validate_history(self.config, self.state, {t['id']:t for t in self.state.tasks()})

    def assert_originals_unchanged(self):
        for task in self.originals:
            self.assertEqual(task, self.state.read(f'state/tasks/{task["id"]}/task.json'))
        self.assertEqual(self.original_campaign, self.state.read('state/campaigns/request-1.json'))

    def test_disabled_hourly_policy_allows_explicit_manual_run_end_to_end(self):
        calls = self.provider.create_calls
        campaign = accept(self.engine, self.manual())
        self.assertEqual(campaign['funding_scope'], 'manual_workflow')
        self.assertEqual((campaign['allocation_index'], campaign['allocation_before_usd'],
            campaign['approved_total_usd'], campaign['downstream_allocation_usd']), (1, 0, 10, 10))
        self.assertEqual(len(campaign['tasks']), 2)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertTrue(submission_enabled(self.config, campaign))
        self.finish()
        self.assertEqual(self.provider.create_calls, calls + 2)
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        self.assertEqual(ledger(self.state)[0], 0)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.assertEqual(self.policy, self.policy_before)
        self.assert_originals_unchanged()

    def test_manual_prepared_batch_submits_with_hourly_disabled(self):
        campaign = accept(self.engine, self.manual())
        with patch.object(self.engine, 'submit'):
            self.engine.prepare()
        batch = next(b for b in self.state.batches() if b['campaign'] == campaign['id'])
        self.assertEqual(batch['status'], 'prepared')
        self.assertFalse(self.policy['enabled'])
        calls = self.provider.create_calls
        self.engine.submit(batch)
        self.assertEqual(self.provider.create_calls, calls + 1)
        self.validate()

    def test_minimal_disabled_policy_retains_reasoning_review_headroom(self):
        self.config.runtime['automatic_downstream_recovery'] = {'enabled':False}
        campaign = accept(self.engine, self.manual())
        self.assertEqual(campaign['review_output_tokens'], self.config.review_output_limit('gpt-6-luna'))
        self.assertEqual(campaign['review_output_tokens'], 8192)
        self.assertEqual(campaign['approved_total_usd'], 10)
        self.finish()
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        self.assertEqual(self.config.runtime['automatic_downstream_recovery'], {'enabled':False})
        self.assertEqual(ledger(self.state)[0], 0)

    def test_explicit_policy_review_limit_remains_frozen_for_manual_run(self):
        self.policy['review_output_tokens'] = 4096
        campaign = accept(self.engine, self.manual())
        self.assertEqual(campaign['review_output_tokens'], 4096)
        self.policy['review_output_tokens'] = 8192
        self.config.runtime['reasoning_review_output_tokens'] = 16384
        self.assertEqual(accept(self.engine, self.manual())['review_output_tokens'], 4096)
        self.validate()

    def test_same_run_is_idempotent_but_changed_inputs_are_rejected(self):
        request = self.manual()
        self.state.write(f'state/queue/{request["id"]}.json', request)
        accepted = accept(self.engine, request)
        before = {str(p):p.read_bytes() for p in self.state.path('state').rglob('*.json')}
        self.assertEqual(accept(self.engine, copy.deepcopy(request)), accepted)
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.state.path('state').rglob('*.json')})
        for changes in ({'budget_usd':9}, {'model':'gpt-6-astra'},
                        {'review_model':'gpt-6.1-sol'}, {'max_articles':1}, {'dry_run':True}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                accept(self.engine, self.manual(**changes))
        changed = self.manual(); changed.pop('manual_authorization')
        with self.assertRaises(ContractError): accept(self.engine, changed)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)

    def test_manual_authorization_schema_and_identity_fail_closed(self):
        valid = self.manual()
        malformed = [None, {}, {**valid['manual_authorization'], 'unexpected':True}]
        for field, value in (('kind','schedule'), ('repository','../elsewhere'),
                ('workflow_ref','trueChristian/berean-translation/.github/workflows/ai-repair.yml@refs/heads/other'),
                ('run_id',37045372894), ('run_id','0'), ('run_id','037045372894'),
                ('run_id','37045372895'), ('actor','different-owner'), ('actor',''), ('actor',True)):
            malformed.append({**valid['manual_authorization'], field:value})
        for authorization in malformed:
            with self.subTest(authorization=authorization), self.assertRaises(ContractError):
                validate_request(self.config, self.manual(manual_authorization=authorization))
        for changes in ({'id':'other'}, {'requested_by':'other'}, {'budget_usd':0},
                {'budget_usd':501}, {'max_articles':6}, {'max_articles':True},
                {'funding_scope':'manual_workflow'}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_request(self.config, self.manual(**changes))

    def test_manual_authorization_cannot_be_combined_with_scheduled_hour(self):
        for hour in (None, '', '2026-10-02T12'):
            with self.subTest(hour=hour), self.assertRaises(ContractError):
                validate_request(self.config, self.manual(scheduled_hour=hour))
        request = self.manual(id='downstream-2026100212', scheduled_hour='2026-10-02T12')
        with self.assertRaises(ContractError): accept(self.engine, request)
        request.pop('manual_authorization')
        with self.assertRaisesRegex(ContractError, 'disabled'):
            accept(self.engine, request)
        enqueue_hour(self.engine)
        self.assertFalse(list(self.state.path('state/queue').glob('downstream-*.json')))
        self.assertEqual(self.policy['total_budget_usd'], 0)

    def test_legacy_manual_requests_still_require_shared_policy(self):
        request = self.manual(); request.pop('manual_authorization')
        with self.assertRaisesRegex(ContractError, 'disabled'): accept(self.engine, request)
        self.policy.update(enabled=True, total_budget_usd=1)
        with self.assertRaisesRegex(ContractError, 'lifetime spending envelope exhausted'):
            accept(self.engine, request)
        self.assertFalse(funding_ledger(self.state)['manual_workflow_count'])

    def test_manual_and_shared_allocations_keep_distinct_sequences_and_global_keys(self):
        # Four independently held article/language pairs provide interleaved allocations.
        queue(self.state, identity='request-2', languages='deu')
        drive(self.engine, self.provider, self.reject)
        self.policy.update(enabled=True, total_budget_usd=2)
        manual1 = accept(self.engine, self.manual(max_articles=1, budget_usd=3))
        shared1 = accept(self.engine, self.shared())
        manual2 = accept(self.engine, self.manual('37045372895', max_articles=1, budget_usd=4))
        shared2 = accept(self.engine, self.shared('shared-2'))
        self.assertEqual((manual1['allocation_index'], manual2['allocation_index']), (1, 1))
        self.assertEqual((shared1['allocation_index'], shared2['allocation_index']), (1, 2))
        self.assertEqual((shared1['allocation_before_usd'], shared2['allocation_before_usd']), (0, 1))
        totals = funding_ledger(self.state)
        self.assertEqual((totals['shared_policy_usd'], totals['manual_workflow_usd']), (2, 7))
        self.assertEqual((totals['shared_policy_count'], totals['manual_workflow_count']), (2, 2))
        self.assertEqual(len(ledger(self.state)[1]), 4)
        self.validate()
        # Existing historical campaigns without the new fields remain valid.
        shared1.pop('funding_scope'); shared1.pop('funding_sha256')
        self.state.save_campaign(shared1)
        self.validate()
        self.assertEqual(ledger(self.state)[0], 2)

    def test_manual_failures_are_permanent_and_unavailable_to_shared_recovery(self):
        campaign = accept(self.engine, self.manual())
        self.finish(self.reject)
        self.policy.update(enabled=True, total_budget_usd=1)
        again = accept(self.engine, self.shared(max_articles=2))
        self.assertTrue(again['empty_selection'])
        self.assertFalse(again['tasks'])
        self.assertEqual(ledger(self.state)[0], 0)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        for original in self.originals:
            self.assertEqual(eligible(self.state, self.config, original, ledger(self.state)[1],
                self.state.tasks()), 'downstream_attempt_exhausted')
        self.assertTrue(all(self.state.read(f'state/tasks/{i}/task.json')['status'] == 'not_ready'
            for i in campaign['tasks']))
        self.validate()

    def test_shared_failures_are_unavailable_to_manual_recovery(self):
        self.policy.update(enabled=True, total_budget_usd=1)
        accept(self.engine, self.shared(max_articles=2))
        self.finish(self.reject)
        self.policy.update(enabled=False, total_budget_usd=0)
        campaign = accept(self.engine, self.manual())
        self.assertTrue(campaign['empty_selection'])
        self.assertEqual(campaign['downstream_allocation_usd'], 0)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 0)
        self.assertEqual(ledger(self.state)[0], 1)
        self.validate()

    def test_manual_preview_and_empty_run_allocate_nothing(self):
        preview = accept(self.engine, self.manual(dry_run=True))
        self.assertEqual(len(preview['selection']), 2)
        self.assertFalse(preview['tasks'])
        self.assertEqual(ledger(self.state), (money(0), set()))
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 0)
        campaign = accept(self.engine, self.manual('37045372895'))
        empty = accept(self.engine, self.manual('37045372896'))
        self.assertTrue(empty['empty_selection'])
        self.assertFalse(empty['tasks'])
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.assertEqual(len(ledger(self.state)[1]), len(campaign['selection']))
        self.validate()

    def test_cancellation_never_recycles_manual_allocation(self):
        campaign = accept(self.engine, self.manual())
        self.engine.cancel_campaign(campaign['id'])
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.assertEqual(len(ledger(self.state)[1]), 2)
        again = accept(self.engine, self.manual('37045372895'))
        self.assertTrue(again['empty_selection'])
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.assertEqual(ledger(self.state)[0], 0)
        self.assert_originals_unchanged()
        self.validate()

    def test_checkpoint_failure_and_partial_abort_retain_manual_envelope(self):
        with patch.object(self.engine, 'checkpoint', side_effect=RuntimeError('simulated push failure')):
            with self.assertRaises(RuntimeError): accept(self.engine, self.manual())
        campaign = self.state.read('state/campaigns/gh-37045372894.json')
        self.assertFalse(campaign['downstream_acceptance_complete'])
        self.assertEqual(accept(self.engine, self.manual()), campaign)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.engine.cancel_campaign(campaign['id'])
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.validate()

    def test_partial_child_staging_can_cancel_without_manual_budget_reset(self):
        with patch.object(self.engine.state, 'save_record', side_effect=RuntimeError('staging failed')):
            with self.assertRaises(RuntimeError): accept(self.engine, self.manual())
        campaign = self.state.read('state/campaigns/gh-37045372894.json')
        self.assertFalse(campaign['tasks'])
        self.engine.cancel_campaign(campaign['id'])
        ended = self.state.read('state/campaigns/gh-37045372894.json')
        self.assertEqual(ended['status'], 'acceptance_aborted')
        self.assertEqual(len(ended['tasks']), 1)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.validate()

    def test_queued_competing_runs_cannot_allocate_same_source_twice(self):
        for run_id in ('37045372894', '37045372895'):
            request = self.manual(run_id)
            self.state.write(f'state/queue/{request["id"]}.json', request)
        self.engine.accept_queue()
        first = self.state.read('state/campaigns/gh-37045372894.json')
        second = self.state.read('state/campaigns/gh-37045372895.json')
        self.assertEqual(len(first['tasks']), 2)
        self.assertTrue(second['empty_selection'])
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.assertEqual(len(ledger(self.state)[1]), 2)
        self.validate()

    def test_manual_funding_cap_scope_and_hash_tampering_fail_closed(self):
        baseline = accept(self.engine, self.manual())
        mutations = []
        for field, value in (('approved_total_usd',11), ('allocation_index',2),
                ('allocation_index',True), ('allocation_before_usd',1),
                ('downstream_allocation_usd',0), ('budget_usd',11),
                ('funding_scope','shared_policy'), ('funding_scope','unknown'),
                ('funding_sha256','changed'), ('request_sha256','changed')):
            changed = copy.deepcopy(baseline); changed[field] = value
            mutations.append(changed)
        changed = copy.deepcopy(baseline)
        changed.pop('funding_scope'); changed.pop('funding_sha256'); mutations.append(changed)
        changed = copy.deepcopy(baseline)
        changed['downstream_request'].pop('manual_authorization')
        changed['request_sha256'] = json_hash(changed['downstream_request']); mutations.append(changed)
        changed = copy.deepcopy(baseline)
        changed['downstream_request']['scheduled_hour'] = None
        changed['request_sha256'] = json_hash(changed['downstream_request']); mutations.append(changed)
        # Even a recomputed funding hash cannot turn a manual cap into a larger cap.
        changed = copy.deepcopy(baseline); changed['approved_total_usd'] = 11
        changed['funding_sha256'] = json_hash(funding_settings(changed)); mutations.append(changed)
        calls = self.provider.create_calls
        for changed in mutations:
            with self.subTest(changed=changed):
                self.state.save_campaign(changed)
                with self.assertRaises(ContractError): ledger(self.state)
                with self.assertRaises(ContractError): self.engine.prepare()
                self.state.save_campaign(baseline)
        self.assertEqual(calls, self.provider.create_calls)
        self.validate()

    def test_manual_execution_settings_and_queued_provenance_remain_frozen(self):
        request = self.manual()
        self.state.write(f'state/queue/{request["id"]}.json', request)
        campaign = accept(self.engine, request)
        changed = copy.deepcopy(campaign); changed['review_output_tokens'] += 1
        self.state.save_campaign(changed)
        with self.assertRaises(ContractError): self.validate()
        self.state.save_campaign(campaign)
        request['manual_authorization']['actor'] = 'other-owner'
        self.state.write(f'state/queue/{request["id"]}.json', request)
        with self.assertRaises(ContractError): ledger(self.state)

    def test_status_separates_manual_allocations_from_disabled_hourly_zero(self):
        accept(self.engine, self.manual())
        self.state.derive(self.config)
        text = self.state.path('STATUS.md').read_text()
        self.assertIn('Paused: hourly/shared-policy recovery submissions are disabled', text)
        self.assertIn('Accepted lifetime recovery allocations: $0.000000 / $0.00.', text)
        self.assertIn('Separately authorized manual workflow allocations: $10.000000 across 1 accepted runs.', text)
        self.assertIn('do not consume or enable the hourly policy', text)
        self.assertEqual(self.policy, self.policy_before)
