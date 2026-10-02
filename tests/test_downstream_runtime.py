"""Offline repair-first recovery, independent review, budgets and audit history."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from berean_translation.common import ContractError, canonical, json_hash, loads
from berean_translation.downstream import accept, enqueue_hour, ledger, validate_history
from berean_translation.requests import build_request
from berean_translation.validation import validate_repository
from support import A, B, setup, queue, drive


class DownstreamTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        queue(self.state)
        def reject(line):
            if ':review' in line['custom_id']:
                return {'score':80, 'passed':False, 'findings':[{'severity':'major','location':'p',
                    'source_quote':'Faith', 'translation_quote':'Wrong', 'suggested_fix':'Preserve faith'}]}
            return self.provider.default_result(line)
        drive(self.engine, self.provider, reject)
        self.originals = copy.deepcopy(self.state.tasks())
        self.original_campaign = self.state.read('state/campaigns/request-1.json')
        self.policy = self.config.runtime['automatic_downstream_recovery']

    def request(self, **changes):
        r = {'id':'repair-1','operation':'repair','model':'gpt-6.1-sol','review_model':'gpt-6.1-sol',
             'budget_usd':2,'max_articles':2,'dry_run':False,'requested_by':'fixture-owner'}
        r.update(changes); return r

    def enable(self, total=10):
        self.policy.update(enabled=True,total_budget_usd=total)

    def run_repair(self, responder=None):
        for _ in range(3):
            self.engine.prepare(); self.provider.complete_all(responder); self.engine.collect()
        self.state.derive(self.config)

    def test_disabled_acceptance_and_hourly_are_free(self):
        calls = self.provider.create_calls
        enqueue_hour(self.engine)
        with self.assertRaises(ContractError): accept(self.engine,self.request())
        preview=accept(self.engine,self.request(dry_run=True))
        self.assertEqual(len(preview['selection']),2)
        self.assertFalse(preview['tasks']); self.assertEqual(ledger(self.state)[0],0)
        self.assertEqual(calls,self.provider.create_calls)

    def test_repair_then_independent_review_preserves_history(self):
        self.enable(); campaign=accept(self.engine,self.request())
        children=[self.state.read(f'state/tasks/{i}/task.json') for i in campaign['tasks']]
        self.assertTrue(all(t['stage']=='correct' for t in children))
        payload=loads(build_request(self.config,self.state,children[0])[0]['body']['messages'][1]['content'])
        self.assertEqual(payload['correction_findings'],self.originals[0]['findings'])
        self.assertIn('source',payload); self.assertIn('translation',payload); self.assertIn('rejection_reason',payload)
        self.assertNotIn('translation_attempts',payload); self.assertNotIn('previous_task',payload)
        self.engine.prepare(); self.provider.complete_all(); self.engine.collect()
        child=self.state.read(f'state/tasks/{children[0]["id"]}/task.json')
        self.assertEqual(child['stage'],'review2')
        review=loads(build_request(self.config,self.state,child)[0]['body']['messages'][1]['content'])
        self.assertNotIn('correction_findings',review); self.assertNotIn('rejection_reason',review)
        self.run_repair()
        self.assertEqual(len(self.state.projection(self.config)['articles']),2)
        for original in self.originals:
            self.assertEqual(original,self.state.read(f'state/tasks/{original["id"]}/task.json'))
        self.assertEqual(self.original_campaign,self.state.read('state/campaigns/request-1.json'))
        self.assertEqual(validate_repository(self.config)['ready'],2)
        for identity in campaign['tasks']:
            task=self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual((task['translation_attempts'],task['review_attempts']),(1,1))

    def test_failure_is_bounded_and_never_selected_again(self):
        self.enable(); campaign=accept(self.engine,self.request())
        def reject(line):
            if ':review' in line['custom_id']: return {'score':94,'passed':False,'findings':[]}
            return self.provider.default_result(line)
        self.run_repair(reject)
        self.assertTrue(all(self.state.read(f'state/tasks/{i}/task.json')['status']=='not_ready' for i in campaign['tasks']))
        calls=self.provider.create_calls
        again=accept(self.engine,self.request(id='repair-2'))
        self.assertTrue(again['empty_selection']); self.assertFalse(again['tasks'])
        self.assertEqual(ledger(self.state)[0],2)
        self.run_repair(); self.assertEqual(self.provider.create_calls,calls)
        self.state.derive(self.config); validate_repository(self.config)

    def test_idempotency_and_budget_cannot_be_recycled(self):
        self.enable(total=2); first=accept(self.engine,self.request())
        self.assertEqual(accept(self.engine,self.request()),first)
        with self.assertRaises(ContractError): accept(self.engine,self.request(model='gpt-6-astra'))
        self.engine.cancel_campaign(first['id'])
        self.assertEqual(ledger(self.state)[0],2)
        with self.assertRaises(ContractError): accept(self.engine,self.request(id='repair-2',budget_usd=1))
        self.assertEqual(self.original_campaign,self.state.read('state/campaigns/request-1.json'))

    def test_source_change_before_prepare_and_before_publication(self):
        self.enable(); campaign=accept(self.engine,self.request(max_articles=1))
        task=self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.upstream.change_source(); self.engine.discover(); self.engine.prepare()
        self.assertEqual(self.state.read(f'state/tasks/{task["id"]}/task.json')['status'],'source_error')
        self.assertEqual(ledger(self.state)[0],2)

    def test_public_and_active_replacements_are_skipped(self):
        self.enable()
        original=self.originals[0]; record=self.state.record(original['language'],original['article_id'])
        record['published']={'human_reviewed':True}; self.state.save_record(record)
        original2=copy.deepcopy(self.originals[1]); original2['status']='queued'; self.state.save_task(original2)
        c=accept(self.engine,self.request(dry_run=True))
        self.assertFalse(c['selection'])

    def test_refusals_unknown_legacy_and_missing_candidates_stay_held(self):
        self.enable()
        a,b=copy.deepcopy(self.originals)
        a['failure_kind']='provider_refusal'; self.state.save_task(a)
        b['failure']='Missing, ambiguous, or truncated model response'; b.pop('failure_kind',None); self.state.save_task(b)
        c=accept(self.engine,self.request(dry_run=True))
        self.assertFalse(c['selection'])
        self.assertEqual({s['reason'] for s in c['skipped']}, {'provider_refusal_requires_owner_attention','legacy_response_outcome_unknown_requires_owner_attention'})

    def test_known_candidate_less_failure_has_one_fresh_generation_and_review(self):
        self.enable()
        previous=copy.deepcopy(next(t for t in self.originals if t['article_id']==A)); previous['failure_kind']='truncated'
        previous['translation_model_actual']=None; self.state.save_task(previous)
        self.state.path(f'state/tasks/{previous["id"]}/candidate.json').unlink()
        c=accept(self.engine,self.request(max_articles=1))
        task=self.state.read(f'state/tasks/{c["tasks"][0]}/task.json')
        self.assertEqual(task['stage'],'translate'); self.assertEqual(c['selection'][0]['mode'],'fresh')
        self.run_repair()
        task=self.state.read(f'state/tasks/{task["id"]}/task.json')
        self.assertEqual(task['status'],'complete')
        self.assertEqual((task['translation_attempts'],task['review_attempts']),(1,1))
        validate_repository(self.config)

    def test_hourly_queue_is_deduplicated_and_small(self):
        self.enable()
        with patch('berean_translation.downstream.datetime') as clock:
            clock.now.return_value.strftime.return_value='2026-10-02T12'
            enqueue_hour(self.engine); enqueue_hour(self.engine)
        requests=list(self.state.path('state/queue').glob('downstream-*.json'))
        self.assertEqual(len(requests),1)
        request=self.state.read(str(requests[0].relative_to(self.root)))
        self.assertEqual(request['max_articles'],3)
        self.engine.accept_queue()
        self.assertEqual(ledger(self.state)[0],1)

    def test_disabling_pauses_prepared_submission(self):
        self.enable(); c=accept(self.engine,self.request())
        with patch.object(self.engine,'submit'):
            self.engine.prepare()
        calls=self.provider.create_calls
        self.policy['enabled']=False
        self.engine.collect(); self.engine.prepare()
        self.assertEqual(self.provider.create_calls,calls)
        self.assertEqual(ledger(self.state)[0],2)

    def test_acceptance_checkpoint_failure_cannot_submit(self):
        self.enable()
        with patch.object(self.engine,'checkpoint',side_effect=RuntimeError('simulated push failure')):
            with self.assertRaises(RuntimeError): accept(self.engine,self.request())
        c=self.state.read('state/campaigns/repair-1.json')
        self.assertFalse(c['downstream_acceptance_complete']); self.assertFalse(c['tasks'])
        self.assertEqual(ledger(self.state)[0],2)
        self.assertEqual(accept(self.engine,self.request()),c)
        self.engine.prepare()
        validate_history(self.config,self.state,{t['id']:t for t in self.state.tasks()})

    def test_result_archive_retains_truncation_refusal_usage_and_is_immutable(self):
        self.enable(); c=accept(self.engine,self.request(max_articles=1)); self.engine.prepare()
        task=self.state.read(f'state/tasks/{c["tasks"][0]}/task.json')
        row={'response':{'status_code':200,'body':{'model':'gpt-6.1-sol','usage':{'prompt_tokens':100,'completion_tokens':50},
            'choices':[{'finish_reason':'length','message':{'content':'{"html":','refusal':None}}]}}}
        self.engine.receive(task,row)
        ended=self.state.read(f'state/tasks/{task["id"]}/task.json')
        self.assertEqual(ended['failure_kind'],'truncated')
        saved=self.state.read(f'state/tasks/{task["id"]}/attempts/correct.json')
        self.assertEqual(saved['response']['content'],'{"html":')
        self.assertEqual(saved['response']['usage']['completion_tokens'],50)
        self.assertGreater(self.state.read('state/campaigns/repair-1.json')['reported_usage_usd'],0)
        self.engine.receive(ended,{'response':{'body':{'model':'another'}}})
        self.assertEqual(saved,self.state.read(f'state/tasks/{task["id"]}/attempts/correct.json'))

    def test_raw_error_policy_codes_never_enter_recovery(self):
        from berean_translation.attempts import evidence
        from berean_translation.downstream import refusal
        for row in ({'error':{'code':'content_filter'}},
                    {'response':{'status_code':400,'body':{'error':{'code':'content_policy_violation'}}}}):
            result=evidence(row,1000)
            self.assertEqual(result['outcome'],'content_filter')
            self.assertTrue(refusal({'failure_kind':result['outcome']}))

    def test_typed_truncation_with_candidate_is_eligible(self):
        self.enable()
        previous=copy.deepcopy(next(t for t in self.originals if t['article_id']==A))
        previous.update(failure_kind='truncated', failure='Missing, ambiguous, or truncated model response')
        self.state.save_task(previous)
        c=accept(self.engine,self.request(max_articles=1))
        self.assertEqual(c['selection'][0]['previous_task_id'],previous['id'])

    def test_partial_task_is_audited_and_aborted_without_recycling(self):
        self.enable()
        real=self.state.save_record
        with patch.object(self.engine.state,'save_record',side_effect=RuntimeError('staging failed')):
            with self.assertRaises(RuntimeError): accept(self.engine,self.request())
        c=self.state.read('state/campaigns/repair-1.json')
        self.assertFalse(c['tasks'])
        children=[t for t in self.state.tasks() if t.get('downstream_recovery')]
        self.assertEqual(len(children),1)
        changed=copy.deepcopy(children[0]); changed['downstream_previous_sha256']='bad'
        self.state.save_task(changed)
        with self.assertRaises(ContractError): validate_history(self.config,self.state,{t['id']:t for t in self.state.tasks()})
        self.state.save_task(children[0]); self.engine.cancel_campaign(c['id'])
        c=self.state.read('state/campaigns/repair-1.json')
        self.assertEqual(c['status'],'acceptance_aborted'); self.assertEqual(c['tasks'],[children[0]['id']])
        self.assertEqual(ledger(self.state)[0],2)
        self.state.derive(self.config); validate_repository(self.config)

    def test_ambiguous_response_does_not_hide_policy_signal(self):
        from berean_translation.attempts import evidence
        row={'response':{'status_code':200,'body':{'choices':[
            {'finish_reason':'stop','message':{'content':'{}'}},
            {'finish_reason':'content_filter','message':{'refusal':'Provider declined'}}]}}}
        self.assertEqual(evidence(row,1000)['outcome'],'content_filter')

    def test_model_record_and_cumulative_ledger_tampering_fail_closed(self):
        self.enable(); c=accept(self.engine,self.request())
        task=self.state.read(f'state/tasks/{c["tasks"][0]}/task.json')
        changed=copy.deepcopy(task); changed['model']='gpt-6-astra'; self.state.save_task(changed)
        with self.assertRaises(ContractError): self.engine.prepare()
        self.state.save_task(task)
        record=self.state.record(task['language'],task['article_id']); changed=copy.deepcopy(record)
        changed['history']=[]; self.state.save_record(changed)
        with self.assertRaises(ContractError): validate_repository(self.config,check_index=False)
        self.state.save_record(record)
        c['allocation_before_usd']=1; self.state.save_campaign(c)
        with self.assertRaises(ContractError): ledger(self.state)

    def test_source_changes_during_review_block_publication(self):
        self.enable(); c=accept(self.engine,self.request(max_articles=1))
        self.engine.prepare(); self.provider.complete_all(); self.engine.collect(); self.engine.prepare()
        self.upstream.change_source(); self.engine.discover()
        self.provider.complete_all(); self.engine.collect()
        task=self.state.read(f'state/tasks/{c["tasks"][0]}/task.json')
        self.assertEqual(task['status'],'source_error')
        self.assertFalse(self.state.projection(self.config)['articles'])

    def test_unselected_child_cannot_bypass_count_limit(self):
        self.enable(); c=accept(self.engine,self.request(max_articles=1))
        task=self.state.read(f'state/tasks/{c["tasks"][0]}/task.json')
        forged=copy.deepcopy(task); forged['id']='f'*32
        self.state.save_task(forged); self.state.save_candidate(forged,self.state.candidate(task))
        calls=self.provider.create_calls
        with self.assertRaises(ContractError): self.engine.prepare()
        self.assertEqual(calls,self.provider.create_calls)

    def test_each_rejected_stage_retains_gate_decision_after_later_attempt(self):
        for original in self.originals:
            first=self.state.read(f'state/tasks/{original["id"]}/decisions/review1.json')
            final=self.state.read(f'state/tasks/{original["id"]}/decisions/review2.json')
            self.assertEqual(first['outcome'],'quality_rejection')
            self.assertEqual(final['outcome'],'quality_rejection')
            self.assertTrue(first['findings'])
            self.assertEqual(self.state.read(f'state/tasks/{original["id"]}/decisions/terminal.json')['outcome'],'not_ready')
