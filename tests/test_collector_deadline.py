"""Offline deadlines yield only at durable, replay-safe runtime boundaries.

Fake clocks model slow checkpoints without sleeping. All provider work and
historical state below are temporary fixtures, never live Batch requests.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from berean_translation import continuation
from berean_translation import downstream
from berean_translation.collector import collect_window
from berean_translation.downstream import accept, funding_ledger
from berean_translation.validation import validate_repository
from support import drive, queue, setup


class FakeClock:
    def __init__(self):
        self.seconds = 0
        self.sleeps = []

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds

    def expire(self):
        self.seconds = 600

    def continue_work(self):
        return self.seconds < 600


class CollectorDeadlineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root)

    def run_window(self, clock, **kwargs):
        return collect_window(self.engine, monotonic=clock.monotonic,
                              sleep=clock.sleep, **kwargs)

    def prepared_batch(self):
        queue(self.state)
        self.engine.discover()
        self.engine.accept_queue()
        with patch.object(self.engine, 'submit'):
            self.engine.prepare()
        batch, = self.state.batches()
        self.assertEqual(batch['status'], 'prepared')
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)
        return batch

    def expire_at_checkpoint(self, clock, phrase):
        original = self.git.checkpoint

        def checkpoint(message):
            original(message)
            if phrase in message:
                clock.expire()

        return patch.object(self.git, 'checkpoint', side_effect=checkpoint)

    def frozen_reservation(self, batch):
        campaign = self.state.read(f'state/campaigns/{batch["campaign"]}.json')
        return {
            'batch_id': batch['id'],
            'payload': self.state.path(f'state/batches/{batch["id"]}/input.jsonl').read_bytes(),
            'reserved_usd': batch['reserved_usd'],
            'campaign_reserved_usd': campaign['reserved_usd'],
            'attempts': {task['id']: (task['translation_attempts'], task['review_attempts'])
                         for task in self.state.tasks()},
        }

    def test_slow_checkpoints_stop_many_batches_before_twenty_minute_job_limit(self):
        # The unrestricted legacy tick crosses the real 20-minute job timeout:
        # 20 one-request batches each require four 41-second checkpoints.
        self.config.runtime['max_batch_requests'] = 1
        languages = ','.join(list(self.config.languages)[:10])
        queue(self.state, languages=languages, budget_usd=50)
        clock = FakeClock()
        original = self.git.checkpoint

        def slow_checkpoint(message):
            original(message)
            clock.seconds += 41

        with patch.object(self.git, 'checkpoint', side_effect=slow_checkpoint):
            result = self.run_window(clock, wait_seconds=600)

        self.assertEqual(result['ticks'], 1)
        self.assertEqual(result['stop_reason'], 'wait_budget_exhausted')
        self.assertEqual(clock.sleeps, [])
        self.assertGreaterEqual(clock.seconds, 600)
        # Allow the current durable operation plus final publication checkpoint.
        self.assertLessEqual(clock.seconds, 600 + 3 * 41)
        self.assertLess(clock.seconds, 20 * 60)
        self.assertGreater(self.provider.create_calls, 0)
        self.assertLess(self.provider.create_calls, 20)
        self.assertTrue(any(task['status'] == 'queued' for task in self.state.tasks()))
        self.assertEqual(self.state.read('state/heartbeat.json')['pending_tasks'], 20)
        campaign, = self.state.campaigns()
        self.assertEqual(sum(campaign['task_counts'].values()), 20)
        self.assertEqual(self.git.checkpoints[-1],
                         'runtime: update source discovery and translation publication index')
        self.assertEqual(validate_repository(self.config)['tasks'], 20)

        # Disabling the window retains the existing unrestricted, single tick.
        with tempfile.TemporaryDirectory() as other:
            config, state, _, provider, git, engine = setup(Path(other))
            config.runtime['max_batch_requests'] = 1
            queue(state, languages=languages, budget_usd=50)
            legacy_clock = FakeClock()
            old_checkpoint = git.checkpoint

            def legacy_checkpoint(message):
                old_checkpoint(message)
                legacy_clock.seconds += 41

            with patch.object(git, 'checkpoint', side_effect=legacy_checkpoint):
                legacy = collect_window(engine, wait_seconds=0,
                    monotonic=legacy_clock.monotonic, sleep=legacy_clock.sleep)
            self.assertEqual(legacy['ticks'], 1)
            self.assertEqual(legacy_clock.sleeps, [])
            self.assertEqual(provider.create_calls, 20)
            self.assertGreater(legacy_clock.seconds, 20 * 60)
            validate_repository(config)

    def test_collection_yields_between_batches_and_resumes_terminal_decisions_once(self):
        self.config.runtime['max_batch_requests'] = 1
        queue(self.state)
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, 2)
        self.provider.complete_all(lambda line: None)

        def continue_work():
            return not any(batch['status'] == 'collected' for batch in self.state.batches())

        self.engine.tick(discover_source=False, continue_work=continue_work)
        self.assertEqual(sorted(batch['status'] for batch in self.state.batches()),
                         ['collected', 'submitted'])
        self.assertEqual(sorted(task['status'] for task in self.state.tasks()),
                         ['in_batch', 'not_ready'])
        campaign, = self.state.campaigns()
        self.assertEqual(campaign['task_counts'], {'in_batch': 1, 'not_ready': 1})
        self.assertEqual(campaign['status'], 'active')
        validate_repository(self.config)

        terminal = next(task for task in self.state.tasks() if task['status'] == 'not_ready')
        frozen = {path: path.read_bytes()
                  for path in self.state.path(f'state/tasks/{terminal["id"]}').rglob('*')
                  if path.is_file()}
        record = copy.deepcopy(self.state.record(terminal['language'], terminal['article_id']))
        self.engine.tick(discover_source=False)
        self.engine.tick(discover_source=False)
        self.assertEqual(self.provider.create_calls, 2)
        self.assertTrue(all(task['status'] == 'not_ready' for task in self.state.tasks()))
        self.assertEqual(self.state.campaigns()[0]['status'], 'finished')
        self.assertEqual(self.state.campaigns()[0]['task_counts'], {'not_ready': 2})
        self.assertEqual(record, self.state.record(terminal['language'], terminal['article_id']))
        self.assertEqual(frozen, {path: path.read_bytes() for path in frozen})
        self.assertEqual(validate_repository(self.config)['ready'], 0)

    def test_queued_acceptance_finishes_one_whole_request_before_yielding(self):
        queue(self.state, 'first', languages='afr')
        queue(self.state, 'second', languages='deu')
        self.engine.discover()
        clock = FakeClock()
        original = self.engine.accept_request

        def accept_and_expire(request):
            campaign = original(request)
            clock.expire()
            return campaign

        with patch.object(self.engine, 'accept_request', side_effect=accept_and_expire):
            self.engine.tick(discover_source=False, continue_work=clock.continue_work)
        self.assertEqual([campaign['id'] for campaign in self.state.campaigns()], ['first'])
        self.assertEqual(len(self.state.tasks()), 2)
        self.assertTrue(all(task['status'] == 'queued' for task in self.state.tasks()))
        self.assertEqual(self.state.campaigns()[0]['task_counts'], {'queued': 2})
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)
        validate_repository(self.config)

        self.engine.tick(discover_source=False)
        self.assertEqual(len(self.state.campaigns()), 2)
        self.assertEqual(len(self.state.tasks()), 4)
        self.assertEqual(self.provider.create_calls, 2)
        validate_repository(self.config)

    def test_expiry_after_preparation_reuses_exact_payload_reservation_and_attempt(self):
        queue(self.state)
        self.engine.discover()
        self.engine.accept_queue()
        clock = FakeClock()
        with self.expire_at_checkpoint(clock, 'reserve task identities and budget'):
            self.engine.prepare(continue_work=clock.continue_work)
        batch, = self.state.batches()
        self.assertEqual(batch['status'], 'prepared')
        self.assertIsNone(batch['input_file_id'])
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
        self.assertTrue(all(task['translation_attempts'] == 1 for task in self.state.tasks()))
        frozen = self.frozen_reservation(batch)
        validate_repository(self.config)

        self.engine.submit(batch, continue_work=clock.continue_work)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
        self.engine.collect()
        resumed, = self.state.batches()
        self.assertEqual(resumed['status'], 'submitted')
        self.assertEqual(self.frozen_reservation(resumed), frozen)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 1))
        validate_repository(self.config)

    def test_expiry_after_upload_checkpoint_reuses_saved_file_before_one_create(self):
        batch = self.prepared_batch()
        frozen = self.frozen_reservation(batch)
        clock = FakeClock()
        with self.expire_at_checkpoint(clock, 'persist OpenAI input file'):
            self.engine.submit(batch, continue_work=clock.continue_work)
        uploaded, = self.state.batches()
        self.assertEqual(uploaded['status'], 'prepared')
        self.assertIsNotNone(uploaded['input_file_id'])
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 0))
        self.assertEqual(self.provider.files[uploaded['input_file_id']], frozen['payload'])
        self.assertEqual(self.frozen_reservation(uploaded), frozen)
        validate_repository(self.config)

        self.engine.submit(uploaded, continue_work=clock.continue_work)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 0))
        self.engine.submit(self.state.batches()[0])
        resumed, = self.state.batches()
        self.assertEqual(resumed['status'], 'submitted')
        self.assertEqual(resumed['input_file_id'], uploaded['input_file_id'])
        self.assertEqual(self.frozen_reservation(resumed), frozen)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 1))
        validate_repository(self.config)

    def test_expiry_after_durable_intent_finishes_create_and_outcome_checkpoint(self):
        batch = self.prepared_batch()
        clock = FakeClock()
        with self.expire_at_checkpoint(clock, 'submission intent'):
            self.engine.submit(batch, continue_work=clock.continue_work)
        submitted, = self.state.batches()
        self.assertEqual(clock.seconds, 600)
        self.assertEqual(submitted['status'], 'submitted')
        self.assertIn(submitted['remote_id'], self.provider.batches)
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(self.git.checkpoints[-1],
                         'runtime: persist OpenAI batch identity or uncertain-submission state')
        self.engine.collect()
        self.assertEqual(self.provider.create_calls, 1)
        validate_repository(self.config)

    def test_expired_intent_with_lost_success_is_reconciled_without_recreating(self):
        batch = self.prepared_batch()
        self.provider.raise_create = 'after'
        clock = FakeClock()
        with self.expire_at_checkpoint(clock, 'submission intent'):
            self.engine.submit(batch, continue_work=clock.continue_work)
        self.assertEqual(self.state.batches()[0]['status'], 'submission_unknown')
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(len(self.provider.batches), 1)
        self.assertEqual(self.git.checkpoints[-1],
                         'runtime: persist OpenAI batch identity or uncertain-submission state')
        validate_repository(self.config)

        self.provider.raise_create = None
        self.engine.collect()
        self.assertEqual(self.state.batches()[0]['status'], 'submitted')
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(self.provider.upload_calls, 1)
        validate_repository(self.config)

    def test_failed_intent_checkpoint_blocks_create_and_replay_remains_uncertain(self):
        batch = self.prepared_batch()
        original = self.git.checkpoint

        def fail_intent(message):
            if 'submission intent' in message:
                raise RuntimeError('simulated checkpoint failure')
            original(message)

        with patch.object(self.git, 'checkpoint', side_effect=fail_intent):
            with self.assertRaisesRegex(RuntimeError, 'simulated checkpoint failure'):
                self.engine.submit(batch, continue_work=lambda: True)
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(self.state.batches()[0]['status'], 'submitting')
        self.engine.collect()
        self.engine.collect()
        self.assertEqual(self.state.batches()[0]['status'], 'submission_unknown')
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(self.provider.upload_calls, 1)
        validate_repository(self.config)

    def test_failed_outcome_checkpoint_does_not_repeat_successful_create(self):
        batch = self.prepared_batch()
        clock = FakeClock()
        original = self.git.checkpoint

        def fail_outcome(message):
            if 'submission intent' in message:
                clock.expire()
            if 'batch identity or uncertain-submission state' in message:
                raise RuntimeError('simulated outcome checkpoint failure')
            original(message)

        with patch.object(self.git, 'checkpoint', side_effect=fail_outcome):
            with self.assertRaisesRegex(RuntimeError, 'simulated outcome checkpoint failure'):
                self.engine.submit(batch, continue_work=clock.continue_work)
        self.assertEqual(self.state.batches()[0]['status'], 'submitted')
        self.assertEqual(self.provider.create_calls, 1)
        self.engine.collect()
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(self.provider.upload_calls, 1)
        validate_repository(self.config)

    def test_poll_that_reaches_deadline_never_starts_an_extra_tick(self):
        queue(self.state)
        clock = FakeClock()
        original_sleep = clock.sleep

        def oversleep(seconds):
            original_sleep(seconds)
            clock.expire()

        clock.sleep = oversleep
        with patch.object(self.engine, 'tick', wraps=self.engine.tick) as tick:
            result = self.run_window(clock, wait_seconds=600)
        self.assertEqual(result['ticks'], 1)
        self.assertEqual(tick.call_count, 1)
        self.assertEqual(clock.sleeps, [60])
        self.assertEqual(result['stop_reason'], 'wait_budget_exhausted')
        self.assertEqual(self.provider.create_calls, 1)
        validate_repository(self.config)

    def test_default_and_zero_wait_call_one_unrestricted_legacy_tick(self):
        # No callback is imposed on users of the original one-tick interface.
        for kwargs in ({}, {'wait_seconds': 0}):
            with self.subTest(kwargs=kwargs):
                engine = Mock()
                engine.provider = object()
                engine.state.batches.return_value = [{'status': 'submitted'}]
                clock = FakeClock()
                result = collect_window(engine, monotonic=clock.monotonic,
                                        sleep=clock.sleep, **kwargs)
                engine.tick.assert_called_once()
                self.assertEqual(engine.tick.call_args.args, ())
                self.assertIsNone(engine.tick.call_args.kwargs.get('continue_work'))
                self.assertEqual(result['ticks'], 1)
                self.assertEqual(clock.sleeps, [])

    def test_slow_polled_prefix_cannot_starve_the_eighth_completed_batch(self):
        self.config.runtime['max_batch_requests'] = 1
        queue(self.state, languages=','.join(list(self.config.languages)[:4]))
        with patch('berean_translation.engine.now', return_value='2026-10-04T00:00:00Z'):
            self.engine.tick()
        batches = self.state.batches()
        self.assertEqual(len(batches), 8)
        self.provider.complete_all(lambda line: None)
        for batch in batches[:-1]:
            self.provider.batches[batch['remote_id']]['status'] = 'in_progress'
        completed = batches[-1]
        clock = FakeClock()
        clock.seconds = 3600
        retrieve = self.provider.retrieve
        polled = []

        def slow_retrieve(identity):
            polled.append(identity)
            clock.seconds += 90
            return retrieve(identity)

        def observation_time():
            return (datetime(2026, 10, 4, tzinfo=timezone.utc)
                    + timedelta(seconds=clock.seconds)).isoformat()

        with patch.object(self.provider, 'retrieve', side_effect=slow_retrieve), \
                patch('berean_translation.engine.now', side_effect=observation_time):
            first = self.run_window(clock, wait_seconds=600)
            self.assertEqual(first['ticks'], 1)
            self.assertEqual(len(polled), 7)
            self.assertEqual(clock.seconds, 3600 + 7 * 90)
            self.assertNotIn(completed['remote_id'], polled)
            untouched = self.state.read(f'state/batches/{completed["id"]}/batch.json')
            self.assertEqual(untouched, completed)
            self.assertEqual(validate_repository(self.config)['tasks'], 8)

            self.run_window(clock, wait_seconds=600)
            self.assertEqual(polled[7], completed['remote_id'])
            terminal = self.state.read(f'state/batches/{completed["id"]}/batch.json')
            self.assertEqual(terminal['status'], 'collected')
            terminal_path = self.state.path(f'state/batches/{completed["id"]}/batch.json')
            frozen = terminal_path.read_bytes()
            self.run_window(clock, wait_seconds=600)
            self.assertEqual(terminal_path.read_bytes(), frozen)

        self.assertEqual(self.provider.create_calls, 8)
        self.assertEqual(self.provider.upload_calls, 8)
        self.assertEqual(polled.count(completed['remote_id']), 1)
        validate_repository(self.config)

    def test_paused_prepared_prefix_yields_to_later_submitted_work_on_resume(self):
        def reject(line):
            if ':review' in line['custom_id']:
                return {'score': 80, 'passed': False, 'findings': []}
            return self.provider.default_result(line)

        queue(self.state, languages='afr,deu')
        drive(self.engine, self.provider, reject)
        self.config.runtime['max_batch_requests'] = 1
        policy = self.config.runtime['automatic_downstream_recovery']
        policy.update(enabled=True, total_budget_usd=10)
        shared = {'id': 'paused', 'operation': 'repair', 'model': policy['model'],
            'review_model': policy['review_model'], 'budget_usd': 10,
            'max_articles': 2, 'dry_run': False, 'requested_by': 'fixture-owner'}
        with patch('berean_translation.engine.now', return_value='2026-10-04T00:00:00Z'):
            paused = accept(self.engine, shared)
            with patch.object(self.engine, 'submit'):
                self.engine.prepare()
        self.assertEqual(len(paused['tasks']), 2)
        paused_batches = [batch for batch in self.state.batches() if batch['campaign'] == 'paused']
        self.assertEqual(len(paused_batches), 2)
        self.assertTrue(all(batch['status'] == 'prepared' for batch in paused_batches))
        policy['enabled'] = False

        repository = 'trueChristian/berean-translation'
        manual = {'id': 'gh-201', 'operation': 'repair', 'model': 'gpt-6-luna',
            'review_model': 'gpt-6-luna', 'budget_usd': 10, 'max_articles': 1,
            'dry_run': False, 'requested_by': 'fixture-owner', 'manual_authorization': {
                'kind': 'github_workflow_dispatch', 'repository': repository,
                'workflow_ref': repository + '/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id': '201', 'actor': 'fixture-owner'}}
        with patch('berean_translation.engine.now', return_value='2026-10-04T00:10:00Z'):
            accepted = accept(self.engine, manual)
            self.engine.prepare()
        self.assertEqual(len(accepted['tasks']), 1)
        submitted, = [batch for batch in self.state.batches() if batch['campaign'] == 'gh-201']
        self.assertEqual(submitted['status'], 'submitted')
        self.provider.complete_all(lambda line: None)
        calls = self.provider.create_calls
        terminal_files = {self.state.path(f'state/batches/{batch["id"]}/batch.json'):
                          self.state.path(f'state/batches/{batch["id"]}/batch.json').read_bytes()
                          for batch in self.state.batches() if batch['status'] == 'collected'}
        clock = FakeClock()
        clock.seconds = 3600
        submission_enabled = downstream.submission_enabled

        def slow_paused_check(config, campaign):
            if campaign['id'] == 'paused':
                clock.seconds += 310
            return submission_enabled(config, campaign)

        def observation_time():
            return (datetime(2026, 10, 4, tzinfo=timezone.utc)
                    + timedelta(seconds=clock.seconds)).isoformat()

        with patch('berean_translation.downstream.submission_enabled', side_effect=slow_paused_check), \
                patch('berean_translation.engine.now', side_effect=observation_time), \
                patch.object(self.provider, 'retrieve', wraps=self.provider.retrieve) as retrieve:
            self.run_window(clock, wait_seconds=600)
            retrieve.assert_not_called()
            self.assertEqual(clock.seconds, 3600 + 620)
            self.assertEqual(self.state.read(f'state/batches/{submitted["id"]}/batch.json'), submitted)
            validate_repository(self.config)

            self.run_window(clock, wait_seconds=600)
            retrieve.assert_called_once_with(submitted['remote_id'])
            self.assertEqual(self.state.read(f'state/batches/{submitted["id"]}/batch.json')['status'],
                             'collected')

        for path, content in terminal_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertTrue(all(self.state.read(f'state/batches/{batch["id"]}/batch.json')['status'] == 'prepared'
                            for batch in paused_batches))
        validate_repository(self.config)

    def test_yielded_continuation_preserves_complete_cycle_budget_and_history(self):
        def reject(line):
            if ':review' in line['custom_id']:
                return {'score': 80, 'passed': False, 'findings': [{
                    'severity': 'major', 'location': 'p', 'source_quote': 'Faith',
                    'translation_quote': 'Wrong', 'suggested_fix': 'Preserve faith'}]}
            return self.provider.default_result(line)

        queue(self.state)
        drive(self.engine, self.provider, reject)
        historical = {task['id']: copy.deepcopy(task) for task in self.state.tasks()}
        policy_before = copy.deepcopy(self.config.runtime['automatic_downstream_recovery'])
        repository = 'trueChristian/berean-translation'
        request = {'id': 'gh-200', 'operation': 'repair', 'model': 'gpt-6-luna',
            'review_model': 'gpt-6-luna', 'budget_usd': 10, 'max_articles': 2,
            'dry_run': False, 'requested_by': 'fixture-owner',
            'continuation_policy': continuation.policy(), 'manual_authorization': {
                'kind': 'github_workflow_dispatch', 'repository': repository,
                'workflow_ref': repository + '/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id': '200', 'actor': 'fixture-owner'}}
        self.state.write('state/queue/gh-200.json', request)
        campaign = accept(self.engine, request)
        self.assertEqual(len(campaign['tasks']), 2)
        plans = {identity: self.state.read(f'state/tasks/{identity}/task.json')['cycle_budget']
                 for identity in campaign['tasks']}
        calls = self.provider.create_calls
        clock = FakeClock()
        with self.expire_at_checkpoint(clock, 'reserve task identities and budget'):
            self.engine.prepare(continue_work=clock.continue_work)
        pending = [batch for batch in self.state.batches() if batch['campaign'] == campaign['id']]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'prepared')
        self.assertEqual(self.provider.create_calls, calls)
        validate_repository(self.config)

        for _ in range(3):
            self.engine.tick(discover_source=False, continue_work=lambda: True)
            self.provider.complete_all()
        self.assertEqual(self.provider.create_calls, calls + 2)
        for identity, plan in plans.items():
            task = self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual(task['cycle_budget'], plan)
            self.assertEqual(task['status'], 'complete')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        current = self.state.read('state/campaigns/gh-200.json')
        self.assertEqual(current['planned_cycle_ceiling_usd'], campaign['planned_cycle_ceiling_usd'])
        self.assertLessEqual(current['reserved_usd'], current['planned_cycle_ceiling_usd'])
        self.assertEqual(current['downstream_allocation_usd'], 10)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.assertEqual(funding_ledger(self.state)['shared_policy_usd'], 0)
        self.assertEqual(self.config.runtime['automatic_downstream_recovery'], policy_before)
        for identity, task in historical.items():
            self.assertEqual(self.state.read(f'state/tasks/{identity}/task.json'), task)
        self.assertEqual(validate_repository(self.config)['ready'], 2)


if __name__ == '__main__':
    unittest.main()
