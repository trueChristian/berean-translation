"""Offline continuation behavior: dispatch workflows, never provider requests."""
import contextlib
import copy
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from berean_translation import autonomous, manual_admission
from berean_translation.cli import main
from berean_translation.collector import collect_window
from berean_translation.config import Config
from berean_translation.common import ContractError, canonical, json_hash, loads
from berean_translation.queue import github_request
from berean_translation.scheduler import continue_collection, work_reason
from support import A, B, drive, queue, setup


NOW = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
REPOSITORY = 'fixture/repo'
ENVIRONMENT = {
    'GITHUB_REPOSITORY': REPOSITORY,
    'GITHUB_RUN_ID': '501',
    'GITHUB_REF': 'refs/heads/main',
    'GITHUB_WORKFLOW_REF': REPOSITORY + '/.github/workflows/ai-worker.yml@refs/heads/main',
    'GITHUB_EVENT_NAME': 'schedule',
    'OPERATION': 'collect',
    'GH_TOKEN': 'offline-fixture-token',
}


class FakeGitHub:
    """Named Actions records with an explicit, observable dispatch boundary."""
    def __init__(self):
        self.calls = []
        self.current = {'id': 501, 'head_branch': 'main',
                        'path': '.github/workflows/ai-worker.yml', 'status': 'in_progress'}
        self.current_reads = 0
        self.cancel_before_dispatch = False
        self.default_branch = 'main'
        self.states = {'ai-worker.yml': 'active', 'ai-discover.yml': 'active'}
        self.histories = {
            'ai-worker.yml': [{'id':501, 'status':'in_progress', 'created_at':NOW.isoformat()}],
            'ai-discover.yml': [{'id':400, 'status':'completed', 'conclusion':'success',
                                 'created_at':(NOW-timedelta(minutes=30)).isoformat()}],
        }
        self.jobs = {501: [{'name':'worker', 'status':'completed', 'conclusion':'success'},
                          {'name':'continue', 'status':'in_progress', 'conclusion':None}]}
        self.post_error = None

    def __call__(self, method, url, token, data=None):
        self.calls.append((method, url, copy.deepcopy(data)))
        if token != ENVIRONMENT['GH_TOKEN']:
            raise AssertionError('Unexpected authentication boundary')
        prefix = '/repos/' + REPOSITORY
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'api.github.com' or not parsed.path.startswith(prefix):
            raise AssertionError('Unexpected target: ' + url)
        path = parsed.path[len(prefix):]
        if method == 'POST':
            if self.post_error:
                raise self.post_error
            return {}  # The production transport decodes an empty HTTP 204 this way.
        if method != 'GET':
            raise AssertionError('Unexpected mutation: ' + method)
        if not path:
            return {'default_branch':self.default_branch}
        if path == '/actions/runs/501':
            self.current_reads += 1
            result = copy.deepcopy(self.current)
            if self.cancel_before_dispatch and self.current_reads > 1:
                result['status'] = 'completed'
                result['conclusion'] = 'cancelled'
            return result
        if path.startswith('/actions/runs/') and path.endswith('/jobs'):
            identity = int(path.split('/')[3])
            return {'jobs':copy.deepcopy(self.jobs.get(identity, [])), 'total_count':len(self.jobs.get(identity, []))}
        if path.startswith('/actions/workflows/'):
            workflow = path.split('/')[3]
            if path.endswith('/runs'):
                return {'workflow_runs':copy.deepcopy(self.histories[workflow]),
                        'total_count':len(self.histories[workflow])}
            return {'state':self.states[workflow], 'path':'.github/workflows/' + workflow}
        raise AssertionError('Unexpected API read: ' + url)

    @property
    def posts(self):
        return [(url, data) for method, url, data in self.calls if method == 'POST']


class SchedulerFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, self.source, self.provider, self.git, self.engine = setup(self.root)
        self.config.runtime['automatic_collection_continuation'] = True
        self.config.languages = {'afr':self.config.languages['afr']}
        self.engine.discover()

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes()
                for p in self.state.path('state').rglob('*') if p.is_file()}

    def automatic(self):
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True


class WorkReasonTests(SchedulerFixture):
    def test_idle_and_completed_history_do_not_request_another_cycle(self):
        self.assertIsNone(work_reason(self.engine))
        queue(self.state)
        drive(self.engine, self.provider)
        self.automatic()
        before = self.snapshot()
        self.assertIsNone(work_reason(self.engine))
        self.assertEqual(self.snapshot(), before)

    def test_fresh_work_is_actionable_without_creating_queue_or_mutating_history(self):
        self.automatic()
        before = self.snapshot()
        self.assertIsNotNone(work_reason(self.engine))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.provider.upload_calls,self.provider.create_calls), (0,0))

    def test_manual_request_and_queued_tasks_continue_without_an_automatic_budget(self):
        request = queue(self.state, budget_usd=0)
        self.assertIsNotNone(work_reason(self.engine))
        self.engine.accept_request(request)
        before = self.snapshot()
        self.assertIsNotNone(work_reason(self.engine))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.provider.create_calls, 0)

    def test_preview_and_rejected_queue_entries_do_not_keep_a_runner_alive(self):
        queue(self.state, 'preview', dry_run=True)
        request = queue(self.state, 'invalid')
        self.state.write('state/queue-errors/invalid.json', {'request':request['id'],'error':'invalid selection'})
        self.assertIsNone(work_reason(self.engine))

    def test_prepared_work_continues_without_creating_a_duplicate_batch(self):
        self.engine.accept_request(queue(self.state))
        with patch.object(self.engine, 'submit'):
            self.engine.prepare()
        before = self.snapshot()
        self.assertIsNotNone(work_reason(self.engine))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.provider.upload_calls,self.provider.create_calls), (0,0))

    def test_unknown_submission_alone_does_not_create_an_endless_continuation_chain(self):
        self.engine.accept_request(queue(self.state))
        self.provider.raise_create = 'before'
        self.engine.prepare()
        self.assertTrue(all(b['status'] == 'submission_unknown' for b in self.state.batches()))
        self.automatic()
        before = self.snapshot()
        self.assertIsNone(work_reason(self.engine))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.provider.create_calls, 1)

    def test_submitted_and_cancelling_batches_still_need_collection_when_automatic_work_is_paused(self):
        self.engine.accept_request(queue(self.state))
        self.engine.prepare()
        self.assertFalse(self.config.runtime['autonomous_translation']['enabled'])
        self.assertIsNotNone(work_reason(self.engine))
        self.engine.cancel_campaign(self.state.campaigns()[0]['id'])
        self.assertEqual(self.state.batches()[0]['status'], 'cancelling')
        self.assertIsNotNone(work_reason(self.engine))

    def test_cancelled_manual_campaign_and_paused_automatic_tasks_are_idle(self):
        self.engine.accept_request(queue(self.state))
        self.engine.cancel_campaign(self.state.campaigns()[0]['id'])
        self.assertIsNone(work_reason(self.engine))
        # Use a separate fixture so cancellation itself is never reset.
        with tempfile.TemporaryDirectory() as directory:
            config, state, _, _, _, engine = setup(Path(directory))
            config.languages = {'afr':config.languages['afr']}
            config.runtime['autonomous_translation']['enabled'] = True
            config.runtime['automatic_new_translation'] = True
            engine.discover(); autonomous.enqueue(engine); engine.accept_queue()
            config.runtime['autonomous_translation']['enabled'] = False
            config.runtime['automatic_new_translation'] = False
            self.assertIsNone(work_reason(engine))

    def test_human_controlled_paths_and_refusals_are_not_work_to_resubmit(self):
        campaign = self.engine.accept_request(queue(self.state))
        for task in self.state.tasks():
            target = self.state.path(f'content/{task["language"]}/articles/{task["article_id"]}.html')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('Unrecognized human-controlled working content')
        self.assertIsNone(work_reason(self.engine))
        for task in self.state.tasks():
            self.state.path(f'content/{task["language"]}/articles/{task["article_id"]}.html').unlink()
            task['failure_kind'] = 'provider_refusal'
            self.state.save_task(task)
            self.engine.finish(task, 'not_ready', 'Provider refusal')
        self.automatic()
        self.assertIsNone(work_reason(self.engine))
        self.assertEqual(self.state.read(f'state/campaigns/{campaign["id"]}.json')['tasks'], campaign['tasks'])

    def test_permanent_automatic_admission_errors_do_not_keep_the_chain_alive(self):
        self.automatic()
        for article in self.state.read('state/source.json')['articles'].values():
            spec, reason = autonomous.selection(self.engine, 'afr', article)
            self.assertIsNone(reason)
            identity = 'auto-' + json_hash(spec)
            self.state.write(f'state/queue-errors/{identity}.json', {'request':identity,'error':'Cannot admit this source'})
        before = self.snapshot()
        self.assertIsNone(work_reason(self.engine))
        self.assertEqual(self.snapshot(), before)

    def test_capacity_held_by_unknown_automatic_work_does_not_spin_on_remaining_sources(self):
        self.automatic()
        self.config.runtime['autonomous_translation'].update(page_size=1,max_active_tasks=1)
        autonomous.enqueue(self.engine); self.engine.accept_queue()
        self.provider.raise_create = 'before'; self.engine.prepare()
        self.assertEqual(len(self.state.tasks()), 1)
        self.assertIsNone(work_reason(self.engine))

    def test_unreachable_manual_admissions_do_not_keep_the_chain_alive(self):
        for outcome in ('missing_queue','queue_error'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                config, state, _, provider, _, engine = setup(Path(directory))
                engine.discover()
                request = queue(state,'held-manual')
                with patch.object(engine.state,'save_task'), patch.object(engine.state,'save_record'):
                    campaign = engine.accept_request(request)
                campaign['tasks'] = []
                state.save_campaign(campaign)
                ledger = manual_admission.initialize(engine,campaign,request)
                manual_admission.validate(state,campaign,ledger)
                if outcome == 'missing_queue':
                    state.path('state/queue/held-manual.json').unlink()
                else:
                    state.write('state/queue-errors/held-manual.json', {
                        'request':'held-manual','error':'Cannot resume this original request'})
                # Retained provenance remains valid, but accept_queue cannot
                # advance this admission without its original usable request.
                manual_admission.validate(state,campaign,ledger)
                self.assertIsNone(work_reason(engine))
                self.assertEqual(provider.create_calls,0)


class ContinuationDispatchTests(SchedulerFixture):
    def setUp(self):
        super().setUp()
        queue(self.state)
        self.state.write('state/last-collection.json', {
            'operation':'collect','completed_at':NOW.isoformat(),
            'collection':{'stop_reason':'wait_budget_exhausted'}})
        self.github = FakeGitHub()

    def run_continuation(self, **environment):
        return continue_collection(self.config, {**ENVIRONMENT, **environment}, self.github, now=NOW)

    def test_success_dispatches_one_collect_cycle_to_main_and_preserves_all_state(self):
        before = self.snapshot()
        result = self.run_continuation()
        self.assertTrue(result['dispatched'])
        self.assertEqual(self.github.posts, [(
            'https://api.github.com/repos/fixture/repo/actions/workflows/ai-worker.yml/dispatches',
            {'ref':'main','inputs':{'operation':'collect'}})])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.provider.upload_calls,self.provider.create_calls), (0,0))

    def test_operator_kill_switch_and_unavailable_provider_do_not_dispatch(self):
        self.config.runtime['automatic_collection_continuation'] = False
        self.assertFalse(self.run_continuation()['dispatched'])
        self.assertFalse(self.github.calls)
        self.config.runtime['automatic_collection_continuation'] = True
        self.state.write('state/last-collection.json', {
            'operation':'collect','completed_at':NOW.isoformat(),
            'collection':{'stop_reason':'provider_unavailable'}})
        self.assertFalse(self.run_continuation()['dispatched'])
        self.assertFalse(self.github.calls)

    def test_wrong_branch_workflow_operation_and_missing_token_never_contact_github(self):
        for override in ({'GITHUB_REF':'refs/heads/feature'}, {'OPERATION':'cancel'},
                         {'GITHUB_WORKFLOW_REF':'fixture/repo/.github/workflows/other.yml@refs/heads/main'},
                         {'GITHUB_RUN_ID':'invalid'}, {'GH_TOKEN':''}):
            with self.subTest(override=override), self.assertRaises(ContractError):
                self.run_continuation(**override)
            self.assertFalse(self.github.calls)

    def test_failed_or_cancelled_current_worker_and_changed_default_branch_do_not_dispatch(self):
        for change in ('failed', 'cancelled', 'wrong_default'):
            with self.subTest(change=change):
                self.github = FakeGitHub()
                if change == 'wrong_default':
                    self.github.default_branch = 'other'
                else:
                    self.github.jobs[501][0]['conclusion'] = change
                self.assertFalse(self.run_continuation()['dispatched'])
                self.assertFalse(self.github.posts)

    def test_workflow_disabled_and_cancellation_during_handoff_do_not_dispatch(self):
        self.github.states['ai-worker.yml'] = 'disabled_manually'
        self.assertFalse(self.run_continuation()['dispatched'])
        self.assertFalse(self.github.posts)
        self.github = FakeGitHub(); self.github.cancel_before_dispatch = True
        self.assertFalse(self.run_continuation()['dispatched'])
        self.assertFalse(self.github.posts)

    def test_pending_collector_and_discovery_are_not_displaced_by_another_dispatch(self):
        for workflow in ('ai-worker.yml','ai-discover.yml'):
            for status in ('queued','pending','requested','waiting'):
                with self.subTest(workflow=workflow,status=status):
                    self.github = FakeGitHub()
                    self.github.histories[workflow].insert(0, {'id':502, 'status':status,'created_at':NOW.isoformat()})
                    self.assertFalse(self.run_continuation()['dispatched'])
                    self.assertFalse(self.github.posts)

    def test_another_active_state_writer_prevents_duplicate_dispatch(self):
        self.github.histories['ai-worker.yml'].insert(0, {'id':502,'status':'in_progress','created_at':NOW.isoformat()})
        self.github.jobs[502] = [{'name':'worker','status':'in_progress','conclusion':None}]
        self.assertFalse(self.run_continuation()['dispatched'])
        self.assertFalse(self.github.posts)

    def test_another_run_finishing_only_its_handoff_does_not_block_progress(self):
        self.github.histories['ai-worker.yml'].insert(0, {'id':502,'status':'in_progress','created_at':NOW.isoformat()})
        self.github.jobs[502] = [{'name':'worker','status':'completed','conclusion':'success'},
                                 {'name':'continue','status':'queued','conclusion':None}]
        self.assertTrue(self.run_continuation()['dispatched'])
        self.assertEqual(len(self.github.posts),1)

    def test_discovery_refresh_is_automatic_after_one_hour_and_uses_existing_workflow(self):
        self.github.histories['ai-discover.yml'][0]['created_at'] = (NOW-timedelta(hours=2)).isoformat()
        result = self.run_continuation()
        self.assertTrue(result['dispatched'])
        self.assertEqual(result['workflow'],'ai-discover.yml')
        self.assertEqual(self.github.posts, [(
            'https://api.github.com/repos/fixture/repo/actions/workflows/ai-discover.yml/dispatches', {'ref':'main'})])

    def test_recent_failed_discovery_does_not_starve_known_collection_work(self):
        self.engine.accept_queue()
        self.engine.prepare()
        self.assertTrue(any(batch['status']=='submitted' for batch in self.state.batches()))
        self.github.histories['ai-discover.yml'][0]['conclusion'] = 'failure'
        self.state.write('state/discovery-status.json', {'status':'held','observed_at':NOW.isoformat()})
        self.assertEqual(self.run_continuation()['workflow'], 'ai-worker.yml')

    def test_recent_source_failure_stops_new_admission_only_until_an_hourly_discovery_attempt(self):
        self.state.write('state/discovery-status.json', {'status':'held','observed_at':NOW.isoformat()})
        self.github.histories['ai-discover.yml'][0]['conclusion'] = 'failure'
        result = self.run_continuation()
        self.assertFalse(result['dispatched'])
        self.assertEqual(result['reason'],'source_discovery_requires_attention')
        self.assertFalse(self.github.posts)
        self.github.histories['ai-discover.yml'][0]['created_at'] = (NOW-timedelta(hours=2)).isoformat()
        result = self.run_continuation()
        self.assertTrue(result['dispatched'])
        self.assertEqual(result['workflow'],'ai-discover.yml')

    def test_disabled_or_intentionally_paused_discovery_does_not_stop_collection(self):
        self.github.states['ai-discover.yml'] = 'disabled_manually'
        self.assertEqual(self.run_continuation()['workflow'],'ai-worker.yml')
        self.github = FakeGitHub()
        self.github.histories['ai-discover.yml'] = []
        self.config.runtime['automatic_discovery'] = False
        self.assertEqual(self.run_continuation()['workflow'],'ai-worker.yml')

    def test_ambiguous_dispatch_failure_is_visible_and_never_blindly_retried(self):
        self.github.post_error = HTTPError('https://api.github.com', 500, 'lost response', {}, None)
        with self.assertRaisesRegex(ContractError, 'rejected by GitHub'):
            self.run_continuation()
        self.assertEqual(len(self.github.posts),1)

    def test_cli_handoff_is_provider_free_even_if_an_api_key_exists_in_environment(self):
        env = {**ENVIRONMENT,'OPENAI_API_KEY':'offline-fixture-not-a-real-key'}
        with patch.dict(os.environ,env,clear=True), patch(
                'berean_translation.scheduler.continue_collection',return_value={'dispatched':False}) as dispatch, patch(
                'berean_translation.cli.OpenAIProvider',side_effect=AssertionError('Provider construction forbidden')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--root',str(self.root),'continue-collection']),0)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0].root,self.root)

    def test_transport_accepts_empty_http_204_without_json_parse_failure(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b''
        payload = {'ref':'main','inputs':{'operation':'collect'}}
        with patch('berean_translation.queue.urlopen',return_value=response) as request:
            self.assertEqual(github_request('POST','https://api.github.com/fixture/dispatches','fixture-token',payload),{})
        actual = request.call_args.args[0]
        self.assertEqual(actual.method,'POST')
        self.assertEqual(loads(actual.data),payload)
        self.assertEqual(request.call_args.kwargs['timeout'],60)


class AutomaticWindowChainTests(SchedulerFixture):
    def window(self):
        # One bounded worker visit per Actions run. Provider completion happens
        # between runs; no cron trigger or manually created campaign is used.
        result = collect_window(self.engine, wait_seconds=60, poll_seconds=60,
                                discover_source=False, monotonic=lambda: 0,
                                sleep=lambda _: self.fail('This window must yield before sleeping'))
        self.state.write('state/last-collection.json', {
            'operation':'collect','completed_at':NOW.isoformat(),'collection':result})
        return result

    def test_successor_windows_drain_multiple_pages_through_review_and_publication(self):
        self.automatic()
        self.config.runtime['autonomous_translation'].update(page_size=1,max_active_tasks=1)
        dispatches = 0
        for ordinal in range(8):
            window = self.window()
            github = FakeGitHub()
            before = self.snapshot()
            handoff = continue_collection(self.config, ENVIRONMENT, github, now=NOW)
            self.assertEqual(self.snapshot(),before)
            self.assertLessEqual(len(github.posts),1)
            if not handoff['dispatched']:
                break
            self.assertTrue(github.posts[0][0].endswith('/ai-worker.yml/dispatches'))
            self.assertEqual(github.posts[0][1],{'ref':'main','inputs':{'operation':'collect'}})
            dispatches += 1
            self.provider.complete_all()
        else:
            self.fail('The chain must stop when every fixture translation is published')
        self.assertGreater(dispatches,1)
        self.assertEqual(len(self.state.projection(self.config)['articles']),2)
        self.assertEqual(len(self.state.tasks()),2)
        self.assertTrue(all(task['status']=='complete' for task in self.state.tasks()))
        self.assertTrue(all((task['translation_attempts'],task['review_attempts'])==(1,1)
                            for task in self.state.tasks()))
        self.assertFalse(list(self.state.path('state/queue').glob('gh-*.json')))
        self.assertEqual(self.provider.create_calls,4)
        self.assertEqual(window['submitted_batches'],0)
        self.assertIsNone(work_reason(self.engine))
        self.assertEqual(handoff['reason'],'no_actionable_work')

    def test_disabling_continuation_stops_the_chain_without_rewriting_pending_paid_work(self):
        self.automatic()
        first = self.window()
        self.assertGreater(first['batches_submitted'],0)
        before = self.snapshot()
        calls = self.provider.create_calls
        self.config.runtime['automatic_collection_continuation'] = False
        github = FakeGitHub()
        handoff = continue_collection(self.config, ENVIRONMENT, github, now=NOW)
        self.assertFalse(handoff['dispatched'])
        self.assertEqual(handoff['reason'],'continuation_disabled')
        self.assertFalse(github.calls)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.provider.create_calls,calls)
        self.assertTrue(any(batch['status']=='submitted' for batch in self.state.batches()))

    def test_rejected_first_request_does_not_starve_later_articles_or_spin_the_chain(self):
        self.automatic()
        self.config.runtime['autonomous_translation'].update(page_size=1,max_active_tasks=1)
        blocked = autonomous.enqueue(self.engine)[0]
        request_path = self.state.path(f'state/queue/{blocked}.json')
        original_request = request_path.read_bytes()
        self.assertEqual(self.state.read(f'state/queue/{blocked}.json')['selection']['article_id'], A)
        error = {'request':blocked,'error':'Permanent fixture admission failure'}
        self.state.write(f'state/queue-errors/{blocked}.json',error)
        for ordinal in range(6):
            self.window()
            handoff = continue_collection(self.config, ENVIRONMENT, FakeGitHub(), now=NOW)
            if not handoff['dispatched']:
                break
            self.provider.complete_all()
        else:
            self.fail('Rejected requests must not keep dispatching unreachable work')
        self.assertEqual([article['id'] for article in self.state.projection(self.config)['articles']],[B])
        self.assertEqual(len(self.state.tasks()),1)
        self.assertEqual(self.state.tasks()[0]['status'],'complete')
        self.assertEqual(self.provider.create_calls,2)
        self.assertEqual(request_path.read_bytes(),original_request)
        self.assertEqual(self.state.read(f'state/queue-errors/{blocked}.json'),error)
        self.assertIsNone(work_reason(self.engine))


class ContinuationPolicyTests(SchedulerFixture):
    def test_continuation_accepts_explicit_enabled_and_disabled_boolean_settings(self):
        for value in (True,False):
            with self.subTest(value=value):
                self.state.write('config/runtime.json', {
                    **self.config.runtime,'automatic_collection_continuation':value})
                self.assertIs(Config(self.root).runtime['automatic_collection_continuation'],value)

    def test_invalid_kill_switch_values_are_rejected_instead_of_treated_as_truthy(self):
        for value in ('false','true',0,1,None,{}):
            with self.subTest(value=value):
                self.state.write('config/runtime.json', {
                    **self.config.runtime,'automatic_collection_continuation':value})
                with self.assertRaisesRegex(ContractError,'continuation must be boolean'):
                    Config(self.root)


if __name__ == '__main__':
    unittest.main()
