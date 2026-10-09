from __future__ import annotations
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from berean_translation import cli
from berean_translation.collector import collect_window
from berean_translation.common import ContractError
from berean_translation.validation import validate_repository
from support import setup, queue, REPO_ROOT


class FakeClock:
    def __init__(self, on_sleep=None):
        self.seconds = 0
        self.sleeps = []
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds
        if self.on_sleep:
            self.on_sleep()


class CollectorWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config,self.state,self.source,self.provider,self.git,self.engine = setup(Path(self.temp.name))

    def run_window(self, clock, **kwargs):
        return collect_window(self.engine,monotonic=clock.monotonic,sleep=clock.sleep,**kwargs)

    def test_fast_translation_and_review_are_collected_in_one_window(self):
        queue(self.state)
        clock = FakeClock(self.provider.complete_all)
        self.engine.discover = Mock(wraps=self.engine.discover)
        result = self.run_window(clock,wait_seconds=600)
        self.assertEqual(result['ticks'],3)
        self.assertEqual(result['stop_reason'],'no_submitted_batches')
        self.assertEqual(clock.sleeps,[60,60])
        self.engine.discover.assert_called_once()
        self.assertEqual(self.provider.create_calls,2)
        self.assertEqual(validate_repository(self.config)['ready'],2)
        self.assertEqual(self.state.campaigns()[0]['status'],'finished')

    def test_slow_batch_stops_at_budget_without_duplicate_submission(self):
        queue(self.state)
        clock = FakeClock()
        result = self.run_window(clock,wait_seconds=180)
        self.assertEqual(result['ticks'],3)
        self.assertEqual(result['stop_reason'],'wait_budget_exhausted')
        self.assertEqual(clock.sleeps,[60,60])
        self.assertEqual(self.provider.create_calls,1)
        self.assertEqual(len(self.state.batches()),1)
        self.assertEqual(self.state.batches()[0]['status'],'submitted')
        self.provider.complete_all()
        resumed = FakeClock(self.provider.complete_all)
        self.run_window(resumed,wait_seconds=600)
        self.assertEqual(self.provider.create_calls,2)
        self.assertEqual(validate_repository(self.config)['ready'],2)

    def test_default_remains_a_single_tick(self):
        queue(self.state)
        clock = FakeClock()
        result = self.run_window(clock)
        self.assertEqual(result['ticks'],1)
        self.assertEqual(clock.sleeps,[])
        self.assertEqual(self.provider.create_calls,1)

    def test_cli_retains_completed_summary_during_polling_then_records_new_window(self):
        previous = {'started_at': '2026-10-08T07:21:04Z', 'completed_at': '2026-10-08T07:30:54Z',
                    'operation': 'collect', 'newly_published': 22,
                    'collection': {'ticks': 6, 'stop_reason': 'wait_budget_exhausted', 'submitted_batches': 5}}
        queue(self.state)
        original_tick = self.engine.tick
        for wait_seconds in (180, 0):
            with self.subTest(wait_seconds=wait_seconds):
                self.state.write('state/last-collection.json', previous)

                def check_tick(**kwargs):
                    result = original_tick(**kwargs)
                    self.assertEqual(self.state.read('state/last-collection.json'), previous)
                    self.assertIn('newly published: 22', self.state.path('STATUS.md').read_text())
                    self.assertIn('Generated: 2026-10-09T09:34:31Z', self.state.path('STATUS.md').read_text())
                    return result

                clock = FakeClock()
                with patch.dict(os.environ, {}, clear=True), \
                        patch('berean_translation.cli.GitStore', return_value=self.git), \
                        patch('berean_translation.cli.Engine', return_value=self.engine), \
                        patch.object(self.engine, 'tick', side_effect=check_tick), \
                        patch('berean_translation.engine.now', return_value='2026-10-09T09:34:31Z'), \
                        patch('berean_translation.cli.now', return_value='2026-10-09T09:40:00Z'), \
                        patch('berean_translation.cli.collect_window', side_effect=lambda engine, **kwargs:
                            collect_window(engine, monotonic=clock.monotonic, sleep=clock.sleep, **kwargs)), \
                        contextlib.redirect_stdout(io.StringIO()):
                    status = cli.main(['--root', str(self.state.root), 'tick', '--wait-seconds', str(wait_seconds)])
                self.assertEqual(status, 0)
                completed = self.state.read('state/last-collection.json')
                self.assertEqual(completed['newly_published'], 0)
                self.assertEqual(completed['collection']['ticks'], 3 if wait_seconds else 1)
                self.assertEqual(completed['collection']['stop_reason'], 'wait_budget_exhausted')
                self.assertIn('newly published: 0', self.state.path('STATUS.md').read_text())
                self.assertIn('Generated: 2026-10-09T09:40:00Z', self.state.path('STATUS.md').read_text())

    def test_cli_discovery_keeps_last_completed_collection_summary(self):
        previous = {'completed_at': '2026-10-08T07:30:54Z', 'operation': 'collect',
                    'newly_published': 22, 'collection': {'stop_reason': 'wait_budget_exhausted',
                                                       'submitted_batches': 5}}
        self.state.write('state/last-collection.json', previous)
        with patch.dict(os.environ, {}, clear=True), \
                patch('berean_translation.cli.GitStore', return_value=self.git), \
                patch('berean_translation.cli.Engine', return_value=self.engine), \
                patch('berean_translation.engine.now', return_value='2026-10-09T09:34:31Z'), \
                contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(['--root', str(self.state.root), 'tick', '--discover-only'])
        self.assertEqual(status, 0)
        self.assertEqual(self.state.read('state/last-collection.json'), previous)
        self.assertEqual(self.state.read('state/discovery-status.json')['status'], 'complete')
        self.assertIn('newly published: 22', self.state.path('STATUS.md').read_text())
        self.assertIn('Generated: 2026-10-09T09:34:31Z', self.state.path('STATUS.md').read_text())
        self.assertEqual(self.provider.create_calls, 0)

    def test_collection_only_reuses_source_and_reports_real_stage_progress(self):
        queue(self.state)
        initial = self.run_window(FakeClock())
        self.assertEqual(initial['tasks_created'], 2)
        self.assertEqual(initial['batches_submitted'], 1)
        self.assertEqual(initial['translations_published'], 0)
        self.provider.complete_all()
        self.engine.discover = Mock(side_effect=AssertionError('Collection must not scan English'))
        result = self.run_window(FakeClock(self.provider.complete_all),
                                 wait_seconds=600, discover_source=False)
        self.engine.discover.assert_not_called()
        self.assertFalse(result['discover_source'])
        self.assertEqual(result['tasks_created'], 0)
        self.assertEqual(result['batches_collected'], 2)
        self.assertEqual(result['batches_submitted'], 1)
        self.assertEqual(result['translations_published'], 2)

    def test_idle_and_dry_run_exit_without_waiting_or_spending(self):
        for dry_run in (False,True):
            if dry_run:
                queue(self.state,dry_run=True)
            clock = FakeClock()
            result = self.run_window(clock,wait_seconds=600)
            self.assertEqual(result['ticks'],1)
            self.assertEqual(clock.sleeps,[])
            self.assertEqual(self.provider.create_calls,0)

    def test_no_provider_does_not_spin_on_queued_work(self):
        queue(self.state)
        self.engine.provider = None
        result = self.run_window(FakeClock(),wait_seconds=600)
        self.assertEqual(result['stop_reason'],'provider_unavailable')
        self.assertEqual(result['ticks'],1)
        self.assertEqual(self.provider.create_calls,0)

    def test_uncertain_create_is_not_retried_or_kept_alive(self):
        queue(self.state)
        self.provider.raise_create = 'before'
        clock = FakeClock()
        result = self.run_window(clock,wait_seconds=600)
        self.assertEqual(result['ticks'],1)
        self.assertEqual(clock.sleeps,[])
        self.assertEqual(self.provider.create_calls,1)
        self.assertEqual(self.state.batches()[0]['status'],'submission_unknown')

    def test_initial_tick_time_and_oversleep_count_toward_deadline(self):
        queue(self.state)
        clock = FakeClock()
        original = self.engine.tick
        def slow_tick(**kwargs):
            original(**kwargs)
            clock.seconds += 150
        self.engine.tick = slow_tick
        result = self.run_window(clock,wait_seconds=180)
        self.assertEqual(result['ticks'],1)
        self.assertEqual(clock.sleeps,[])
        self.engine.tick = original
        clock = FakeClock(lambda: setattr(clock,'seconds',600))
        result = self.run_window(clock,wait_seconds=180)
        self.assertEqual(result['ticks'],1)
        self.assertEqual(clock.sleeps,[60])

    def test_invalid_bounds_fail_before_any_work(self):
        self.engine.tick = Mock()
        for args in ({'wait_seconds':-1},{'wait_seconds':901},{'wait_seconds':True},
                     {'poll_seconds':0},{'poll_seconds':29},{'poll_seconds':301}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                self.run_window(FakeClock(),**args)
        self.engine.tick.assert_not_called()

    def test_bounded_correction_budget_and_attempt_limits_are_unchanged(self):
        queue(self.state)
        def reject_reviews(line):
            if ':review' in line['custom_id']:
                return {'score':70,'passed':False,'findings':[]}
            return self.provider.default_result(line)
        clock = FakeClock(lambda: self.provider.complete_all(reject_reviews))
        result = self.run_window(clock,wait_seconds=600)
        self.assertEqual(result['ticks'],5)
        self.assertEqual(self.provider.create_calls,4)
        self.assertTrue(all(t['status']=='not_ready' for t in self.state.tasks()))
        self.assertTrue(all(t['translation_attempts']==2 and t['review_attempts']==2
                            for t in self.state.tasks()))
        campaign = self.state.campaigns()[0]
        self.assertLessEqual(campaign['reserved_usd'],campaign['budget_usd'])

    def test_workflow_has_bounded_polling_and_same_trusted_single_writer(self):
        import yaml
        doc = yaml.load((REPO_ROOT/'.github/workflows/ai-worker.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(doc['on']['schedule'][0]['cron'],'7,22,37,52 * * * *')
        self.assertEqual(doc['concurrency']['group'],'berean-translation-state-writer')
        self.assertEqual(doc['concurrency']['cancel-in-progress'],'false')
        worker = doc['jobs']['worker']
        self.assertGreaterEqual(int(worker['timeout-minutes']),20)
        runs = '\n'.join(step.get('run','') for step in worker['steps'])
        self.assertIn('tick --no-discover --publish --wait-seconds 600 --poll-seconds 60',runs)
        self.assertNotIn('workflow_dispatch',runs)


if __name__ == '__main__':
    unittest.main()
