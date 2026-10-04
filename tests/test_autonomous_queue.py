"""Offline end-to-end simulations of the bounded, self-draining archive queue."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from berean_translation import autonomous
from berean_translation.common import ContractError, canonical, json_hash
from berean_translation.validation import validate_repository
from support import A, B, drive, queue, setup


class AutonomousQueueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.config.runtime['autonomous_translation'].update(enabled=True, page_size=3, max_active_tasks=6)
        self.config.runtime['automatic_new_translation'] = True
        self.engine.discover()

    def limited_languages(self):
        self.config.languages = {'afr': self.config.languages['afr']}

    def tasks(self):
        return self.state.tasks()

    def validate(self):
        self.state.derive(self.config)
        validate_repository(self.config)

    def test_all_twenty_languages_drain_multiple_pages_without_manual_next(self):
        drive(self.engine, self.provider, ticks=32)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 40)
        self.assertEqual(len(self.tasks()), 40)
        self.assertEqual({t['language'] for t in self.tasks()}, set(self.config.languages))
        self.assertTrue(all(t['status'] == 'complete' for t in self.tasks()))
        self.assertLessEqual(autonomous.ledger(self.state)['allocated_usd'], 30)
        before = self.provider.create_calls
        drive(self.engine, self.provider, ticks=2)
        self.assertEqual(self.provider.create_calls, before)
        self.validate()

    def test_preserved_overlong_negative_review_does_not_trigger_paid_retry(self):
        previous, _ = self.make_saved(stage='review1', kind='invalid_result')
        findings = [{'severity':'major', 'location':'p', 'source_quote':'Faith',
                     'translation_quote':str(i), 'suggested_fix':'Restore meaning'} for i in range(49)]
        archived = {'result': {'score':32, 'passed':False, 'findings':findings}}
        path = f'state/tasks/{previous["id"]}/results/review1.json'
        self.state.write(path, archived)
        frozen = canonical(self.state.read(f'state/tasks/{previous["id"]}/task.json'))
        self.assertEqual(autonomous.resume_stage(self.state, previous),
                         (None, 'preserved_review_overflow_requires_attention'))
        self.upstream.articles = [a for a in self.upstream.articles if a['id'] == previous['article_id']]
        self.upstream.rebuild()
        before = self.provider.create_calls
        drive(self.engine, self.provider, ticks=3)
        self.assertEqual(self.provider.create_calls, before)
        self.assertEqual(self.state.read(path), archived)
        self.assertEqual(canonical(self.state.read(f'state/tasks/{previous["id"]}/task.json')), frozen)

    def test_released_budget_holds_recheck_active_capacity_at_admission(self):
        self.limited_languages()
        self.config.runtime['autonomous_translation'].update(page_size=1, max_active_tasks=1,
                                                             max_envelope_usd=0.000001)
        self.engine.tick(); self.engine.tick()
        self.assertEqual(len(list((self.root/'state/automatic-holds').glob('*.json'))), 2)
        self.assertEqual(self.tasks(), [])
        self.config.runtime['autonomous_translation']['max_envelope_usd'] = 10
        self.engine.tick()
        self.assertEqual(len(self.tasks()), 1)
        self.engine.tick()
        self.assertEqual(len(self.tasks()), 1)
        holds = [self.state.read(p.relative_to(self.root).as_posix()) for p in (self.root/'state/automatic-holds').glob('*.json')]
        self.assertTrue(any(h['reason'] == 'automatic_capacity_wait' for h in holds))
        drive(self.engine, self.provider, ticks=8)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.assertEqual(len(self.tasks()), 2)
        self.validate()

    def test_frontier_matches_current_publication_and_source_removal_selection(self):
        self.config.runtime['autonomous_translation']['enabled'] = False
        self.config.runtime['automatic_new_translation'] = False
        self.limited_languages()
        queue(self.state, 'published')
        drive(self.engine, self.provider)
        published = copy.deepcopy(self.state.record('afr', A)['published'])
        queue(self.state, 'rereview', operation='review', issues='all')
        def reject(line):
            if ':review' in line['custom_id']:
                return {'score':80, 'passed':False, 'findings':[]}
            return self.provider.default_result(line)
        drive(self.engine, self.provider, reject, ticks=8)
        self.config.runtime['autonomous_translation']['enabled'] = True
        rows = autonomous.frontier(self.config, self.state, self.tasks())['items']
        self.assertEqual({r['reason'] for r in rows}, {'already_translated'})
        before = self.provider.create_calls
        drive(self.engine, self.provider, ticks=2)
        self.assertEqual(self.provider.create_calls, before)
        self.assertEqual(self.state.record('afr', A)['published'], published)
        self.upstream.articles = [a for a in self.upstream.articles if a['id'] != A]
        self.upstream.rebuild(); self.engine.discover()
        rows = autonomous.frontier(self.config, self.state, self.tasks())['items']
        self.assertEqual(next(r['reason'] for r in rows if r['article_id'] == A),
                         'source_removed_requires_attention')

    def test_full_first_line_chain_reserved_before_first_create(self):
        self.limited_languages()
        original_create = self.provider.create
        def create(*args):
            campaign = self.state.read(f'state/campaigns/{args[2]}.json')
            self.assertEqual(set(campaign['stage_budget']['stages_usd']), {'translate', 'review1', 'correct', 'review2'})
            self.assertEqual(campaign['budget_usd'], campaign['stage_budget']['total_reserved_usd'])
            self.assertTrue(any('allocate full automatic' in m for m in self.git.checkpoints))
            self.assertTrue(any('submission intent' in m for m in self.git.checkpoints))
            return original_create(*args)
        self.provider.create = create
        def responder(line):
            if line['custom_id'].endswith(':review1'):
                return {'score': 80, 'passed': False, 'findings': [{'severity': 'major', 'location': 'p',
                    'source_quote': 'Faith', 'translation_quote': 'Faith', 'suggested_fix': 'Preserve source meaning'}]}
            return self.provider.default_result(line)
        drive(self.engine, self.provider, responder, ticks=6)
        self.assertTrue(all(t['status'] == 'complete' for t in self.tasks()))
        self.assertTrue(all((t['translation_attempts'], t['review_attempts']) == (2, 2) for t in self.tasks()))
        self.validate()

    def make_saved(self, stage='review2', kind=None):
        self.config.runtime['autonomous_translation']['enabled'] = False
        self.config.runtime['automatic_new_translation'] = False
        request = queue(self.state, 'old-manual', budget_usd=5)
        campaign = self.engine.accept_request(request)
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        source = self.state.source(task)
        self.state.save_candidate(task, {k: source['article'].get(k) for k in ('title', 'subtitle', 'section')} | {'html': source['html']})
        task.update(stage=stage, translation_model_actual='gpt-4.1-mini', failure_kind=kind)
        self.state.save_task(task)
        self.engine.finish(task, 'not_ready' if kind else 'budget_blocked', 'Saved stage for offline test')
        second = self.state.read(f'state/tasks/{campaign["tasks"][1]}/task.json')
        self.engine.finish(second, 'cancelled')
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        self.limited_languages()
        return self.state.read(f'state/tasks/{task["id"]}/task.json'), campaign

    def test_saved_final_review_allocates_exact_review_and_never_retranslates(self):
        previous, original = self.make_saved()
        frozen = canonical(previous)
        self.engine.tick()
        current = next(t for t in self.tasks() if t.get('automatic_previous_task') == previous['id'])
        self.assertEqual(current['stage'], 'review2')
        campaign = self.state.read(f'state/campaigns/{current["campaign"]}.json')
        self.assertEqual(set(campaign['stage_budget']['stages_usd']), {'review2'})
        self.assertLess(campaign['automatic_allocation_usd'], 1)
        self.assertEqual(canonical(self.state.read(f'state/tasks/{previous["id"]}/task.json')), frozen)
        self.provider.complete_all(); self.engine.tick()
        self.assertEqual(self.state.record('afr', A)['published']['task'], current['id'])
        self.assertEqual(self.state.read(f'state/campaigns/{original["id"]}.json')['budget_usd'], 5)
        self.validate()

    def test_saved_correction_uses_candidate_and_complete_cycle_above_old_one_dollar(self):
        self.upstream.contents[f'content/articles/{A}.html'] = self.upstream.contents[f'content/articles/{A}.html'].replace('Faith and', 'Faith ' * 7000 + 'and')
        self.upstream.rebuild(); self.engine.discover()
        previous, _ = self.make_saved(stage='correct')
        self.engine.tick()
        current = next(t for t in self.tasks() if t.get('automatic_previous_task') == previous['id'])
        campaign = self.state.read(f'state/campaigns/{current["campaign"]}.json')
        self.assertEqual(set(campaign['stage_budget']['stages_usd']), {'correct', 'review2'})
        self.assertGreater(campaign['automatic_allocation_usd'], 1)
        self.assertLess(campaign['automatic_allocation_usd'], 2)
        drive(self.engine, self.provider, ticks=4)
        self.assertEqual(self.state.record('afr', A)['published']['task'], current['id'])
        self.validate()

    def test_refusals_unknown_outcomes_and_human_pairs_never_enqueue(self):
        previous, _ = self.make_saved(kind='provider_refusal')
        self.engine.tick()
        self.assertFalse([t for t in self.tasks() if t.get('autonomous')])
        self.assertEqual(self.provider.create_calls, 0)
        with patch.object(self.engine, 'human_protected', return_value=True):
            self.upstream.change_source(); self.engine.tick()
            self.assertEqual(self.provider.create_calls, 0)

    def test_shared_budget_pause_and_resume_keeps_pending_identity(self):
        self.limited_languages()
        autonomous.initialize(self.engine)
        self.config.runtime['autonomous_translation']['total_budget_usd'] = 0.01
        # Keep max envelope within the temporarily reduced live cap.
        self.config.runtime['autonomous_translation']['max_envelope_usd'] = 0.01
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, 0)
        requests = {p.name: p.read_bytes() for p in (self.root/'state/queue').glob('auto-*.json')}
        self.assertTrue(requests)
        self.assertFalse(list((self.root/'state/queue-errors').glob('*.json')))
        self.config.runtime['autonomous_translation'].update(total_budget_usd=30, max_envelope_usd=10)
        drive(self.engine, self.provider, ticks=4)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'state/queue').glob('auto-*.json')}, requests)
        self.validate()

    def test_policy_pause_blocks_next_stage_but_collects_submitted_work(self):
        self.limited_languages(); self.engine.tick()
        calls = self.provider.create_calls
        self.config.runtime['autonomous_translation']['enabled'] = False
        self.config.runtime['automatic_new_translation'] = False
        self.config.runtime['automatic_downstream_recovery'].update(enabled=True, total_budget_usd=10)
        self.provider.complete_all(); self.engine.tick()
        self.assertFalse(list((self.root/'state/queue').glob('downstream-*.json')))
        self.assertEqual(self.provider.create_calls, calls)
        self.assertTrue(all(t['stage'] == 'review1' and t['status'] == 'queued' for t in self.tasks()))
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        drive(self.engine, self.provider, ticks=3)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.validate()

    def test_acceptance_checkpoint_crash_resumes_without_double_allocation(self):
        self.limited_languages(); autonomous.enqueue(self.engine)
        original = self.engine.checkpoint
        def checkpoint(message):
            original(message)
            if 'allocate full automatic' in message:
                raise RuntimeError('simulated process stop after durable funding')
        with patch.object(self.engine, 'checkpoint', side_effect=checkpoint):
            with self.assertRaises(RuntimeError):
                self.engine.accept_queue()
        before = autonomous.ledger(self.state)['allocated_usd']
        self.assertGreater(before, 0)
        drive(self.engine, self.provider, ticks=4)
        self.assertEqual(len(self.tasks()), 2)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.validate()

    def test_source_change_before_create_and_human_edit_preserve_safety(self):
        self.limited_languages(); autonomous.enqueue(self.engine); self.engine.accept_queue()
        self.upstream.change_source(); self.engine.discover(); self.engine.prepare()
        self.assertEqual(self.provider.create_calls, 1)  # The unchanged second article proceeds.
        self.assertEqual(next(t for t in self.tasks() if t['article_id'] == A)['status'], 'source_error')
        self.validate()

    def test_cancelled_allocation_is_permanent_and_tampering_is_rejected(self):
        self.limited_languages(); autonomous.enqueue(self.engine); self.engine.accept_queue()
        before = autonomous.ledger(self.state)['allocated_usd']
        campaign = self.state.campaigns()[0]
        self.engine.cancel_campaign(campaign['id'])
        self.assertEqual(autonomous.ledger(self.state)['allocated_usd'], before)
        campaign = self.state.read(f'state/campaigns/{campaign["id"]}.json')
        campaign['budget_usd'] += 1; self.state.save_campaign(campaign)
        with self.assertRaises(ContractError):
            autonomous.ledger(self.state)

    def test_legacy_six_dollar_baseline_and_refresh_authority_are_not_reassigned(self):
        previous, manual = self.make_saved(stage='correct')
        self.config.runtime['autonomous_translation']['enabled'] = False
        self.config.runtime['automatic_new_translation'] = False
        self.config.runtime['automatic_downstream_recovery'].update(enabled=True, total_budget_usd=10)
        from berean_translation import downstream, continuation
        request = {'id':'old-shared', 'operation':'repair', 'model':'gpt-6.1-sol',
            'review_model':'gpt-6.1-sol', 'budget_usd':6, 'dry_run':False, 'max_articles':1,
            'requested_by':'old-approved-policy', 'continuation_policy':continuation.policy()}
        self.state.write('state/queue/old-shared.json', request)
        accepted = downstream.accept(self.engine, request)
        self.assertEqual(accepted['downstream_allocation_usd'], 6)
        self.config.runtime['automatic_source_refresh']['enabled'] = True
        article = self.state.read('state/source.json')['articles'][B]
        self.engine.accept_request({'id':'old-refresh', 'operation':'translate', 'issues':article['issue_id'],
            'languages':'afr', 'model':'gpt-5-mini', 'review_model':'gpt-5-mini', 'budget_usd':10,
            'dry_run':False, 'retry_failed':False, 'source_refresh':True, 'article_ids':[B],
            'source_translation_keys':{B:article['translation_key']}})
        before = canonical(self.state.read('state/campaigns/old-shared.json'))
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        autonomous.initialize(self.engine)
        funding = autonomous.ledger(self.state)
        self.assertEqual(funding['allocated_usd'], 6)
        self.assertEqual(funding['authority']['legacy_recovery_allocations'],
                         [{'campaign':'old-shared','allocation_usd':6.0}])
        self.assertEqual(funding['authority']['separate_legacy_refresh_envelopes'],
                         [{'campaign':'old-refresh','budget_usd':10.0}])
        self.assertEqual(canonical(self.state.read('state/campaigns/old-shared.json')), before)
        self.assertEqual(self.state.read(f'state/campaigns/{manual["id"]}.json')['budget_usd'], 5)

    def test_progressing_history_beyond_three_cycles_is_not_an_arbitrary_stop(self):
        previous, _ = self.make_saved(kind='quality_rejection')
        source = self.state.source(previous)
        records = []
        for ordinal, quote in enumerate(('Faith', 'grace', 'John', 'tree'), 1):
            task = copy.deepcopy(previous)
            task.update(id=str(ordinal)*32, autonomous_recovery=True,
                        automatic_previous_task=records[-1]['id'] if records else None,
                        findings=[{'severity':'major','location':'p','source_quote':quote,
                                   'translation_quote':'different','suggested_fix':'Correct meaning'}],
                        finished_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
            self.state.save_task(task)
            self.state.save_candidate(task, {'html': source['html'] + str(ordinal), 'title':'Faith','subtitle':None,'section':'Teaching'})
            records.append(task)
        stage, reason = autonomous.resume_stage(self.state, records[-1])
        self.assertEqual((stage, reason), ('correct', None))
        records[-1]['findings'] = records[-2]['findings']
        self.assertEqual(autonomous.resume_stage(self.state, records[-1])[1], 'continuation_repeated_findings_requires_attention')


if __name__ == '__main__':
    unittest.main()
