"""Offline exact-task recovery, immutable provenance, and cumulative-budget tests.

All model responses come from FakeProvider. These fixtures demonstrate runtime
selection and spending safeguards, not genuine translations or model quality.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, json_hash, loads
from berean_translation.requests import build_request
from berean_translation.validation import validate_repository
from support import A, B, FakeProvider, drive, queue, setup


class TargetedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root)
        # Exercise actual offline translation/review/correction/final-review
        # transitions, retaining old results, batches, counters and candidates.
        prompts = {name: self.config.prompt(name) for name in ('translation', 'review')}
        self.config.runtime['prompt_version'] = '1.0.2'
        for name in prompts:
            (self.root / f'prompts/{name}.txt').write_text(
                f'Artificial frozen 1.0.2 {name} prompt.', encoding='utf-8')
        self.original_request = queue(self.state, 'original', languages='afr,deu')
        drive(self.engine, self.provider, self.fail_reviews, ticks=5)
        self.original = self.state.read('state/campaigns/original.json')
        self.assertEqual(self.original['status'], 'finished')
        self.old = {(task['language'], task['article_id']): task for task in self.state.tasks()}
        self.assertEqual(len(self.old), 4)
        self.assertTrue(all(task['status'] == 'not_ready' for task in self.old.values()))
        for name, prompt in prompts.items():
            (self.root / f'prompts/{name}.txt').write_text(prompt, encoding='utf-8')
        self.config.runtime['prompt_version'] = '1.0.3'

    @staticmethod
    def fail_reviews(line):
        if ':review' in line['custom_id']:
            return {'score': 94, 'passed': False, 'findings': []}
        return FakeProvider.default_result(line)

    def request(self, identity='recovery', tasks=None, **changes):
        tasks = [self.old[('afr', A)]['id']] if tasks is None else tasks
        request = {
            'id': identity, 'operation': 'review',
            'recovery_of_campaign': 'original', 'previous_task_ids': tasks,
            'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
            'budget_usd': 1.0, 'dry_run': False, 'retry_failed': False,
            'requested_by': 'fixture-owner',
        }
        request.update(changes)
        return request

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def task(self, identity):
        return self.state.read(f'state/tasks/{identity}/task.json')

    def assert_rejected_without_writes(self, request):
        before = self.files()
        calls = (self.provider.upload_calls, self.provider.create_calls)
        with self.assertRaises(ContractError):
            self.engine.accept_request(request)
        self.assertEqual(self.files(), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)

    def restore_latest(self, old):
        record = self.state.record(old['language'], old['article_id'])
        record['latest_task'] = old['id']
        self.state.save_record(record)

    def test_exact_subset_never_broadens_and_old_history_is_byte_identical(self):
        selected = [self.old[('deu', B)], self.old[('afr', A)]]
        before = self.files()
        calls = (self.provider.upload_calls, self.provider.create_calls)
        request = self.request(tasks=[task['id'] for task in selected])
        with patch.object(self.upstream.client, 'snapshot', side_effect=AssertionError('must reuse pinned source')):
            campaign = self.engine.accept_request(request)
        self.assertEqual(campaign['recovery_of_campaign'], 'original')
        self.assertEqual(campaign['previous_task_ids'], request['previous_task_ids'])
        self.assertEqual(campaign['request_sha256'], json_hash(request))
        self.assertEqual(len(campaign['tasks']), 2)
        self.assertEqual({(row['previous_task_id'], row['article_id'], row['language'])
                          for row in campaign['selection']},
                         {(task['id'], task['article_id'], task['language']) for task in selected})
        self.assertEqual(len(campaign['selection']), 2)
        self.assertEqual(campaign['skipped'], [])
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(campaign['recovery_allocation_usd'], 1.0)
        for identity in campaign['tasks']:
            task = self.task(identity)
            previous = self.old[(task['language'], task['article_id'])]
            self.assertEqual(task['recovery_of_task'], previous['id'])
            self.assertEqual(task['recovery_previous_task_sha256'], json_hash(previous))
            self.assertEqual(task['recovery_candidate_sha256'], json_hash(self.state.candidate(previous)))
            self.assertEqual(task['source_snapshot'], previous['source_snapshot'])
            self.assertEqual(task['translation_key'], previous['translation_key'])
            self.assertEqual(task['translation_model_actual'], previous['translation_model_actual'])
            self.assertEqual(task['stage'], 'review1')
            self.assertEqual(task['status'], 'queued')
            self.assertEqual(task['translation_attempts'], 0)
            self.assertEqual(task['review_attempts'], 0)
            self.assertEqual(task['model'], 'gpt-5-mini')
            self.assertEqual(task['review_model'], 'gpt-5-mini')
            self.assertEqual(self.state.path(f'state/tasks/{identity}/candidate.json').read_bytes(),
                             before[f'state/tasks/{previous["id"]}/candidate.json'])
            record = self.state.record(task['language'], task['article_id'])
            previous_record = loads(before[f'state/records/{task["language"]}/{task["article_id"]}.json'])
            self.assertEqual(record['latest_task'], identity)
            self.assertEqual(record['history'][:-1], previous_record['history'])
        after = self.files()
        for path, data in before.items():
            if path.startswith(('state/tasks/', 'state/batches/', 'state/sources/')) or path == 'state/campaigns/original.json':
                self.assertEqual(after[path], data, path)
        for language, article in (('afr', B), ('deu', A)):
            path = f'state/records/{language}/{article}.json'
            self.assertEqual(after[path], before[path])
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)

    def test_new_source_revision_with_same_fingerprint_reuses_original_snapshot(self):
        old = self.old[('afr', A)]
        self.upstream.revision = 'b' * 40
        self.engine.discover()
        current = self.state.read('state/source.json')
        self.assertEqual(current['articles'][A]['translation_key'], old['translation_key'])
        self.assertNotEqual(current['revision'], self.state.source(old)['revision'])
        campaign = self.engine.accept_request(self.request())
        task = self.task(campaign['tasks'][0])
        self.assertEqual(task['source_snapshot'], old['source_snapshot'])
        self.assertEqual(self.state.source(task)['revision'], 'a' * 40)

    def test_recovery_freezes_current_1_0_3_prompts_and_settings_for_every_stage(self):
        old = self.old[('afr', A)]
        previous_campaign = self.state.path('state/campaigns/original.json').read_bytes()
        campaign = self.engine.accept_request(self.request())
        self.assertEqual(campaign['prompt_version'], '1.0.3')
        self.assertEqual(campaign['prompts'], {name: self.config.prompt(name) for name in ('translation', 'review')})
        self.assertEqual(self.original['prompt_version'], '1.0.2')
        task = self.task(campaign['tasks'][0])
        expected = {}
        for stage in ('review1', 'correct', 'review2'):
            line, cost, bound = build_request(self.config, self.state, {**task, 'stage': stage})
            expected[stage] = (line, cost, bound)
            payload = loads(line['body']['messages'][1]['content'])
            self.assertEqual(payload['translation'], self.state.candidate(old))
            self.assertEqual(payload['source']['html'], self.state.source(old)['html'])
            self.assertIn('source_context', payload)
            self.assertNotIn('byline', payload['source'])
            self.assertIn('copy, retain, or restore English quoted prose as a fidelity workaround',
                          line['body']['messages'][0]['content'])
        # Changes after acceptance cannot rewrite the pinned request contract.
        self.config.runtime.update(prompt_version='future', max_output_tokens=42, review_output_tokens=17)
        self.config.languages['afr']['guidance'] = 'Unrelated future guidance'
        self.config.models['gpt-5-mini']['api_model'] = 'unrelated-future-model'
        for name in ('translation', 'review'):
            (self.root / f'prompts/{name}.txt').write_text('Unrelated future prompt.', encoding='utf-8')
        for stage, result in expected.items():
            with self.subTest(stage=stage):
                self.assertEqual(build_request(self.config, self.state, {**task, 'stage': stage}), result)
        self.assertEqual(self.state.path('state/campaigns/original.json').read_bytes(), previous_campaign)

    def test_identical_replay_is_idempotent_after_latest_pointer_moves(self):
        request = self.request()
        campaign = self.engine.accept_request(request)
        before = self.files()
        self.assertEqual(self.engine.accept_request(copy.deepcopy(request)), campaign)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.state.tasks()), 5)
        for field, value in (('budget_usd', 0.5), ('model', 'gpt-4.1-mini'),
                             ('review_model', 'gpt-4.1-mini'), ('dry_run', True),
                             ('previous_task_ids', [self.old[('afr', B)]['id']]),
                             ('recovery_of_campaign', 'different'), ('requested_by', 'someone-else')):
            with self.subTest(field=field):
                self.assert_rejected_without_writes({**request, field: value})

    def test_normal_campaign_also_rejects_same_id_changed_request(self):
        self.assertEqual(self.engine.accept_request(self.original_request), self.original)
        self.assert_rejected_without_writes({**self.original_request, 'budget_usd': 4})

    def test_dry_run_writes_only_report_without_reservation_or_source_fetch(self):
        request = self.request(dry_run=True)
        before = self.files()
        checkpoints = list(self.git.checkpoints)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        with patch.object(self.upstream.client, 'snapshot', side_effect=AssertionError('dry run must not fetch source')):
            campaign = self.engine.accept_request(request)
        after = self.files()
        self.assertEqual(set(after) - set(before), {'state/campaigns/recovery.json'})
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(self.git.checkpoints, checkpoints)
        self.assertEqual(campaign['status'], 'planned')
        self.assertEqual(campaign['tasks'], [])
        self.assertEqual(len(campaign['selection']), 1)
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(campaign['recovery_allocation_usd'], 0)
        ledger = campaign['recovery_budget']
        # The free report shows a hypothetical post-acceptance balance.
        self.assertAlmostEqual(ledger['remaining_after_usd'],
                               ledger['remaining_before_usd'] - request['budget_usd'], places=6)
        self.assertEqual(ledger['requested_usd'], request['budget_usd'])
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
        self.assertEqual(self.engine.accept_request(request), campaign)
        self.assertEqual(self.files(), after)
        # A preview does not consume the candidate or reserve its envelope.
        accepted = self.engine.accept_request(self.request('accepted-after-preview'))
        self.assertEqual(len(accepted['tasks']), 1)
        self.assertEqual(accepted['recovery_budget']['previously_allocated_usd'], 0)

    def test_cumulative_full_envelopes_leave_parent_budget_and_history_unchanged(self):
        before_parent = self.state.path('state/campaigns/original.json').read_bytes()
        reserved = self.original['reserved_usd']
        available = round(self.original['budget_usd'] - reserved, 6)
        allocations = [1.5, 1.5, round(available - 3.0, 6)]
        previous = 0.0
        for index, old in enumerate(list(self.old.values())[:3]):
            campaign = self.engine.accept_request(self.request(
                f'portion-{index}', tasks=[old['id']], budget_usd=allocations[index]))
            ledger = campaign['recovery_budget']
            self.assertEqual(ledger['original_cap_usd'], self.original['budget_usd'])
            self.assertEqual(ledger['original_reserved_usd'], reserved)
            self.assertAlmostEqual(ledger['previously_allocated_usd'], previous, places=6)
            self.assertEqual(ledger['requested_usd'], allocations[index])
            self.assertAlmostEqual(ledger['remaining_before_usd'], available - previous, places=6)
            self.assertAlmostEqual(ledger['remaining_after_usd'], available - previous - allocations[index], places=6)
            self.assertEqual(campaign['recovery_allocation_usd'], allocations[index])
            self.assertEqual(campaign['reserved_usd'], 0)
            previous += allocations[index]
        last = list(self.old.values())[3]
        self.assert_rejected_without_writes(self.request('over-cap', tasks=[last['id']], budget_usd=0.000001))
        self.assertEqual(self.state.path('state/campaigns/original.json').read_bytes(), before_parent)
        self.assertAlmostEqual(reserved + sum(c['recovery_allocation_usd'] for c in self.state.campaigns()
                                              if c.get('recovery_of_campaign') == 'original'),
                               self.original['budget_usd'], places=6)

    def test_sequential_queue_acceptance_cannot_oversubscribe_before_batches_are_reserved(self):
        first = self.request('a-first', tasks=[self.old[('afr', A)]['id']], budget_usd=3)
        second = self.request('b-second', tasks=[self.old[('deu', B)]['id']], budget_usd=3)
        for request in (first, second):
            self.state.write(f'state/queue/{request["id"]}.json', request)
        calls = self.provider.create_calls
        parent = self.state.path('state/campaigns/original.json').read_bytes()
        self.engine.accept_queue()
        accepted = self.state.read('state/campaigns/a-first.json')
        self.assertEqual(accepted['recovery_allocation_usd'], 3)
        self.assertEqual(accepted['reserved_usd'], 0)
        self.assertIsNone(self.state.read('state/campaigns/b-second.json'))
        self.assertTrue(self.state.read('state/queue-errors/b-second.json')['error'])
        self.assertEqual(self.state.record('deu', B)['latest_task'], self.old[('deu', B)]['id'])
        self.assertEqual(self.state.path('state/campaigns/original.json').read_bytes(), parent)
        self.assertEqual(self.provider.create_calls, calls)

    def test_cancellation_does_not_release_an_accepted_envelope(self):
        self.engine.accept_request(self.request('cancel-me', budget_usd=3))
        self.engine.cancel_campaign('cancel-me')
        cancelled = self.state.read('state/campaigns/cancel-me.json')
        self.assertTrue(cancelled['cancel_requested'])
        self.assertEqual(cancelled['recovery_allocation_usd'], 3)
        self.assertEqual(cancelled['reserved_usd'], 0)
        self.assertEqual(self.task(cancelled['tasks'][0])['status'], 'cancelled')
        self.assert_rejected_without_writes(self.request('too-much-after-cancellation',
            tasks=[self.old[('afr', B)]['id']], budget_usd=3))

    def test_failed_campaign_keeps_its_full_envelope_not_only_actual_reservations(self):
        campaign = self.engine.accept_request(self.request('fail-me', budget_usd=3))
        self.provider.raise_upload = True
        drive(self.engine, self.provider, ticks=4)
        failed = self.state.read('state/campaigns/fail-me.json')
        self.assertEqual(failed['status'], 'finished')
        self.assertEqual(self.task(campaign['tasks'][0])['status'], 'not_ready')
        self.assertLess(failed['reserved_usd'], 3)
        self.assertEqual(failed['recovery_allocation_usd'], 3)
        self.assert_rejected_without_writes(self.request('too-much-after-failure',
            tasks=[self.old[('afr', B)]['id']], budget_usd=3))

    def test_recovered_task_cannot_repeat_even_if_latest_pointer_is_manually_restored(self):
        for index, status in enumerate(('active', 'finished', 'cancelled', 'failed')):
            with self.subTest(campaign_status=status):
                old = list(self.old.values())[index]
                campaign = self.engine.accept_request(self.request(
                    f'prior-{index}', tasks=[old['id']], budget_usd=0.25))
                campaign['status'] = status
                self.state.save_campaign(campaign)
                task = self.task(campaign['tasks'][0])
                task['status'] = 'not_ready'
                self.state.save_task(task)
                self.restore_latest(old)
                self.assert_rejected_without_writes(self.request(
                    f'repeat-{index}', tasks=[old['id']], budget_usd=0.25))

    def test_forbidden_broad_selectors_reject_even_when_empty_or_default(self):
        cases = (
            ('languages', 'all'), ('languages', 'afr'), ('languages', None),
            ('issues', 'next'), ('issues', 'all'), ('issues', None),
            ('article_ids', []), ('article_ids', [A]),
            ('source_translation_keys', {}), ('source_translation_keys', {A: self.old[('afr', A)]['translation_key']}),
            ('source_refresh', False), ('source_refresh', True), ('source_refresh', None),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assert_rejected_without_writes(self.request(**{field: value}))

    def test_overprecise_recovery_budgets_reject_before_any_write(self):
        for budget in ('0.30000000000000004', '1.0000000000000000000000000000001',
                      '0.123456000000000000000000000001', '0.12345678901234567',
                       0.30000000000000004, 0.12345678901234567,
                       '0.0000001', 0.0000001):
            with self.subTest(budget=budget):
                self.assert_rejected_without_writes(self.request(budget_usd=budget))
        campaign = self.engine.accept_request(self.request(budget_usd='0.123456'))
        self.assertEqual(campaign['budget_usd'], 0.123456)
        self.assertEqual(campaign['recovery_allocation_usd'], 0.123456)

    def test_accepted_allocation_checkpoint_survives_loss_of_uncheckpointed_task_writes(self):
        request = self.request('restart-recovery', budget_usd=3)
        durable = []
        calls = (self.provider.upload_calls, self.provider.create_calls)
        parent_before = self.state.path('state/campaigns/original.json').read_bytes()

        def persist_checkpoint(message):
            campaign = self.state.read('state/campaigns/restart-recovery.json')
            if campaign:
                self.assertEqual(campaign['recovery_allocation_usd'], 3)
                self.assertFalse(campaign['recovery_acceptance_complete'])
                self.assertEqual(campaign['tasks'], [])
                self.assertEqual(len(self.state.tasks()), 4)
                self.assertEqual(set(path for path in self.files() if path.startswith('state/tasks/')),
                                 set(path for path in before if path.startswith('state/tasks/')))
                durable.append((message, self.files()))

        before = self.files()
        with patch.object(self.git, 'checkpoint', side_effect=persist_checkpoint):
            with patch.object(self.engine.state, 'save_record', side_effect=OSError('simulated crash after materialization')):
                with self.assertRaises(OSError):
                    self.engine.accept_request(request)
        self.assertEqual(len(durable), 1, 'full envelope must checkpoint before task materialization')
        self.assertEqual(len(self.state.tasks()), 5)
        checkpoint = durable[0][1]
        # Simulate restarting from the last persisted commit, losing local
        # candidate/task writes rather than relying on in-process objects.
        for path in sorted(self.root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() and path.relative_to(self.root).as_posix() not in checkpoint:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for relative, contents in checkpoint.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        self.engine = type(self.engine)(self.config, self.upstream.client, self.provider, self.git)
        campaign = self.state.read('state/campaigns/restart-recovery.json')
        self.assertEqual(len(self.state.tasks()), 4)
        self.assertFalse(campaign['recovery_acceptance_complete'])
        self.assertEqual(campaign['recovery_allocation_usd'], 3)
        self.assertEqual(self.state.record('afr', A)['latest_task'], self.old[('afr', A)]['id'])
        self.assert_rejected_without_writes(self.request('repeat-after-restart'))
        self.assert_rejected_without_writes(self.request('over-cap-after-restart',
            tasks=[self.old[('afr', B)]['id']], budget_usd=3))
        self.engine.prepare()
        self.engine.tick(discover_source=False)
        self.assertEqual(self.state.read('state/campaigns/restart-recovery.json')['status'], 'acceptance_incomplete')
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
        self.assertEqual(self.state.path('state/campaigns/original.json').read_bytes(), parent_before)

    def test_recovery_requires_explicit_budget_and_dry_run_decision(self):
        for field in ('budget_usd', 'dry_run'):
            request = self.request()
            del request[field]
            with self.subTest(missing=field):
                self.assert_rejected_without_writes(request)

    def test_partial_acceptance_retains_envelope_and_blocks_paid_prepare(self):
        request = self.request('interrupted', budget_usd=3)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        # Simulate disk failure after the new task exists but before its record
        # and complete campaign selection have been committed locally.
        with patch.object(self.engine.state, 'save_record', side_effect=OSError('offline simulated disk failure')):
            with self.assertRaises(OSError):
                self.engine.accept_request(request)
        campaign = self.state.read('state/campaigns/interrupted.json')
        self.assertFalse(campaign['recovery_acceptance_complete'])
        self.assertEqual(campaign['recovery_allocation_usd'], 3)
        self.assertEqual(campaign['reserved_usd'], 0)
        pending = [task for task in self.state.tasks() if task['campaign'] == 'interrupted']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'queued')
        with self.assertRaises(ContractError):
            self.engine.prepare()
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)
        self.assert_rejected_without_writes(self.request('repeat-interrupted'))
        self.assert_rejected_without_writes(self.request('over-cap-after-interruption',
            tasks=[self.old[('afr', B)]['id']], budget_usd=3))

    def test_recovery_requires_review_and_false_retry_and_real_boolean_flags(self):
        for field, value in (('operation', 'translate'), ('operation', None),
                             ('retry_failed', True), ('retry_failed', 'false'), ('retry_failed', 0),
                             ('dry_run', 'true'), ('dry_run', 0), ('dry_run', None)):
            with self.subTest(field=field, value=value):
                self.assert_rejected_without_writes(self.request(**{field: value}))

    def test_exact_task_selector_requires_nonempty_unique_known_string_ids(self):
        old = self.old[('afr', A)]['id']
        for value in ([], None, '', old, [old, old], ['f' * 32], [old, 'f' * 32],
                      [None], [123], [[old]], ['../task'], ['']):
            with self.subTest(value=value):
                self.assert_rejected_without_writes(self.request(tasks=value) if value is not None
                                                    else self.request(previous_task_ids=None))
        for field in ('previous_task_ids', 'recovery_of_campaign'):
            request = self.request()
            del request[field]
            with self.subTest(missing=field):
                self.assert_rejected_without_writes(request)

    def test_invalid_or_unknown_parent_rejects_without_creating_partial_state(self):
        for value in ('missing', '', None, 123, '../original'):
            with self.subTest(parent=value):
                self.assert_rejected_without_writes(self.request(recovery_of_campaign=value))

    def test_parent_must_be_finished_original_campaign(self):
        for status in ('active', 'planned', 'cancelled', 'failed'):
            parent = {**self.original, 'status': status}
            self.state.save_campaign(parent)
            with self.subTest(status=status):
                self.assert_rejected_without_writes(self.request())
        self.state.save_campaign({**self.original, 'recovery_of_campaign': 'older-original'})
        self.assert_rejected_without_writes(self.request())

    def test_selection_cannot_mix_campaigns_or_claim_tasks_from_another_parent(self):
        other = copy.deepcopy(self.original)
        other['id'] = 'other-original'
        self.state.save_campaign(other)
        old = copy.deepcopy(self.old[('deu', B)])
        old['campaign'] = 'other-original'
        self.state.save_task(old)
        for tasks in ([old['id']], [self.old[('afr', A)]['id'], old['id']]):
            with self.subTest(tasks=tasks):
                self.assert_rejected_without_writes(self.request(tasks=tasks))

    def test_only_terminal_not_ready_tasks_can_be_selected(self):
        old = self.old[('afr', A)]
        for status in ('queued', 'in_batch', 'complete', 'proposal', 'cancelled',
                       'budget_blocked', 'source_error', 'unknown'):
            self.state.save_task({**old, 'status': status})
            with self.subTest(status=status):
                self.assert_rejected_without_writes(self.request())

    def test_previous_task_must_still_be_latest_for_its_exact_pair(self):
        old = self.old[('afr', A)]
        later = {**old, 'id': 'f' * 32}
        self.state.save_task(later)
        record = self.state.record('afr', A)
        record['latest_task'] = later['id']
        self.state.save_record(record)
        self.assert_rejected_without_writes(self.request())

    def test_missing_candidate_rejects_the_entire_subset_before_any_write(self):
        old = self.old[('deu', B)]
        self.state.path(f'state/tasks/{old["id"]}/candidate.json').unlink()
        self.assert_rejected_without_writes(self.request(tasks=[self.old[('afr', A)]['id'], old['id']]))

    def test_missing_or_tampered_pinned_source_fails_closed(self):
        old = self.old[('afr', A)]
        path = self.state.path(old['source_snapshot'])
        original = path.read_bytes()
        path.unlink()
        self.assert_rejected_without_writes(self.request())
        path.write_bytes(original)
        source = self.state.source(old)
        source['html'] += '<p>Unapproved content</p>'
        self.state.write(old['source_snapshot'], source)
        self.assert_rejected_without_writes(self.request())

    def test_changed_or_withdrawn_current_source_rejects_exact_recovery(self):
        self.upstream.change_source()
        self.engine.discover()
        self.assert_rejected_without_writes(self.request())
        self.upstream.articles = [article for article in self.upstream.articles if article['id'] != A]
        self.upstream.rebuild()
        self.engine.discover()
        self.assert_rejected_without_writes(self.request())

    def test_existing_publication_including_human_reviewed_blocks_recovery(self):
        for human_reviewed in (False, True):
            record = self.state.record('afr', A)
            record['published'] = {'human_reviewed': human_reviewed}
            self.state.save_record(record)
            with self.subTest(human_reviewed=human_reviewed):
                self.assert_rejected_without_writes(self.request())

    def test_untracked_public_html_or_sidecar_alone_blocks_recovery(self):
        for extension in ('html', 'json'):
            path = self.state.path(f'content/afr/articles/{A}.{extension}')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Untracked human-owned file', encoding='utf-8')
            with self.subTest(extension=extension):
                self.assert_rejected_without_writes(self.request())
            path.unlink()

    def test_active_overlapping_task_cannot_hide_behind_restored_latest_pointer(self):
        old = self.old[('afr', A)]
        for status in ('queued', 'in_batch'):
            self.state.save_task({**old, 'id': 'e' * 32, 'campaign': 'active-other', 'status': status})
            self.restore_latest(old)
            with self.subTest(status=status):
                self.assert_rejected_without_writes(self.request())

    def test_unrelated_active_pair_does_not_block_exact_subset(self):
        old = self.old[('deu', B)]
        self.state.save_task({**old, 'id': 'e' * 32, 'campaign': 'active-other', 'status': 'queued'})
        campaign = self.engine.accept_request(self.request())
        self.assertEqual(len(campaign['tasks']), 1)
        self.assertEqual(self.task(campaign['tasks'][0])['article_id'], A)
        self.assertEqual(self.task(campaign['tasks'][0])['language'], 'afr')

    def test_oversized_exact_subset_rejects_without_partial_acceptance(self):
        self.config.runtime['max_tasks_per_request'] = 1
        self.assert_rejected_without_writes(self.request(tasks=[task['id'] for task in self.old.values()]))

    def test_original_campaign_cannot_hide_missing_or_unfinished_work(self):
        old = self.old[('deu', B)]
        self.state.save_task({**old, 'status': 'queued'})
        self.assert_rejected_without_writes(self.request())
        self.state.save_task(old)
        self.state.save_campaign({**self.original, 'tasks': [*self.original['tasks'], 'f' * 32]})
        self.assert_rejected_without_writes(self.request())
        self.state.save_campaign({**self.original, 'dry_run': True})
        self.assert_rejected_without_writes(self.request())

    def test_unfinished_original_batch_blocks_recovery_even_when_tasks_say_finished(self):
        batch = self.state.batches()[0]
        for status in ('prepared', 'submitting', 'submission_unknown', 'submitted', 'cancelling'):
            self.state.save_batch({**batch, 'status': status})
            with self.subTest(status=status):
                self.assert_rejected_without_writes(self.request())

    def test_original_task_requires_unprotected_nonrecovery_model_provenance(self):
        old = self.old[('afr', A)]
        for field, value in (('protected', True), ('recovery_of_task', 'f' * 32),
                             ('translation_model_actual', None), ('batch', 'unfinished-batch')):
            self.state.save_task({**old, field: value})
            with self.subTest(field=field):
                self.assert_rejected_without_writes(self.request())

    def test_malformed_saved_candidate_is_rejected_before_any_new_task(self):
        old = self.old[('afr', A)]
        candidate = self.state.candidate(old)
        cases = (None, [], {}, {**candidate, 'unexpected': 'field'},
                 {**candidate, 'html': ''}, {**candidate, 'html': 123},
                 {**candidate, 'title': False}, {**candidate, 'section': {}})
        for candidate in cases:
            self.state.save_candidate(old, candidate)
            with self.subTest(candidate=candidate):
                self.assert_rejected_without_writes(self.request())

    def test_new_task_identity_collision_never_overwrites_existing_files(self):
        from berean_translation.common import digest
        identity = digest('recovery:afr:' + A)[:32]
        path = self.state.path(f'state/tasks/{identity}/untracked.txt')
        path.parent.mkdir(parents=True)
        path.write_text('Keep this existing task directory', encoding='utf-8')
        self.assert_rejected_without_writes(self.request())

    def test_validator_detects_changed_original_task_or_candidate_after_acceptance(self):
        self.engine.accept_request(self.request())
        validate_repository(self.config)
        old = self.old[('afr', A)]
        task_path = self.state.path(f'state/tasks/{old["id"]}/task.json')
        candidate_path = self.state.path(f'state/tasks/{old["id"]}/candidate.json')
        for path in (task_path, candidate_path):
            original_bytes = path.read_bytes()
            data = loads(original_bytes)
            if path == task_path:
                data['failure'] = 'Rewritten historical conclusion'
            else:
                data['title'] = 'Rewritten historical candidate'
            self.state.write(path.relative_to(self.root).as_posix(), data)
            with self.subTest(path=path.name), self.assertRaises(ContractError):
                validate_repository(self.config)
            path.write_bytes(original_bytes)
        validate_repository(self.config)

    def test_validator_enforces_recovery_links_and_per_task_stage_bounds(self):
        campaign = self.engine.accept_request(self.request())
        task = self.task(campaign['tasks'][0])
        cases = (('recovery_of_task', 'f' * 32), ('recovery_candidate_sha256', 'f' * 64),
                 ('recovery_previous_task_sha256', 'f' * 64), ('stage', 'translate'),
                 ('translation_attempts', 2), ('article_id', B), ('language', 'deu'),
                 ('translation_key', 'f' * 64))
        for field, value in cases:
            self.state.save_task({**task, field: value})
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_repository(self.config)
        self.state.save_task(task)
        validate_repository(self.config)

    def test_validator_rejects_tampered_exact_selection_report(self):
        campaign = self.engine.accept_request(self.request())
        for field, value in (('article_id', B), ('language', 'deu'),
                             ('source_snapshot', self.old[('afr', B)]['source_snapshot'])):
            changed = copy.deepcopy(campaign)
            changed['selection'][0][field] = value
            self.state.save_campaign(changed)
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_repository(self.config)
        self.state.save_campaign(campaign)
        validate_repository(self.config)

    def test_validator_rejects_recycled_or_tampered_allocation_envelopes(self):
        campaign = self.engine.accept_request(self.request())
        cases = (('recovery_allocation_usd', 0), ('recovery_allocation_usd', 0.5),
                 ('recovery_allocation_usd', None), ('budget_usd', 2),
                 ('request_sha256', 'invalid'), ('recovery_acceptance_complete', False))
        for field, value in cases:
            self.state.save_campaign({**campaign, field: value})
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_repository(self.config)
        for field in campaign['recovery_budget']:
            changed = copy.deepcopy(campaign)
            changed['recovery_budget'][field] += 0.25
            self.state.save_campaign(changed)
            with self.subTest(ledger_field=field), self.assertRaises(ContractError):
                validate_repository(self.config)
        self.state.save_campaign(campaign)
        validate_repository(self.config)

    def test_coherently_lowered_budget_and_allocation_cannot_restore_spending_headroom(self):
        campaign = self.engine.accept_request(self.request('allocation-owner', budget_usd=3))
        campaign['budget_usd'] = 1
        campaign['recovery_allocation_usd'] = 1
        ledger = campaign['recovery_budget']
        ledger['requested_usd'] = 1
        ledger['remaining_after_usd'] = round(ledger['remaining_before_usd'] - 1, 6)
        self.state.save_campaign(campaign)
        with self.assertRaises(ContractError):
            validate_repository(self.config)
        self.assert_rejected_without_writes(self.request('spend-recycled-headroom',
            tasks=[self.old[('afr', B)]['id']], budget_usd=3))

    def test_validator_rejects_cumulative_oversubscription_even_with_locally_consistent_reports(self):
        self.engine.accept_request(self.request('first', budget_usd=2))
        second = self.engine.accept_request(self.request(
            'second', tasks=[self.old[('afr', B)]['id']], budget_usd=2))
        second['budget_usd'] = second['recovery_allocation_usd'] = 3
        ledger = second['recovery_budget']
        ledger['requested_usd'] = 3
        # Fabricate a locally balanced report that hides the first allocation.
        ledger['previously_allocated_usd'] = 0
        ledger['remaining_before_usd'] = round(self.original['budget_usd'] - self.original['reserved_usd'], 6)
        ledger['remaining_after_usd'] = round(ledger['remaining_before_usd'] - 3, 6)
        self.state.save_campaign(second)
        with self.assertRaises(ContractError):
            validate_repository(self.config)

    def test_prior_paid_allocation_cannot_hide_behind_changed_parent_or_dry_run(self):
        first = self.engine.accept_request(self.request('first', budget_usd=3))
        for field, value in (('dry_run', True), ('recovery_of_campaign', 'unrelated-original')):
            self.state.save_campaign({**first, field: value})
            with self.subTest(field=field):
                self.assert_rejected_without_writes(self.request('second',
                    tasks=[self.old[('afr', B)]['id']], budget_usd=3))
                with self.assertRaises(ContractError):
                    validate_repository(self.config)
        self.state.save_campaign(first)

    def test_recovery_prepare_reserves_only_current_stage_and_blocks_unfunded_correction(self):
        preview = self.engine.accept_request(self.request('stage-preview', dry_run=True))
        old = self.old[('afr', A)]
        synthetic = {**old, 'campaign': preview['id'], 'stage': 'review1',
                     'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
                     'models': copy.deepcopy(self.config.models)}
        _, review_cost, _ = build_request(self.config, self.state, synthetic)
        budget = round(review_cost, 6)
        parent_before = self.state.path('state/campaigns/original.json').read_bytes()
        campaign = self.engine.accept_request(self.request('one-stage', budget_usd=budget))
        self.assertEqual(campaign['reserved_usd'], 0)
        calls = self.provider.create_calls
        self.engine.prepare()
        task = self.task(campaign['tasks'][0])
        self.assertEqual(task['stage'], 'review1')
        self.assertEqual(task['review_attempts'], 1)
        self.assertEqual(task['translation_attempts'], 0)
        self.assertEqual(self.provider.create_calls, calls + 1)
        self.provider.complete_all(self.fail_reviews)
        self.engine.collect()
        self.engine.prepare()
        task = self.task(task['id'])
        self.assertEqual(task['status'], 'budget_blocked')
        self.assertEqual(task['stage'], 'correct')
        self.assertEqual(task['translation_attempts'], 0)
        self.assertEqual(self.provider.create_calls, calls + 1)
        updated = self.state.read('state/campaigns/one-stage.json')
        self.assertEqual(updated['recovery_allocation_usd'], budget)
        self.assertAlmostEqual(updated['reserved_usd'], budget, places=6)
        self.assertEqual(self.state.path('state/campaigns/original.json').read_bytes(), parent_before)

    def test_full_recovery_can_publish_without_retranslation_or_touching_unselected_tasks(self):
        before = self.files()
        calls = self.provider.create_calls
        campaign = self.engine.accept_request(self.request())
        drive(self.engine, self.provider, ticks=3)
        task = self.task(campaign['tasks'][0])
        self.assertEqual(task['status'], 'complete')
        self.assertEqual(task['review_attempts'], 1)
        self.assertEqual(task['translation_attempts'], 0)
        self.assertEqual(self.provider.create_calls, calls + 1)
        self.assertEqual(validate_repository(self.config)['published'], 1)
        self.assertFalse(self.state.record('afr', A)['published']['human_reviewed'])
        self.assertEqual(self.state.record('afr', A)['published']['task'], task['id'])
        for old in self.old.values():
            for path, contents in before.items():
                if path.startswith(f'state/tasks/{old["id"]}/'):
                    self.assertEqual(self.state.path(path).read_bytes(), contents)
        for pair in (('afr', B), ('deu', A), ('deu', B)):
            self.assertEqual(self.state.record(*pair)['latest_task'], self.old[pair]['id'])
            self.assertIsNone(self.state.record(*pair)['published'])

    def test_failed_recovery_is_bounded_to_one_correction_and_two_reviews(self):
        calls = self.provider.create_calls
        campaign = self.engine.accept_request(self.request())
        drive(self.engine, self.provider, self.fail_reviews, ticks=5)
        task = self.task(campaign['tasks'][0])
        self.assertEqual(task['status'], 'not_ready')
        self.assertEqual(task['translation_attempts'], 1)
        self.assertEqual(task['review_attempts'], 2)
        self.assertEqual(self.provider.create_calls, calls + 3)
        self.assertIsNotNone(self.state.candidate(task))
        self.assertIsNone(self.state.record('afr', A)['published'])
        self.assertEqual(self.state.read('state/campaigns/recovery.json')['status'], 'finished')
        for old in self.old.values():
            self.assertEqual(self.task(old['id'])['translation_attempts'], 2)
            self.assertEqual(self.task(old['id'])['review_attempts'], 2)
        calls = self.provider.create_calls
        drive(self.engine, self.provider, ticks=3)
        self.assertEqual(self.provider.create_calls, calls)
        validate_repository(self.config)


if __name__ == '__main__':
    unittest.main()
