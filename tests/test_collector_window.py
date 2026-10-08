from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
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
