"""Offline lifecycle coverage for append-only, independently funded recovery.

Every provider response is a FakeProvider fixture. These tests exercise admission,
frozen provenance and publication gates, not live models or translation quality.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from berean_translation import continuation
from berean_translation.common import ContractError, canonical, json_hash, loads
from berean_translation.downstream import (
    accept, execution_settings, funding_ledger, ledger,
    validate_history,
)
from berean_translation.requests import build_request
from berean_translation.validation import validate_repository
from support import A, drive, queue, setup


class RecoveryContinuationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root)
        # One held pair makes each selected successor and cost count unambiguous.
        self.upstream.articles = self.upstream.articles[:1]
        self.upstream.rebuild()
        queue(self.state, 'original')
        drive(self.engine, self.provider, self.response('original', 'Faith'))
        self.original = self.state.tasks()[0]
        self.policy = self.config.runtime['automatic_downstream_recovery']
        self.assertEqual(self.original['failure_kind'], 'quality_rejection')
        self.assertEqual(self.original['status'], 'not_ready')

    def manual(self, run='200', *, legacy=False, **changes):
        repository = 'trueChristian/berean-translation'
        request = {'id': 'gh-' + run, 'operation': 'repair',
            'model': 'gpt-6.1-sol', 'review_model': 'gpt-6.1-sol',
            'budget_usd': 2, 'max_articles': 1, 'dry_run': False,
            'requested_by': 'fixture-owner', 'manual_authorization': {
                'kind': 'github_workflow_dispatch', 'repository': repository,
                'workflow_ref': repository + '/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id': run, 'actor': 'fixture-owner'}}
        if not legacy:
            request['continuation_policy'] = continuation.policy()
        request.update(changes)
        return request

    def shared(self, identity='shared-1', **changes):
        request = {'id': identity, 'operation': 'repair',
            'model': self.policy['model'], 'review_model': self.policy['review_model'],
            'budget_usd': self.policy['campaign_budget_usd'],
            'max_articles': self.policy['max_articles'], 'dry_run': False,
            'requested_by': 'fixture-owner', 'continuation_policy': continuation.policy()}
        request.update(changes)
        return request

    def response(self, label, anchor=None, *, score=80, passed=False, fix='Preserve source meaning'):
        def respond(line):
            if ':review' in line['custom_id']:
                return {'score': score if anchor is not None else 97,
                    'passed': passed if anchor is not None else True,
                    'findings': [] if anchor is None else [{
                        'severity': 'major', 'location': 'p', 'source_quote': anchor,
                        'translation_quote': label, 'suggested_fix': fix}]}
            result = self.provider.default_result(line)
            result['html'] = result['html'].replace('Faith and', label + ' and')
            return result
        return respond

    def task(self, identity):
        return self.state.read(f'state/tasks/{identity}/task.json')

    def child(self, campaign):
        self.assertEqual(len(campaign['tasks']), 1, campaign.get('skipped'))
        return self.task(campaign['tasks'][0])

    def submit(self, request):
        self.state.write(f'state/queue/{request["id"]}.json', request)
        return accept(self.engine, request)

    def finish(self, responder=None):
        # Exactly one repair and one review; a third tick establishes quiescence.
        for _ in range(3):
            self.engine.prepare()
            self.provider.complete_all(responder)
            self.engine.collect()
        self.state.derive(self.config)

    def snapshot(self, campaigns):
        paths = []
        for identity in campaigns:
            paths.extend((self.state.path(f'state/campaigns/{identity}.json'),
                          self.state.path(f'state/queue/{identity}.json')))
            campaign = self.state.read(f'state/campaigns/{identity}.json')
            for task_id in campaign['tasks']:
                paths.extend(self.state.path(f'state/tasks/{task_id}').rglob('*'))
        return {path: path.read_bytes() for path in paths if path.is_file()}

    def assert_frozen(self, snapshot):
        for path, content in snapshot.items():
            with self.subTest(historical_file=str(path.relative_to(self.root))):
                self.assertEqual(path.read_bytes(), content)

    def validate(self):
        self.state.derive(self.config)
        validate_history(self.config, self.state, {t['id']: t for t in self.state.tasks()})
        return validate_repository(self.config)

    def reject_cycle(self, run='200', label='first candidate', anchor='grace', **changes):
        campaign = self.submit(self.manual(run, **changes))
        child = self.child(campaign)
        calls = self.provider.create_calls
        self.finish(self.response(label, anchor))
        child = self.task(child['id'])
        self.assertEqual(child['status'], 'not_ready')
        self.assertEqual((child['translation_attempts'], child['review_attempts']), (1, 1))
        self.assertEqual(self.provider.create_calls, calls + 2)
        return campaign, child

    def legacy_sol_failure(self):
        campaign = self.submit(self.manual('100', legacy=True))
        # Construct the actual pre-repair-prompt frozen format before executing
        # it. Subsequent snapshots must remain byte-for-byte unchanged.
        campaign['prompts'].pop('repair')
        campaign['execution_settings_sha256'] = json_hash(execution_settings(campaign))
        self.state.save_campaign(campaign)
        child = self.child(campaign)
        line = build_request(self.config, self.state, child)[0]
        self.assertTrue(line['body']['messages'][0]['content'].startswith(self.config.prompt('translation')))
        self.assertIn('95/100', line['body']['messages'][0]['content'])
        self.finish(self.response('old Sol candidate', 'Faith'))
        child = self.task(child['id'])
        self.assertEqual(child['status'], 'not_ready')
        self.assertNotIn('continuation', child)
        self.validate()
        return campaign, child

    def assert_empty(self, campaign, reason):
        self.assertTrue(campaign['empty_selection'])
        self.assertFalse(campaign['tasks'])
        self.assertEqual(campaign['downstream_allocation_usd'], 0)
        self.assertIn(reason, {item['reason'] for item in campaign['skipped']})

    def change_strategy(self, suffix='Second bounded repair strategy.'):
        path = self.root / 'prompts/repair.txt'
        path.write_text(path.read_text(encoding='utf-8') + '\n' + suffix, encoding='utf-8')

    def test_old_sol_failure_gets_new_child_and_independent_passing_review(self):
        old_campaign, old = self.legacy_sol_failure()
        frozen = self.snapshot(['original', old_campaign['id']])
        request = self.manual()
        original_request = copy.deepcopy(request)
        calls = self.provider.create_calls
        campaign = self.submit(request)
        child = self.child(campaign)
        self.assertEqual(request, original_request)
        self.assertEqual(child['continuation']['cycle'], 2)
        self.assertEqual(child['downstream_previous_task'], old['id'])
        self.assertNotEqual(child['continuation']['strategy_sha256'],
                            continuation.strategy(old_campaign, 'afr'))
        self.assertEqual(child['stage'], 'correct')
        repair = build_request(self.config, self.state, child)[0]
        self.assertTrue(repair['body']['messages'][0]['content'].startswith(self.config.prompt('translation')))
        self.assertIn('95/100', repair['body']['messages'][0]['content'])
        repair_payload = loads(repair['body']['messages'][1]['content'])
        self.assertEqual(repair_payload['translation'], self.state.candidate(old))
        self.assertEqual(repair_payload['correction_findings'], old['findings'])
        self.engine.prepare()
        self.provider.complete_all(self.response('repaired candidate'))
        self.engine.collect()
        child = self.task(child['id'])
        self.assertEqual(child['stage'], 'review2')
        review = loads(build_request(self.config, self.state, child)[0]['body']['messages'][1]['content'])
        self.assertNotIn('correction_findings', review)
        self.assertNotIn('rejection_reason', review)
        self.assertIsNone(self.state.record('afr', A)['published'])
        self.finish()
        child = self.task(child['id'])
        self.assertEqual(child['status'], 'complete')
        self.assertEqual((child['translation_attempts'], child['review_attempts']), (1, 1))
        self.assertEqual(self.provider.create_calls, calls + 2)
        self.assertEqual(self.state.record('afr', A)['published']['task'], child['id'])
        self.assertEqual(self.validate()['ready'], 1)
        self.assert_frozen(frozen)

    def test_new_strategy_does_not_bypass_major_findings_even_at_high_score(self):
        old_campaign, _ = self.legacy_sol_failure()
        frozen = self.snapshot(['original', old_campaign['id']])
        campaign = self.submit(self.manual())
        child = self.child(campaign)
        calls = self.provider.create_calls
        self.finish(self.response('still wrong', 'Faith', score=99, passed=True))
        child = self.task(child['id'])
        self.assertEqual(child['status'], 'not_ready')
        self.assertEqual(child['failure_kind'], 'quality_rejection')
        self.assertEqual((child['translation_attempts'], child['review_attempts']), (1, 1))
        self.assertEqual(self.provider.create_calls, calls + 2)
        self.assertIsNone(self.state.record('afr', A)['published'])
        self.assertFalse(self.state.projection(self.config)['articles'])
        self.assert_frozen(frozen)
        self.validate()

    def test_legacy_requests_keep_once_only_behavior_and_frozen_request_bytes(self):
        campaign, _ = self.legacy_sol_failure()
        frozen = self.snapshot(['original', campaign['id']])
        self.change_strategy()
        again = self.submit(self.manual('201', legacy=True))
        self.assert_empty(again, 'downstream_attempt_exhausted')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.assertNotIn('continuation_policy', campaign['downstream_request'])
        self.assert_frozen(frozen)
        self.validate()

    def test_three_total_cycles_include_old_history_and_changed_strategy_never_resets(self):
        legacy, _ = self.legacy_sol_failure()
        second, second_task = self.reject_cycle(label='second candidate', anchor='grace')
        third, third_task = self.reject_cycle('201', label='third candidate', anchor='A tree')
        self.assertEqual((second_task['continuation']['cycle'], third_task['continuation']['cycle']), (2, 3))
        frozen = self.snapshot(['original', legacy['id'], second['id'], third['id']])
        self.change_strategy('Even a fourth distinct strategy cannot reset the lineage.')
        calls = self.provider.create_calls
        exhausted = self.submit(self.manual('202'))
        self.assert_empty(exhausted, 'continuation_cycles_exhausted_requires_attention')
        self.finish()
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 6)
        self.assert_frozen(frozen)
        self.validate()

    def test_two_cycles_per_strategy_then_only_one_material_changed_strategy_chance(self):
        first, first_task = self.reject_cycle()
        second, second_task = self.reject_cycle('201', label='second candidate', anchor='A tree')
        self.assertEqual(first_task['continuation']['strategy_sha256'], second_task['continuation']['strategy_sha256'])
        blocked = self.submit(self.manual('202'))
        self.assert_empty(blocked, 'continuation_strategy_exhausted_requires_attention')
        self.change_strategy()
        third, third_task = self.reject_cycle('203', label='third candidate', anchor='Faith')
        self.assertEqual(third_task['continuation']['cycle'], 3)
        self.assertEqual(third_task['downstream_previous_task'], second_task['id'])
        self.assertNotEqual(first_task['continuation']['strategy_sha256'], third_task['continuation']['strategy_sha256'])
        self.change_strategy('A third strategy still cannot authorize cycle four.')
        self.assert_empty(self.submit(self.manual('204')), 'continuation_cycles_exhausted_requires_attention')
        self.assertEqual(len(funding_ledger(self.state)['lineages'][third_task['downstream_key']]), 3)
        self.validate()

    def test_unchanged_candidate_stops_without_spending_even_when_findings_change(self):
        self.reject_cycle(label='original', anchor='grace')
        calls = self.provider.create_calls
        blocked = self.submit(self.manual('201'))
        self.assert_empty(blocked, 'continuation_no_candidate_progress_requires_attention')
        self.finish()
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)

    def test_candidate_reversion_to_older_input_is_detected_across_strategy_changes(self):
        self.reject_cycle()
        self.change_strategy()
        _, second = self.reject_cycle('201', label='original', anchor='A tree')
        self.assertEqual(self.state.candidate(second), self.state.candidate(self.original))
        blocked = self.submit(self.manual('202'))
        self.assert_empty(blocked, 'continuation_no_candidate_progress_requires_attention')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 2)
        self.validate()

    def test_reworded_findings_and_quote_punctuation_do_not_claim_progress(self):
        campaign = self.submit(self.manual())
        self.finish(self.response('changed candidate', 'FAITH!',
                                  fix='Different reviewer wording for the same source defect'))
        self.assertEqual(self.child(campaign)['failure_kind'], 'quality_rejection')
        self.assert_empty(self.submit(self.manual('201')), 'continuation_repeated_findings_requires_attention')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.validate()

    def test_ambiguous_or_unsubstantiated_findings_stop_conservatively(self):
        for index, anchor in enumerate(('Faith ... grace', 'faith … grace', 'Absent source phrase', '')):
            with self.subTest(anchor=anchor):
                # Preview each ambiguous terminal result against the same first
                # frozen strategy without adding allocations or successor tasks.
                if index == 0:
                    campaign, child = self.reject_cycle(anchor=anchor)
                else:
                    child['findings'][0]['source_quote'] = anchor
                    self.state.save_task(child)
                blocked = self.submit(self.manual(str(201 + index)))
                self.assert_empty(blocked, 'continuation_progress_uncertain_requires_attention')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)

    def test_duplicate_request_is_idempotent_and_changed_inputs_cannot_reauthorize(self):
        request = self.manual()
        campaign = self.submit(request)
        frozen = self.snapshot(['original', campaign['id']])
        calls = (self.provider.upload_calls, self.provider.create_calls)
        self.assertEqual(accept(self.engine, copy.deepcopy(request)), campaign)
        for changes in ({'budget_usd': 3}, {'max_articles': 2}, {'model': 'gpt-6-luna'},
                        {'continuation_policy': None}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                accept(self.engine, {**request, **changes})
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.assert_frozen(frozen)

    def test_active_overlap_and_restored_predecessor_cannot_fork_lineage(self):
        first = self.submit(self.manual())
        original_record = first['selection'][0]['record_before']
        self.state.save_record(copy.deepcopy(original_record))
        calls = self.provider.create_calls
        blocked = self.submit(self.manual('201'))
        self.assert_empty(blocked, 'already_processing')
        # Retain the real append-only record, then complete the child normally.
        child = self.child(first)
        original_record['latest_task'] = child['id']
        original_record['history'].append({'event': 'downstream_repair_requested',
            'task': child['id'], 'previous_task': self.original['id'],
            'campaign': first['id'], 'at': child['created_at']})
        self.state.save_record(original_record)
        self.finish(self.response('changed candidate', 'grace'))
        restored = copy.deepcopy(self.state.record('afr', A))
        restored['latest_task'] = self.original['id']
        self.state.save_record(restored)
        again = self.submit(self.manual('202'))
        self.assert_empty(again, 'lineage_predecessor_changed_requires_attention')
        self.assertEqual(self.provider.create_calls, calls + 2)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)

    def test_scheduled_cooldown_is_enforced_but_explicit_manual_continuation_is_immediate(self):
        _, child = self.reject_cycle()
        self.policy.update(enabled=True, total_budget_usd=10, campaign_budget_usd=2, max_articles=1)
        at = datetime.fromisoformat(child['finished_at']) + timedelta(minutes=10)
        with patch('berean_translation.downstream.now', return_value=at.isoformat()):
            scheduled = self.shared('downstream-2026100310', scheduled_hour='2026-10-03T10')
            blocked = self.submit(scheduled)
        self.assert_empty(blocked, 'continuation_cooldown')
        immediate = self.submit(self.manual('201'))
        self.assertEqual(self.child(immediate)['continuation']['cycle'], 2)
        self.assertEqual(ledger(self.state)[0], 0)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 4)
        self.validate()

    def test_historical_scheduled_authority_adds_one_append_only_successor(self):
        _, child = self.reject_cycle()
        self.policy.update(enabled=True, total_budget_usd=4, campaign_budget_usd=2, max_articles=1)
        future = datetime.fromisoformat(child['finished_at']) + timedelta(hours=2)
        hour = future.strftime('%Y-%m-%dT%H')
        request = self.shared('downstream-' + future.strftime('%Y%m%d%H'), scheduled_hour=hour)
        with patch('berean_translation.downstream.now', return_value=future.isoformat()):
            campaign = accept(self.engine, request)
        successor = self.child(campaign)
        self.assertEqual(successor['downstream_previous_task'], child['id'])
        self.assertEqual(successor['continuation']['cycle'], 2)
        self.assertEqual(ledger(self.state)[0], 2)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 2)
        self.validate()

    def test_disabled_or_exhausted_shared_funding_never_recycles_manual_or_cancelled_work(self):
        self.reject_cycle()
        calls = self.provider.create_calls
        with self.assertRaisesRegex(ContractError, 'disabled'):
            accept(self.engine, self.shared())
        self.policy.update(enabled=True, total_budget_usd=2, campaign_budget_usd=2, max_articles=1)
        campaign = self.submit(self.shared())
        self.engine.cancel_campaign(campaign['id'])
        self.assertEqual(self.child(campaign)['status'], 'cancelled')
        with self.assertRaisesRegex(ContractError, 'exhausted'):
            accept(self.engine, self.shared('shared-2'))
        self.assertEqual(ledger(self.state)[0], 2)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 2)
        self.assertEqual(self.provider.create_calls, calls)
        self.validate()

    def test_partial_acceptance_and_abort_keep_envelope_and_consume_lineage_slot(self):
        self.reject_cycle()
        request = self.manual('201')
        calls = self.provider.create_calls
        with patch.object(self.engine, 'checkpoint', side_effect=RuntimeError('offline checkpoint failure')):
            with self.assertRaisesRegex(RuntimeError, 'checkpoint'):
                self.submit(request)
        partial = self.state.read(f'state/campaigns/{request["id"]}.json')
        self.assertFalse(partial['downstream_acceptance_complete'])
        self.assertFalse(partial['tasks'])
        self.assertEqual(partial['selection'][0]['continuation']['cycle'], 2)
        self.assertEqual(accept(self.engine, request), partial)
        self.engine.cancel_campaign(partial['id'])
        self.assertEqual(self.state.read(f'state/campaigns/{partial["id"]}.json')['status'], 'acceptance_aborted')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 4)
        self.change_strategy()
        self.assert_empty(self.submit(self.manual('202')), 'lineage_predecessor_changed_requires_attention')
        self.assertEqual(self.provider.create_calls, calls)
        self.validate()

    def test_partially_staged_successor_is_cancelled_without_restoring_funding_or_eligibility(self):
        first, child = self.reject_cycle()
        frozen = self.snapshot(['original', first['id']])
        calls = self.provider.create_calls
        request = self.manual('201')
        with patch.object(self.engine.state, 'save_record', side_effect=RuntimeError('offline staging failure')):
            with self.assertRaisesRegex(RuntimeError, 'staging'):
                self.submit(request)
        partial = self.state.read(f'state/campaigns/{request["id"]}.json')
        self.assertFalse(partial['downstream_acceptance_complete'])
        self.assertFalse(partial['tasks'])
        staged = [task for task in self.state.tasks() if task['campaign'] == partial['id']]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]['downstream_previous_task'], child['id'])
        self.assertEqual(staged[0]['continuation']['cycle'], 2)
        self.engine.cancel_campaign(partial['id'])
        ended = self.state.read(f'state/campaigns/{partial["id"]}.json')
        self.assertEqual(ended['status'], 'acceptance_aborted')
        self.assertEqual(ended['tasks'], [staged[0]['id']])
        self.assertEqual(self.task(staged[0]['id'])['status'], 'cancelled')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 4)
        self.change_strategy()
        again = self.submit(self.manual('202'))
        self.assert_empty(again, 'lineage_predecessor_changed_requires_attention')
        self.assertEqual(self.provider.create_calls, calls)
        self.assert_frozen(frozen)
        self.validate()

    def test_technical_independent_review_failure_never_authorizes_another_rewrite(self):
        campaign = self.submit(self.manual())
        calls = self.provider.create_calls
        valid_generation = self.response('repaired candidate')

        def invalid_review(line):
            return {} if ':review' in line['custom_id'] else valid_generation(line)

        self.finish(invalid_review)
        child = self.child(campaign)
        self.assertEqual(child['status'], 'not_ready')
        self.assertEqual(child['failure_kind'], 'invalid_result')
        self.assertEqual(child['stage'], 'review2')
        self.assertEqual((child['translation_attempts'], child['review_attempts']), (1, 1))
        self.assertEqual(self.provider.create_calls, calls + 2)
        self.change_strategy()
        blocked = self.submit(self.manual('201'))
        self.assert_empty(blocked, 'continuation_review_only_requires_attention')
        self.assertIsNone(self.state.record('afr', A)['published'])
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.validate()

    def test_unrelated_source_revision_preserves_lineage_but_changed_fingerprint_blocks(self):
        _, child = self.reject_cycle()
        self.upstream.revision = 'c' * 40
        self.upstream.rebuild()
        self.engine.discover()
        preview = self.submit(self.manual('201', dry_run=True))
        self.assertEqual(preview['source_revision'], 'c' * 40)
        self.assertEqual(preview['selection'][0]['continuation']['cycle'], 2)
        self.assertEqual(preview['selection'][0]['previous_task_id'], child['id'])
        self.upstream.change_source()
        self.engine.discover()
        self.assert_empty(self.submit(self.manual('202')), 'source_changed')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.validate()

    def test_strategy_identity_ignores_prices_unrelated_registry_and_repository_revision(self):
        campaign, child = self.reject_cycle()
        strategy = child['continuation']['strategy_sha256']
        changes = copy.deepcopy(campaign)
        changes['source_revision'] = 'f' * 40
        changes['models']['gpt-6.1-sol']['input_batch_usd_per_million'] *= 2
        changes['models']['gpt-6.1-sol']['quality_note'] = 'Editorial description changed'
        changes['models']['gpt-4.1-mini']['api_model'] = 'unrelated-registry-model'
        changes['glossaries']['deu'] = {'unrelated': 'entry'}
        changes['language_settings']['deu'] = {'unrelated': True}
        self.assertEqual(continuation.strategy(changes, 'afr'), strategy)
        for section, key, value in (
                ('prompts', 'repair', 'A different applicable repair prompt'),
                ('prompts', 'review', 'A different independent review rubric'),
                ('glossaries', 'afr', {'faith': 'geloof'})):
            changed = copy.deepcopy(campaign)
            changed[section][key] = value
            with self.subTest(section=section, key=key):
                self.assertNotEqual(continuation.strategy(changed, 'afr'), strategy)
        for field, value in (('api_model', 'changed-api-model'), ('reasoning_effort', 'high')):
            changed = copy.deepcopy(campaign)
            changed['models']['gpt-6.1-sol'][field] = value
            with self.subTest(model_field=field):
                self.assertNotEqual(continuation.strategy(changed, 'afr'), strategy)
        for field in ('max_output_tokens', 'review_output_tokens', 'quality_threshold'):
            changed = copy.deepcopy(campaign)
            changed[field] += 1
            with self.subTest(setting=field):
                self.assertNotEqual(continuation.strategy(changed, 'afr'), strategy)

    def test_tampered_old_queue_or_successor_predecessor_fails_before_new_paid_work(self):
        first, child = self.reject_cycle()
        second = self.submit(self.manual('201'))
        calls = self.provider.create_calls
        original_request = first['downstream_request']
        changed_request = copy.deepcopy(original_request)
        changed_request['budget_usd'] = 1
        self.state.write(f'state/queue/{first["id"]}.json', changed_request)
        with self.assertRaises(ContractError):
            self.engine.prepare()
        self.state.write(f'state/queue/{first["id"]}.json', original_request)
        successor = self.child(second)
        successor['downstream_previous_task'] = self.original['id']
        self.state.save_task(successor)
        with self.assertRaises(ContractError):
            self.engine.prepare()
        self.assertEqual(self.provider.create_calls, calls)

    def test_duplicate_cycle_or_forked_selection_cannot_hide_in_funding_history(self):
        first, child = self.reject_cycle()
        second = self.submit(self.manual('201'))
        for field, value in (('cycle', 1), ('cycle', 3)):
            changed = copy.deepcopy(second)
            changed['selection'][0]['continuation'][field] = value
            self.state.save_campaign(changed)
            with self.subTest(cycle=value), self.assertRaises(ContractError):
                funding_ledger(self.state)
        changed = copy.deepcopy(second)
        changed['selection'][0]['previous_task_id'] = self.original['id']
        self.state.save_campaign(changed)
        with self.assertRaisesRegex(ContractError, 'immediate predecessor'):
            funding_ledger(self.state)
        self.state.save_campaign(second)
        self.validate()

    def test_shorter_candidate_tampering_fails_at_every_generation_review_boundary(self):
        campaign = self.submit(self.manual())
        child = self.child(campaign)
        historical = self.snapshot(['original'])

        def reject_tampered_candidate(operation, expected_error):
            before = self.state.candidate(child)
            changed = copy.deepcopy(before)
            changed['html'] = changed['html'].replace('original and', 'x and').replace(
                'valid generated candidate and', 'x and')
            self.assertLess(len(canonical(changed)), len(canonical(before)))
            self.state.save_candidate(child, changed)
            calls = (self.provider.upload_calls, self.provider.create_calls)
            with patch.object(self.provider, 'retrieve', wraps=self.provider.retrieve) as retrieve:
                with self.assertRaisesRegex(ContractError, expected_error):
                    operation()
                retrieve.assert_not_called()
            self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
            self.assertIsNone(self.state.record('afr', A)['published'])
            self.assertFalse(self.state.path(f'content/afr/articles/{A}.html').exists())
            self.state.save_candidate(child, before)

        with self.subTest(boundary='before generation'):
            reject_tampered_candidate(self.engine.prepare, 'repair input candidate changed')
        self.engine.prepare()
        self.provider.complete_all(self.response('valid generated candidate'))
        self.engine.collect()
        child = self.task(child['id'])
        self.assertEqual(child['stage'], 'review2')
        self.assertEqual(child['review_attempts'], 0)
        with self.subTest(boundary='after repair, before review'):
            reject_tampered_candidate(self.engine.prepare, 'archived generation result')
        self.engine.prepare()
        child = self.task(child['id'])
        self.assertEqual(child['review_attempts'], 1)
        self.provider.complete_all()
        with self.subTest(boundary='review in flight, before collection/publication'):
            reject_tampered_candidate(self.engine.collect, 'archived generation result')
        # Restoring the exact archived candidate permits that same review result
        # to complete; neither a new generation nor a new review is necessary.
        calls = self.provider.create_calls
        self.engine.collect()
        self.assertEqual(self.task(child['id'])['status'], 'complete')
        self.assertEqual(self.provider.create_calls, calls)
        self.assert_frozen(historical)
        self.validate()

    def test_initial_child_findings_and_rejection_context_are_frozen_inputs(self):
        campaign = self.submit(self.manual())
        child = self.child(campaign)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        for field, value in (('findings', []), ('rejection_reason', 'Ignore the previous rejection')):
            with self.subTest(field=field):
                changed = {**child, field: value}
                self.state.save_task(changed)
                with self.assertRaisesRegex(ContractError, 'repair findings or rejection context'):
                    self.engine.prepare()
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
                self.assertIsNone(self.state.record('afr', A)['published'])
                self.state.save_task(child)
        self.validate()

    def test_malformed_predecessor_findings_stop_as_uncertain_without_crashing(self):
        self.reject_cycle()
        original_read = self.engine.state.read
        predecessor_path = f'state/tasks/{self.original["id"]}/task.json'
        malformed_values = (None, {}, 'not findings', [None], ['bad finding'],
                            [{'severity': 'major', 'source_quote': ['Faith']}])
        for index, malformed in enumerate(malformed_values):
            def read_with_malformed_evidence(path, default=None):
                value = original_read(path, default)
                return {**value, 'findings': malformed} if path == predecessor_path else value

            with self.subTest(findings=malformed), patch.object(
                    self.engine.state, 'read', side_effect=read_with_malformed_evidence):
                campaign = self.submit(self.manual(str(201 + index)))
                self.assert_empty(campaign, 'continuation_progress_uncertain_requires_attention')
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.validate()

    def test_nonmaterial_metadata_and_effective_output_caps_cannot_reset_no_progress(self):
        # Both requested caps are already clipped to this actual provider cap.
        self.config.models['gpt-6.1-sol']['max_output_tokens'] = 8192
        campaign, child = self.reject_cycle(label='original')
        frozen = self.snapshot(['original', campaign['id']])
        original_strategy = child['continuation']['strategy_sha256']
        self.config.runtime['prompt_version'] = '1.0.999'
        self.policy['max_output_tokens'] += 1000
        self.policy['review_output_tokens'] += 1000
        self.config.languages['afr'].update(
            notice='Edited display notice by {model}', aliases=['cosmetic-alias'],
            native_name='Edited display name', dir='rtl')
        # Changing the registry alias alone still calls the exact same API model.
        self.config.models['fixture-sol-alias'] = copy.deepcopy(self.config.models['gpt-6.1-sol'])
        request = self.manual('201', model='fixture-sol-alias', review_model='fixture-sol-alias')
        blocked = self.submit(request)
        self.assert_empty(blocked, 'continuation_no_candidate_progress_requires_attention')
        # Empty campaigns omit irrelevant language settings; the applicable
        # template still proves this is the same effective model/prompt strategy.
        from berean_translation.downstream import execution_template
        self.assertEqual(continuation.strategy(execution_template(self.config, request), 'afr'), original_strategy)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 1)
        self.assert_frozen(frozen)
        self.validate()

    def test_oversize_candidate_is_preserved_and_only_explicit_larger_manual_cap_can_continue(self):
        request = self.manual(continuation_policy=continuation.policy(1024))
        campaign = self.submit(request)
        child = self.child(campaign)
        calls = self.provider.create_calls
        oversized_label = 'complete candidate content ' * 100
        self.finish(self.response(oversized_label))
        child = self.task(child['id'])
        candidate = self.state.candidate(child)
        self.assertEqual(child['status'], 'not_ready')
        self.assertEqual(child['failure_kind'], 'candidate_size_limit')
        self.assertEqual((child['translation_attempts'], child['review_attempts']), (1, 0))
        self.assertEqual(self.provider.create_calls, calls + 1)
        self.assertGreater(len(canonical(candidate)), 1024)
        self.assertIn(oversized_label, candidate['html'])
        archived = self.state.read(f'state/tasks/{child["id"]}/results/correct.json')
        self.assertEqual(candidate, archived['result'])
        frozen = self.snapshot(['original', campaign['id']])
        self.assert_empty(self.submit(self.manual('201', continuation_policy=continuation.policy(1024))),
                          'continuation_candidate_size_requires_attention')
        self.policy.update(enabled=True, total_budget_usd=4, campaign_budget_usd=2, max_articles=1)
        future = datetime.fromisoformat(child['finished_at']) + timedelta(hours=2)
        with patch('berean_translation.downstream.now', return_value=future.isoformat()):
            scheduled = self.submit(self.shared('downstream-2026100312', scheduled_hour='2026-10-03T12'))
        self.assert_empty(scheduled, 'continuation_candidate_size_requires_attention')
        self.assertEqual(self.provider.create_calls, calls + 1)
        larger = self.submit(self.manual('202', continuation_policy=continuation.policy(8192)))
        successor = self.child(larger)
        self.assertEqual(successor['continuation']['cycle'], 2)
        self.assertEqual(successor['downstream_previous_task'], child['id'])
        self.assertEqual(successor['cycle_budget']['max_candidate_bytes'], 8192)
        self.assertEqual(successor['continuation']['strategy_sha256'], child['continuation']['strategy_sha256'])
        self.finish(self.response(oversized_label))
        self.assertEqual(self.task(successor['id'])['status'], 'complete')
        self.assertEqual(self.provider.create_calls, calls + 3)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 4)
        self.assertEqual(ledger(self.state)[0], 0)
        self.assert_frozen(frozen)
        self.validate()

    def test_frozen_cooldown_admission_is_rechecked_before_submission(self):
        _, previous = self.reject_cycle()
        self.policy.update(enabled=True, total_budget_usd=4, campaign_budget_usd=2, max_articles=1)
        finished_at = datetime.fromisoformat(previous['finished_at'])
        with patch('berean_translation.downstream.now', return_value=(finished_at + timedelta(hours=2)).isoformat()):
            campaign = self.submit(self.shared('downstream-2026100312', scheduled_hour='2026-10-03T12'))
        self.validate()
        # The frozen selection must remain justified at its recorded admission
        # time, even if wall-clock time would make the same pair eligible today.
        campaign['created_at'] = (finished_at + timedelta(minutes=1)).isoformat()
        self.state.save_campaign(campaign)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        with self.assertRaisesRegex(ContractError, 'frozen continuation admission is ineligible'):
            self.engine.prepare()
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)

    def test_frozen_no_progress_admission_cannot_hide_behind_valid_lineage_and_hashes(self):
        self.reject_cycle(label='original')
        # Simulate a faulty selector that persisted otherwise coherent campaign,
        # child, funding and source evidence for an ineligible successor.
        with patch('berean_translation.continuation.next_reason', return_value=None):
            campaign = self.submit(self.manual('201'))
        self.assertEqual(self.child(campaign)['continuation']['cycle'], 2)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_count'], 2)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        with self.assertRaisesRegex(ContractError, 'frozen continuation admission is ineligible'):
            self.engine.prepare()
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
        self.assertIsNone(self.state.record('afr', A)['published'])

    def test_frozen_held_predecessor_gates_reject_coherently_rehashed_ineligible_history(self):
        baseline = self.submit(self.manual())
        baseline_child = self.child(baseline)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        mutations = (
            ('cancelled task', {'status': 'cancelled'}, {}),
            ('already complete task', {'status': 'complete'}, {}),
            ('provider refusal', {'failure_kind': 'provider_refusal'}, {}),
            ('content filter', {'failure_kind': 'content_filter'}, {}),
            ('protected task', {'protected': True}, {}),
            ('unresolved batch', {'batch': 'unfinished-batch'}, {}),
            ('published predecessor', {}, {'published': {'human_reviewed': False}}),
            ('human-reviewed predecessor', {}, {'published': {'human_reviewed': True}}),
            ('different latest predecessor', {}, {'latest_task': 'f' * 32}),
        )
        for label, task_changes, record_changes in mutations:
            with self.subTest(ineligible=label):
                previous = {**self.original, **task_changes}
                changed = copy.deepcopy(baseline)
                item = changed['selection'][0]
                item['previous_task_sha256'] = json_hash(previous)
                item['record_before'].update(record_changes)
                item['record_before_sha256'] = json_hash(item['record_before'])
                child = {**baseline_child, 'downstream_previous_sha256': item['previous_task_sha256']}
                self.state.save_task(previous)
                self.state.save_task(child)
                self.state.save_campaign(changed)
                with self.assertRaisesRegex(ContractError, 'frozen predecessor was not eligible held work'):
                    validate_history(self.config, self.state, {t['id']: t for t in self.state.tasks()})
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
                self.assertIsNone(self.state.record('afr', A)['published'])
                self.state.save_task(self.original)
                self.state.save_task(baseline_child)
                self.state.save_campaign(baseline)
        self.validate()

    def test_derived_frontier_keeps_every_unfinished_category_without_rewriting_history(self):
        campaign, held = self.reject_cycle(label='original')
        languages = sorted(language for language in self.config.languages if language != 'afr')[:5]
        queue(self.state, 'additional-held-pairs', languages=','.join(languages))
        drive(self.engine, self.provider, self.response('other held candidate', 'Faith'))
        by_language = {task['language']: task for task in self.state.tasks()
                       if task['campaign'] == 'additional-held-pairs'}
        self.assertEqual(set(by_language), set(languages))
        # Retain explicit examples of the terminal outcomes the frontier must
        # surface, including ones outside repair's eligible held statuses.
        for language, status in zip(languages[1:4], ('cancelled', 'source_error', 'proposal')):
            task = by_language[language]
            self.state.save_task({**task, 'status': status})
        # A newly accepted ordinary request provides a real active pair without
        # performing any additional simulated provider work.
        active_language = languages[4]
        queue(self.state, 'active-pair', languages=active_language, retry_failed=True)
        self.engine.accept_queue()
        active_campaign = self.state.read('state/campaigns/active-pair.json')
        self.assertEqual(active_campaign['status'], 'active')
        state_bytes = {path: path.read_bytes() for path in self.state.path('state').rglob('*.json')
                       if path.name != 'automatic-status.json'}
        future = datetime.fromisoformat(held['finished_at']) + timedelta(hours=2)
        with patch('berean_translation.autonomous.datetime') as clock:
            clock.now.return_value = future
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.state.derive(self.config)
        self.assertEqual(state_bytes,
                         {path: path.read_bytes() for path in self.state.path('state').rglob('*.json')
                          if path.name != 'automatic-status.json'})
        report = self.state.read('RECOVERY.json')
        self.assertTrue(report['derived'])
        self.assertFalse(report['automatic_enabled'])
        rows = {item['language']: item for item in report['items']}
        expected = {'afr': 'continuation_no_candidate_progress_requires_attention',
                    languages[0]: 'automatic_paused', languages[1]: 'terminal_outcome_requires_attention',
                    languages[2]: 'terminal_outcome_requires_attention',
                    languages[3]: 'terminal_outcome_requires_attention', active_language: 'active'}
        self.assertEqual({language: rows[language]['reason'] for language in expected}, expected)
        self.assertEqual(sum(report['counts'].values()), len(expected))
        summary = self.state.read('state/automatic-status.json')
        self.assertEqual(summary['counts']['unstarted'], len(self.config.languages) - len(expected))
        self.assertEqual(rows['afr']['accepted_cycles'], 1)
        self.assertEqual(self.state.read(f'state/campaigns/{campaign["id"]}.json')['status'], 'finished')
        self.assertFalse(self.state.projection(self.config)['articles'])
        status = self.state.path('STATUS.md').read_text(encoding='utf-8')
        self.assertIn('[Recovery frontier](RECOVERY.json)', status)
        self.assertIn('A finished original campaign remains historical', status)
        self.assertIn('current publication readiness is shown in the issue/language rows', status)


if __name__ == '__main__':
    unittest.main()
