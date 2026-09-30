from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from berean_translation.batch_telemetry import REMOTE_TIMESTAMPS, observe_batch
from berean_translation.common import canonical, loads
from berean_translation.state import provider_time
from support import A, drive, queue, setup


class BatchTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config,self.state,self.upstream,self.provider,self.git,self.engine = setup(self.root)

    def start(self):
        queue(self.state)
        self.engine.tick()
        return self.state.batches()[0]

    def replace_results(self, batch, rows):
        remote = self.provider.batches[batch['remote_id']]
        self.provider.files[remote['output_file_id']] = b'\n'.join(canonical(row) for row in rows)

    def results(self, batch):
        remote = self.provider.batches[batch['remote_id']]
        return [loads(line) for line in self.provider.files[remote['output_file_id']].splitlines()]

    def test_submit_retains_provider_facts_without_confusing_local_creation(self):
        original = self.provider.create
        def create(*args):
            remote = original(*args)
            remote.update(created_at=1700000000,in_progress_at=1700000010,
                          completed_at=None,request_counts={'total':2,'completed':0,'failed':0})
            return remote
        self.provider.create = create
        with patch('berean_translation.engine.now',return_value='2026-09-30T10:00:00+00:00'):
            batch = self.start()
        self.assertEqual(batch['created_at'],'2026-09-30T10:00:00+00:00')
        self.assertEqual(batch['remote_created_at'],1700000000)
        self.assertEqual(batch['remote_in_progress_at'],1700000010)
        self.assertIsNone(batch['remote_completed_at'])
        self.assertEqual(batch['remote_request_counts'],{'total':2,'completed':0,'failed':0})
        self.assertEqual(batch['remote_status'],'in_progress')
        self.assertEqual(batch['remote_observed_at'],batch['created_at'])
        self.assertNotIn('last_polled_at',batch)

    def test_reconciliation_retains_provider_facts_without_resubmission(self):
        self.provider.raise_create = 'after'
        batch = self.start()
        remote = self.provider.batches[next(iter(self.provider.batches))]
        remote.update(created_at=1700000000,completed_at=1700000300,status='completed',
                      request_counts={'total':2,'completed':2,'failed':0})
        self.engine.collect()
        batch = self.state.batches()[0]
        self.assertEqual(batch['remote_completed_at'],1700000300)
        self.assertEqual(batch['remote_status'],'completed')
        self.assertIn('last_reconciled_at',batch)
        self.assertIn('recovered_at',batch)
        self.assertEqual(self.provider.create_calls,1)
        self.assertNotIn('collected_at',batch)

    def test_collection_keeps_provider_completion_and_local_collection_separate(self):
        batch = self.start()
        self.provider.complete_all()
        remote = self.provider.batches[batch['remote_id']]
        remote.update(created_at=1700000000,in_progress_at=1700000001,
                      finalizing_at=1700000100,completed_at=1700000300,
                      request_counts={'total':2,'completed':2,'failed':0})
        with patch('berean_translation.engine.now',return_value='2026-09-30T12:00:00+00:00'):
            self.engine.collect()
        batch = self.state.batches()[0]
        self.assertEqual(batch['status'],'collected')
        self.assertEqual(batch['remote_completed_at'],1700000300)
        self.assertEqual(batch['collected_at'],'2026-09-30T12:00:00+00:00')
        self.assertEqual(batch['completed_at'],batch['collected_at'])
        self.assertEqual(batch['last_polled_at'],batch['collected_at'])
        self.assertEqual(batch['remote_observed_at'],batch['collected_at'])
        self.assertEqual(batch['remote_request_counts']['completed'],2)
        snapshot = copy.deepcopy(batch)
        self.engine.collect()
        self.assertEqual(self.state.batches()[0],snapshot)
        self.assertEqual(self.provider.create_calls,1)

    def test_all_lifecycle_fields_are_retained_without_overwriting_known_times_with_null(self):
        batch = {}
        remote = {'status':'cancelled',**{field:1700000000+n for n,field in enumerate(REMOTE_TIMESTAMPS)}}
        observe_batch(batch,remote,'2026-09-30T12:00:00+00:00')
        for field in REMOTE_TIMESTAMPS:
            self.assertEqual(batch['remote_'+field],remote[field])
        observe_batch(batch,{'status':'cancelled','cancelled_at':None},'2026-09-30T13:00:00+00:00')
        self.assertEqual(batch['remote_cancelled_at'],remote['cancelled_at'])

    def test_in_progress_poll_updates_counts_and_observation_without_marking_collection(self):
        batch = self.start()
        remote = self.provider.batches[batch['remote_id']]
        remote.update(request_counts={'total':2,'completed':1,'failed':0},in_progress_at=1700000000)
        with patch('berean_translation.engine.now',return_value='2026-09-30T12:00:00+00:00'):
            self.engine.collect()
        batch = self.state.batches()[0]
        self.assertEqual(batch['last_polled_at'],'2026-09-30T12:00:00+00:00')
        self.assertEqual(batch['remote_request_counts']['completed'],1)
        self.assertEqual(batch['status'],'submitted')
        self.assertNotIn('collected_at',batch)
        self.assertNotIn('completed_at',batch)

    def test_poll_failure_is_redacted_resumable_and_does_not_invent_remote_failure(self):
        batch = self.start()
        original = self.provider.retrieve
        self.provider.retrieve = Mock(side_effect=RuntimeError('Authorization: Bearer secret-value'))
        self.engine.collect()
        batch = self.state.batches()[0]
        self.assertEqual(batch['poll_error_type'],'RuntimeError')
        self.assertEqual(batch['status'],'submitted')
        self.assertEqual(batch['remote_status'],'in_progress')
        self.assertNotIn('secret-value',canonical(batch).decode())
        self.provider.retrieve = original
        self.engine.collect()
        self.assertNotIn('poll_error_type',self.state.batches()[0])
        self.assertEqual(self.provider.create_calls,1)

    def test_result_download_failure_is_redacted_and_later_collection_is_safe(self):
        self.start()
        self.provider.complete_all()
        original = self.provider.content
        self.provider.content = Mock(side_effect=RuntimeError('private request/secret-value'))
        self.engine.collect()
        batch = self.state.batches()[0]
        self.assertEqual(batch['collection_error_type'],'RuntimeError')
        self.assertEqual(batch['status'],'submitted')
        self.assertNotIn('secret-value',canonical(batch).decode())
        self.provider.content = original
        self.engine.collect()
        batch = self.state.batches()[0]
        self.assertNotIn('collection_error_type',batch)
        self.assertEqual(batch['status'],'collected')
        self.assertEqual(self.provider.create_calls,1)
        self.assertTrue(all(t['review_attempts']==0 for t in self.state.tasks()))

    def test_failed_provider_batch_retains_safe_diagnostics_and_lifecycle(self):
        batch = self.start()
        remote = self.provider.batches[batch['remote_id']]
        remote.update(status='failed',failed_at=1700000500,
                      request_counts={'total':2,'completed':0,'failed':2},
                      errors={'data':[{'code':'invalid_json_line','line':2,'message':'secret-value',
                                       'param':'private-content'}]})
        self.engine.collect()
        batch = self.state.batches()[0]
        self.assertEqual(batch['status'],'collected')
        self.assertEqual(batch['remote_status'],'failed')
        self.assertEqual(batch['remote_failed_at'],1700000500)
        self.assertNotIn('remote_completed_at',batch)
        self.assertEqual(batch['remote_errors'][0]['line'],2)
        for task in self.state.tasks():
            self.assertEqual(task['status'],'not_ready')
            self.assertIn('invalid_json_line',task['failure'])
            self.assertIn('persisted batch input',task['failure'])
        self.assertNotIn('secret-value',canonical(batch).decode())
        self.assertNotIn('private-content',canonical(batch).decode())

    def test_error_rows_are_actionable_redacted_and_do_not_discard_successful_rows(self):
        batch = self.start()
        self.provider.complete_all()
        rows = self.results(batch)
        failed_id = rows[0]['custom_id'].split(':')[0]
        rows[0]['error'] = {'code':'batch_expired','message':'secret-value'}
        rows[0]['response'] = None
        self.replace_results(batch,rows)
        self.engine.collect()
        failed = self.state.read(f'state/tasks/{failed_id}/task.json')
        self.assertEqual(failed['status'],'not_ready')
        self.assertEqual(failed['provider_failure']['code'],'batch_expired')
        self.assertIn('explicit new request',failed['failure'])
        self.assertNotIn('secret-value',canonical(failed).decode())
        self.assertEqual(sum(t['status']=='queued' for t in self.state.tasks()),1)
        self.assertEqual(self.provider.create_calls,1)

    def test_unrecognized_provider_codes_and_http_body_messages_never_leak(self):
        batch = self.start()
        self.provider.complete_all()
        rows = self.results(batch)
        rows[0]['error'] = {'code':'secret-value','message':'secret-value'}
        rows[1]['response'] = {'status_code':429,'body':{'error':{'code':'rate_limit_exceeded','message':'secret-value'}}}
        self.replace_results(batch,rows)
        self.engine.collect()
        tasks = self.state.tasks()
        self.assertEqual({t['provider_failure']['code'] for t in tasks},{'provider_error','rate_limit_exceeded'})
        self.assertNotIn('secret-value',canonical(tasks).decode())
        self.assertTrue(any('HTTP 429' in t['failure'] for t in tasks))

    def test_malformed_rows_fail_closed_without_raw_json_keys_in_diagnostics(self):
        batch = self.start()
        self.provider.complete_all()
        remote = self.provider.batches[batch['remote_id']]
        self.provider.files[remote['output_file_id']] = b'{"secret-value":1,"secret-value":2}'
        self.engine.collect()
        self.assertEqual(self.state.batches()[0]['status'],'results_invalid')
        self.assertNotIn('secret-value',canonical(self.state.batches()).decode())
        self.assertNotIn('secret-value',canonical(self.state.tasks()).decode())
        self.assertTrue(all(t['status']=='not_ready' for t in self.state.tasks()))

    def test_old_collected_records_stay_unchanged_and_report_provider_time_as_unknown(self):
        batch = self.start()
        batch.update(status='collected',completed_at='2026-09-29T12:00:00+00:00',remote_status='completed')
        batch.pop('remote_observed_at')
        self.state.save_batch(batch)
        before = self.state.path(f'state/batches/{batch["id"]}/batch.json').read_bytes()
        self.provider.retrieve = Mock(side_effect=AssertionError('Old collected batches must not be fetched'))
        self.engine.collect()
        self.state.derive(self.config)
        self.assertEqual(before,self.state.path(f'state/batches/{batch["id"]}/batch.json').read_bytes())
        report = self.state.path('STATUS.md').read_text()
        self.assertIn('not recorded | 2026-09-29T12:00:00+00:00',report)
        self.assertIn('legacy `completed_at` is local collection time',report)
        self.assertNotIn('remote_completed_at',self.state.batches()[0])

    def test_unrecognized_status_and_malformed_telemetry_do_not_leak_or_invent_facts(self):
        batch = {}
        observe_batch(batch,{'status':'secret-value','completed_at':True,'failed_at':'secret-value',
                            'request_counts':{'total':True,'completed':-1,'failed':'secret-value'},
                            'errors':{'data':[{'code':'secret-value','message':'secret-value'}]}},'local time')
        self.assertEqual(batch['remote_status'],'unknown')
        self.assertEqual(batch['remote_request_counts'],{})
        self.assertNotIn('remote_completed_at',batch)
        self.assertNotIn('remote_failed_at',batch)
        self.assertNotIn('secret-value',canonical(batch).decode())
        self.assertEqual(provider_time(10**100),'not recorded')


class HeartbeatStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config,self.state,self.upstream,self.provider,self.git,self.engine = setup(self.root)

    def test_terminal_work_updates_pending_count_within_the_same_day(self):
        with patch('berean_translation.engine.now',return_value='2026-09-30T10:00:00+00:00'):
            queue(self.state)
            self.engine.tick()
            self.assertEqual(self.state.read('state/heartbeat.json')['pending_tasks'],2)
            drive(self.engine,self.provider)
        heartbeat = self.state.read('state/heartbeat.json')
        self.assertEqual(heartbeat['utc_date'],'2026-09-30')
        self.assertEqual(heartbeat['pending_tasks'],0)
        self.assertEqual(self.state.campaigns()[0]['status'],'finished')
        self.assertIn('| translate | finished | 2 |',self.state.path('STATUS.md').read_text())

    def test_same_day_source_change_updates_heartbeat_but_idle_ticks_do_not_rewrite_it(self):
        with patch('berean_translation.engine.now',return_value='2026-09-30T10:00:00+00:00'):
            self.engine.tick()
            self.upstream.change_source()
            self.engine.tick()
            self.assertEqual(self.state.read('state/heartbeat.json')['source_revision'],'b'*40)
            before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            with patch.object(self.engine.state,'write',wraps=self.engine.state.write) as writer:
                self.engine.tick()
                self.assertFalse(any(call.args[0]=='state/heartbeat.json' for call in writer.call_args_list))
            after = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)

    def test_in_run_tick_reuses_discovered_source_without_network_rescan(self):
        self.engine.tick()
        original = self.state.read('state/source.json')
        self.upstream.change_source()
        with patch.object(self.engine.source_client,'discover',side_effect=AssertionError('Unexpected rescan')):
            self.engine.tick(discover_source=False)
        self.assertEqual(self.state.read('state/source.json'),original)


if __name__ == '__main__':
    unittest.main()
