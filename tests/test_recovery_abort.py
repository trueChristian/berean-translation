"""Offline recovery-acceptance aborts at every materialization boundary.

The API is simulated and every mutation is inside temporary fixture repositories.
These checks exercise retained authorization and history, not paid model work.
"""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, digest, json_hash
from berean_translation.validation import validate_repository
from berean_translation.config import Config
from berean_translation.engine import Engine
from berean_translation.gitstore import GitStore
from berean_translation.state import State
from support import A, B, FakeProvider, drive, queue, setup


class RecoveryAbortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root)
        queue(self.state, 'original', languages='afr,deu')
        drive(self.engine, self.provider, self.fail_reviews, ticks=5)
        self.old = sorted(self.state.tasks(), key=lambda task: (task['language'], task['article_id']))
        self.selected = self.old[:3]
        self.unselected = self.old[3]
        self.request = {
            'id': 'interrupted', 'operation': 'review', 'recovery_of_campaign': 'original',
            'previous_task_ids': [task['id'] for task in self.selected],
            'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
            'budget_usd': 3, 'dry_run': False, 'retry_failed': False,
            'requested_by': 'fixture-owner',
        }
        self.children = [digest('interrupted:' + task['language'] + ':' + task['article_id'])[:32]
                         for task in self.selected]
        self.state.write('state/queue/interrupted.json', self.request)
        self.original_files = {path: contents for path, contents in self.files().items()
                               if path.startswith(('state/tasks/', 'state/sources/', 'state/batches/'))
                               or path in ('state/campaigns/original.json', 'state/queue/original.json')}
        self.original_records = {task['id']: copy.deepcopy(self.state.record(task['language'], task['article_id']))
                                 for task in self.old}
        self.original_calls = (self.provider.upload_calls, self.provider.create_calls)

    @staticmethod
    def fail_reviews(line):
        if ':review' in line['custom_id']:
            return {'score': 94, 'passed': False, 'findings': []}
        return FakeProvider.default_result(line)

    def files(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def restore_files(self, contents):
        for path in sorted(self.root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() and path.relative_to(self.root).as_posix() not in contents:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for relative, data in contents.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def campaign(self):
        return self.state.read('state/campaigns/interrupted.json')

    def task(self, identity):
        return self.state.read(f'state/tasks/{identity}/task.json')

    def interrupted_acceptance(self, boundary):
        # Raise at real acceptance write boundaries. The retained campaign is
        # exactly the checkpointed incomplete state; no synthetic paid state is
        # needed to demonstrate candidate-only or unlisted child-task files.
        methods = {
            'allocation_only': ('save_candidate', 1),
            'candidate_only': ('save_task', 1),
            'task_before_record': ('save_record', 1),
            'one_fully_staged': ('save_candidate', 2),
            'mixed': ('save_task', 2),
            'before_complete_flag': ('save_campaign', None),
        }
        method, stop = methods[boundary]
        original = getattr(self.engine.state, method)
        count = 0

        def interrupted(*args, **kwargs):
            nonlocal count
            if method == 'save_campaign':
                value = args[0]
                should_fail = value.get('id') == 'interrupted' and value.get('recovery_acceptance_complete') is True
            else:
                count += 1
                should_fail = count == stop
            if should_fail:
                raise OSError('offline simulated acceptance interruption: ' + boundary)
            return original(*args, **kwargs)

        with patch.object(self.engine.state, method, side_effect=interrupted):
            with self.assertRaises(OSError):
                self.engine.accept_request(self.request)
        campaign = self.campaign()
        self.assertFalse(campaign['recovery_acceptance_complete'])
        self.assertEqual(campaign['recovery_allocation_usd'], 3)
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), self.original_calls)
        return campaign

    def partition(self):
        materialized, candidate_only, unstaged = [], [], []
        for identity in self.children:
            if self.task(identity) is not None:
                materialized.append(identity)
            elif self.state.path(f'state/tasks/{identity}/candidate.json').exists():
                candidate_only.append(identity)
            else:
                unstaged.append(identity)
        return materialized, candidate_only, unstaged

    def assert_original_unchanged(self):
        for path, contents in self.original_files.items():
            self.assertEqual(self.state.path(path).read_bytes(), contents, path)

    def assert_aborted(self, before, expected):
        campaign = self.campaign()
        self.assertEqual(campaign['status'], 'acceptance_aborted')
        self.assertIs(campaign['recovery_acceptance_complete'], False)
        self.assertIs(campaign['cancel_requested'], True)
        abort = campaign['recovery_abort']
        self.assertIsInstance(abort['at'], str)
        self.assertTrue(abort['at'])
        names = ('materialized_task_ids', 'candidate_only_task_ids', 'unstaged_task_ids')
        flattened = []
        for name, identities in zip(names, expected):
            self.assertEqual(sorted(abort[name]), sorted(identities), name)
            flattened.extend(abort[name])
        self.assertEqual(sorted(flattened), sorted(self.children))
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(sorted(campaign['tasks']), sorted(expected[0]))
        for field in ('budget_usd', 'recovery_allocation_usd', 'recovery_budget',
                      'selection', 'previous_task_ids', 'recovery_request', 'request_sha256',
                      'model', 'review_model', 'prompts', 'prompt_version', 'models'):
            self.assertEqual(campaign[field], before[field], field)
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(campaign['reported_usage_usd'], 0)
        for item, old in zip(campaign['selection'], self.selected):
            self.assertEqual(item['record_before'], self.original_records[old['id']])
            self.assertEqual(item['record_before_sha256'], json_hash(item['record_before']))
        for identity in expected[0]:
            task = self.task(identity)
            self.assertEqual(task['status'], 'cancelled')
            self.assertEqual(task['stage'], 'review1')
            self.assertEqual(task['translation_attempts'], 0)
            self.assertEqual(task['review_attempts'], 0)
            self.assertEqual(task['events'], [])
            self.assertEqual(task['finished_at'], abort['at'])
            self.assertEqual(task['failure'], 'Incomplete recovery acceptance cancelled by owner')
            record = self.state.record(task['language'], task['article_id'])
            cancellations = [event for event in record['history']
                             if event.get('task') == identity and event.get('event') == 'cancelled']
            self.assertEqual(cancellations, [{'event': 'cancelled', 'task': identity,
                'at': abort['at'], 'reason': 'Incomplete recovery acceptance cancelled by owner'}])
            requested = [event for event in record['history']
                         if event.get('task') == identity and event.get('event') == 'requested']
            self.assertEqual(requested, [{'event': 'requested', 'task': identity,
                'campaign': 'interrupted', 'at': task['created_at']}])
        for identity in expected[1] + expected[2]:
            self.assertIsNone(self.task(identity))
        for identity in expected[1]:
            self.assertTrue(self.state.path(f'state/tasks/{identity}/candidate.json').exists())
        for identity in expected[2]:
            self.assertFalse(self.state.path(f'state/tasks/{identity}').exists())
        for old in self.old:
            record = self.state.record(old['language'], old['article_id'])
            previous_history = self.original_records[old['id']]['history']
            self.assertEqual(record['history'][:len(previous_history)], previous_history)
        self.assert_original_unchanged()
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), self.original_calls)
        validate_repository(self.config)
        return campaign

    def abort_boundary(self, boundary):
        before = self.interrupted_acceptance(boundary)
        expected = self.partition()
        candidates = {identity: self.state.path(f'state/tasks/{identity}/candidate.json').read_bytes()
                      for identity in expected[0] + expected[1]}
        self.engine.cancel_campaign('interrupted')
        campaign = self.assert_aborted(before, expected)
        for identity, contents in candidates.items():
            self.assertEqual(self.state.path(f'state/tasks/{identity}/candidate.json').read_bytes(), contents)
        after = self.files()
        self.engine.cancel_campaign('interrupted')
        self.assertEqual(self.files(), after, 'repeated explicit abort must not rewrite history')
        self.engine.tick()
        self.assertEqual(self.campaign(), campaign)
        self.assert_original_unchanged()
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), self.original_calls)
        validate_repository(self.config)

    def test_abort_allocation_only(self):
        self.abort_boundary('allocation_only')

    def test_abort_candidate_only_before_task(self):
        self.abort_boundary('candidate_only')

    def test_abort_task_before_record_history_or_campaign_list(self):
        self.abort_boundary('task_before_record')

    def test_abort_some_fully_staged_children(self):
        self.abort_boundary('one_fully_staged')

    def test_abort_mixed_materialized_candidate_only_and_unstaged_children(self):
        self.abort_boundary('mixed')

    def test_abort_all_children_before_complete_flag(self):
        self.abort_boundary('before_complete_flag')

    def assert_cancel_rejected_without_writes(self):
        before = self.files()
        checkpoints = list(self.git.checkpoints)
        calls = (self.provider.upload_calls, self.provider.create_calls)
        with self.assertRaises(ContractError):
            self.engine.cancel_campaign('interrupted')
        self.assertEqual(self.files(), before)
        self.assertEqual(self.git.checkpoints, checkpoints)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), calls)

    def test_abort_retains_all_requested_ids_and_envelope_and_allows_unrelated_work(self):
        self.interrupted_acceptance('mixed')
        self.engine.cancel_campaign('interrupted')
        for index, old in enumerate(self.selected):
            # Restore the pointer deliberately: accepted-history dedupe must be
            # authoritative even for children never staged in the first place.
            record = self.state.record(old['language'], old['article_id'])
            previous_record = copy.deepcopy(record)
            record['latest_task'] = old['id']
            self.state.save_record(record)
            request = {**self.request, 'id': f'duplicate-{index}',
                       'previous_task_ids': [old['id']], 'budget_usd': 0.1}
            before = self.files()
            with self.assertRaises(ContractError):
                self.engine.accept_request(request)
            self.assertEqual(self.files(), before)
            self.state.save_record(previous_record)
        over_budget = {**self.request, 'id': 'overspend-after-abort',
                       'previous_task_ids': [self.unselected['id']]}
        before = self.files()
        with self.assertRaises(ContractError):
            self.engine.accept_request(over_budget)
        self.assertEqual(self.files(), before)
        request = {**over_budget, 'id': 'later-authorized', 'budget_usd': 1}
        campaign = self.engine.accept_request(request)
        self.assertEqual(campaign['recovery_budget']['previously_allocated_usd'], 3)
        drive(self.engine, self.provider, ticks=3)
        self.assertEqual(self.task(campaign['tasks'][0])['status'], 'complete')
        self.assertEqual(self.campaign()['status'], 'acceptance_aborted')
        self.assertEqual(self.campaign()['recovery_allocation_usd'], 3)
        self.assert_original_unchanged()
        validate_repository(self.config)

    def test_aborted_record_history_allows_later_explicit_ordinary_review(self):
        self.interrupted_acceptance('before_complete_flag')
        self.engine.cancel_campaign('interrupted')
        abort_before = self.campaign()
        calls = self.provider.create_calls
        queue(self.state, 'later-ordinary-review', operation='review', languages='afr', issues='all')
        drive(self.engine, self.provider, ticks=3)
        self.assertEqual(self.provider.create_calls, calls + 1)
        later = self.state.read('state/campaigns/later-ordinary-review.json')
        self.assertEqual(len(later['tasks']), 2)
        self.assertTrue(all(self.task(identity)['status'] == 'complete' for identity in later['tasks']))
        self.assertEqual(self.campaign(), abort_before)
        self.assertTrue(all(self.task(identity)['status'] == 'cancelled' for identity in self.children))
        for article in (A, B):
            self.assertIsNotNone(self.state.record('afr', article)['published'])
        self.assert_original_unchanged()
        validate_repository(self.config)

    def test_terminal_abort_journal_and_cancellation_history_are_audited(self):
        self.interrupted_acceptance('mixed')
        self.engine.cancel_campaign('interrupted')
        baseline = self.files()
        original_campaign = self.campaign()
        cases = (
            ('materialized_task_ids', []),
            ('materialized_task_ids', [self.children[0], self.children[0]]),
            ('candidate_only_task_ids', ['f' * 32]),
            ('unstaged_task_ids', []),
            ('at', 'changed-cancellation-time'),
        )
        for field, value in cases:
            self.restore_files(baseline)
            campaign = copy.deepcopy(original_campaign)
            campaign['recovery_abort'][field] = value
            self.state.save_campaign(campaign)
            with self.subTest(journal_field=field), self.assertRaises(ContractError):
                validate_repository(self.config)
        self.restore_files(baseline)
        task = self.task(self.children[0])
        record = self.state.record(task['language'], task['article_id'])
        cancellation = next(event for event in record['history']
                            if event.get('task') == task['id'] and event.get('event') == 'cancelled')
        record['history'].append(copy.deepcopy(cancellation))
        self.state.save_record(record)
        with self.assertRaises(ContractError):
            validate_repository(self.config)

    def test_abort_rejects_unexpected_child_files_before_any_write(self):
        self.interrupted_acceptance('mixed')
        baseline = self.files()
        for suffix in ('unexpected.txt', 'results/review1.json', 'results/translate.json', 'candidate.backup.json'):
            self.restore_files(baseline)
            path = self.state.path(f'state/tasks/{self.children[0]}/{suffix}')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}', encoding='utf-8')
            with self.subTest(suffix=suffix):
                self.assert_cancel_rejected_without_writes()

    def test_abort_rejects_changed_candidate_or_foreign_child_identity(self):
        self.interrupted_acceptance('mixed')
        baseline = self.files()
        for field, value in (('id', 'f' * 32), ('campaign', 'foreign'),
                             ('article_id', self.unselected['article_id']),
                             ('recovery_of_task', self.unselected['id'])):
            self.restore_files(baseline)
            task = self.task(self.children[0])
            task[field] = value
            self.state.write(f'state/tasks/{self.children[0]}/task.json', task)
            with self.subTest(field=field):
                self.assert_cancel_rejected_without_writes()
        for identity in self.children[:2]:
            self.restore_files(baseline)
            candidate = self.state.read(f'state/tasks/{identity}/candidate.json')
            candidate['title'] = 'Changed after acceptance'
            self.state.write(f'state/tasks/{identity}/candidate.json', candidate)
            with self.subTest(candidate=identity):
                self.assert_cancel_rejected_without_writes()

    def test_abort_rejects_unlisted_foreign_task_claiming_the_campaign(self):
        self.interrupted_acceptance('task_before_record')
        task = self.task(self.children[0])
        task['id'] = 'f' * 32
        self.state.save_task(task)
        self.state.save_candidate(task, self.state.candidate(self.selected[0]))
        self.assert_cancel_rejected_without_writes()

    def test_failed_final_push_requires_fresh_checkout_then_durably_finishes_abort(self):
        remote_temp = tempfile.TemporaryDirectory()
        self.addCleanup(remote_temp.cleanup)
        remote = Path(remote_temp.name) / 'origin.git'
        fresh = Path(remote_temp.name) / 'fresh'
        def git(root, *args):
            return subprocess.run(['git', '-C', str(root), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Offline fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.test')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-m', 'offline pre-recovery fixture')
        git(Path(remote_temp.name), 'init', '--bare', str(remote))
        git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(self.root, 'remote', 'add', 'origin', str(remote))
        git(self.root, 'push', '-u', 'origin', 'main')
        store = GitStore(self.root, publish=True)
        self.engine.gitstore = store
        self.interrupted_acceptance('before_complete_flag')
        original_git = store.git
        pushes = 0
        def fail_terminal_push(*args, **kwargs):
            nonlocal pushes
            if args and args[0] == 'push':
                pushes += 1
                if pushes == 2:
                    return subprocess.CompletedProcess(args, 1, stdout='', stderr='offline simulated rejection')
            return original_git(*args, **kwargs)
        with patch.object(store, 'git', side_effect=fail_terminal_push):
            with self.assertRaisesRegex(ContractError, 'push rejected'):
                self.engine.cancel_campaign('interrupted')
        self.assertEqual(self.campaign()['status'], 'acceptance_aborted')
        self.assertEqual(json.loads(git(remote, 'show', 'main:state/campaigns/interrupted.json'))['status'],
                         'acceptance_aborting')
        local_before = {path: data for path, data in self.files().items() if not path.startswith('.git/')}
        with self.assertRaisesRegex(ContractError, 'fresh main checkout'):
            self.engine.cancel_campaign('interrupted')
        self.assertEqual({path: data for path, data in self.files().items() if not path.startswith('.git/')}, local_before)
        git(Path(remote_temp.name), 'clone', str(remote), str(fresh))
        fresh_config = Config(fresh)
        fresh_engine = Engine(fresh_config, self.upstream.client, self.provider, GitStore(fresh, publish=True))
        fresh_engine.cancel_campaign('interrupted')
        validate_repository(fresh_config)
        durable = json.loads(git(remote, 'show', 'main:state/campaigns/interrupted.json'))
        self.assertEqual(durable['status'], 'acceptance_aborted')
        self.assertEqual(durable['recovery_allocation_usd'], 3)
        self.assertEqual(State(fresh).read('state/campaigns/interrupted.json'), durable)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), self.original_calls)
        for path, contents in self.original_files.items():
            self.assertEqual((fresh / path).read_bytes(), contents, path)

    def test_abort_dispatch_cannot_bypass_audit_with_changed_mutable_flags(self):
        self.interrupted_acceptance('task_before_record')
        baseline = self.files()
        for field, value in (('dry_run', True), ('recovery_of_campaign', ''),
                             ('recovery_acceptance_complete', True), ('operation', 'translate')):
            self.restore_files(baseline)
            campaign = self.campaign()
            campaign[field] = value
            self.state.save_campaign(campaign)
            with self.subTest(field=field):
                self.assert_cancel_rejected_without_writes()

    def test_abort_rejects_reserved_paid_batch_attempt_or_model_event_history(self):
        self.interrupted_acceptance('task_before_record')
        baseline = self.files()
        for field, value in (('reserved_usd', 0.001), ('reported_usage_usd', 0.001)):
            self.restore_files(baseline)
            campaign = self.campaign()
            campaign[field] = value
            self.state.save_campaign(campaign)
            with self.subTest(campaign_field=field):
                self.assert_cancel_rejected_without_writes()
        for field, value in (('translation_attempts', 1), ('review_attempts', 1),
                             ('events', [{'stage': 'review1'}]), ('batch', 'paid-batch'),
                             ('stage', 'correct'), ('status', 'in_batch')):
            self.restore_files(baseline)
            task = self.task(self.children[0])
            task[field] = value
            self.state.save_task(task)
            with self.subTest(task_field=field):
                self.assert_cancel_rejected_without_writes()
        for status in ('prepared', 'submission_unknown', 'submitted', 'collected'):
            self.restore_files(baseline)
            self.state.save_batch({'id': 'suspicious', 'campaign': 'interrupted', 'status': status,
                                   'tasks': [self.children[0]], 'reserved_usd': 0})
            with self.subTest(batch_status=status):
                self.assert_cancel_rejected_without_writes()

    def test_abort_rejects_foreign_record_pointer_or_history(self):
        self.interrupted_acceptance('one_fully_staged')
        baseline = self.files()
        old = self.selected[0]
        variants = (
            {'latest_task': 'f' * 32},
            {'history_append': {'event': 'requested', 'task': 'f' * 32, 'campaign': 'foreign', 'at': 'fixture-time'}},
            {'history_append': {'event': 'complete', 'task': self.children[0], 'at': 'fixture-time'}},
            {'history_append': {'event': 'requested', 'task': self.children[0], 'campaign': 'foreign', 'at': 'fixture-time'}},
        )
        for variant in variants:
            self.restore_files(baseline)
            record = self.state.record(old['language'], old['article_id'])
            if 'history_append' in variant:
                record['history'].append(variant['history_append'])
            else:
                record.update(variant)
            self.state.save_record(record)
            with self.subTest(variant=variant):
                self.assert_cancel_rejected_without_writes()

    def test_abort_checkpoints_intent_before_any_task_cancellation(self):
        before = self.interrupted_acceptance('before_complete_flag')
        expected = self.partition()
        checkpoints = []
        original_save = self.engine.state.save_task

        def checkpoint(message):
            campaign = self.campaign()
            if campaign.get('status') == 'acceptance_aborting':
                self.assertTrue(campaign['cancel_requested'])
                self.assertFalse(campaign['recovery_acceptance_complete'])
                self.assertEqual(campaign['recovery_allocation_usd'], 3)
                self.assertEqual(sorted(campaign['recovery_abort']['materialized_task_ids']), sorted(self.children))
                self.assertTrue(all(self.task(identity)['status'] == 'queued' for identity in self.children))
                checkpoints.append(copy.deepcopy(campaign['recovery_abort']))

        def save_task(task):
            if task.get('campaign') == 'interrupted' and task.get('status') == 'cancelled':
                self.assertEqual(len(checkpoints), 1, 'cancellation requires durable abort intent')
            return original_save(task)

        with patch.object(self.git, 'checkpoint', side_effect=checkpoint):
            with patch.object(self.engine.state, 'save_task', side_effect=save_task):
                self.engine.cancel_campaign('interrupted')
        self.assertEqual(len(checkpoints), 1)
        self.assert_aborted(before, expected)
        self.assertEqual(self.campaign()['recovery_abort'], checkpoints[0])

    def test_abort_checkpoint_failure_prevents_task_or_record_cancellation(self):
        self.interrupted_acceptance('before_complete_flag')
        before = self.files()
        with patch.object(self.git, 'checkpoint', side_effect=OSError('simulated durable checkpoint failure')):
            with self.assertRaises(OSError):
                self.engine.cancel_campaign('interrupted')
        for path, contents in before.items():
            if path.startswith(('state/tasks/', 'state/records/')):
                self.assertEqual(self.state.path(path).read_bytes(), contents, path)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), self.original_calls)
        # The local persisted intent may be retried safely after durability returns.
        self.engine.cancel_campaign('interrupted')
        self.assertEqual(self.campaign()['status'], 'acceptance_aborted')
        validate_repository(self.config)

    def test_interrupted_abort_resumes_without_duplicate_task_or_record_cancellations(self):
        original_campaign = self.interrupted_acceptance('before_complete_flag')
        expected = self.partition()
        baseline = self.files()
        cases = (('save_task', 1), ('save_record', 1), ('save_task', 2), ('save_record', 2),
                 ('save_campaign', None))
        for method, stop in cases:
            self.restore_files(baseline)
            original = getattr(self.engine.state, method)
            count = 0

            def interrupted(*args, **kwargs):
                nonlocal count
                value = args[0]
                if method == 'save_campaign':
                    should_fail = value.get('id') == 'interrupted' and value.get('status') == 'acceptance_aborted'
                else:
                    count += 1
                    should_fail = count == stop
                if should_fail:
                    raise OSError('offline simulated interruption during abort')
                return original(*args, **kwargs)

            with self.subTest(method=method, stop=stop):
                with patch.object(self.engine.state, method, side_effect=interrupted):
                    with self.assertRaises(OSError):
                        self.engine.cancel_campaign('interrupted')
                journal = copy.deepcopy(self.campaign()['recovery_abort'])
                self.engine.cancel_campaign('interrupted')
                self.assert_aborted(original_campaign, expected)
                self.assertEqual(self.campaign()['recovery_abort'], journal)
                after = self.files()
                self.engine.cancel_campaign('interrupted')
                self.assertEqual(self.files(), after)


if __name__ == '__main__':
    unittest.main()
