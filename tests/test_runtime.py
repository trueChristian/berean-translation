from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from berean_translation.common import ContractError, canonical, digest, json_hash, loads
from berean_translation.html import Fragment, reference_numbers, split_article, validate_translation
from berean_translation.requests import accepted_review
from berean_translation.validation import export, validate_repository
from support import A,B,ISSUE,ISSUE2,queue,setup,drive


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config,self.state,self.upstream,self.provider,self.git,self.engine = setup(self.root)

    def test_full_translation_and_review_publish_without_human_wait(self):
        queue(self.state)
        drive(self.engine,self.provider)
        report = validate_repository(self.config)
        self.assertEqual(report['ready'],2)
        self.assertEqual(self.provider.create_calls,2)
        for item in self.state.projection(self.config)['articles']:
            self.assertFalse(item['human_reviewed'])
            self.assertEqual(item['translation_model'],'gpt-4.1-mini-2025-04-14')
            self.assertIn('OpenAI',self.state.path(item['html']).read_text())
            self.assertIn('data-translation-notice="ai"',self.state.path(item['html']).read_text())
            self.assertEqual(item['images'][0]['public_path'],f'/images/articles/{item["id"]}-1.jpg')

    def test_correction_has_at_most_two_translations_and_two_reviews(self):
        def result(line):
            if line['custom_id'].endswith('review1'):
                return {'score':89,'passed':False,'findings':[{'severity':'major','location':'p1','source_quote':'faith',
                    'translation_quote':'wrong','suggested_fix':'Preserve faith'}]}
            return self.provider.default_result(line)
        queue(self.state)
        drive(self.engine,self.provider,result,ticks=8)
        self.assertEqual(self.provider.create_calls,4)
        for task in self.state.tasks():
            self.assertEqual(task['status'],'complete')
            self.assertEqual(task['translation_attempts'],2)
            self.assertEqual(task['review_attempts'],2)
            self.assertTrue(self.state.path(f'state/tasks/{task["id"]}/results/review1.json').exists())

    def test_failed_second_review_is_retained_but_never_exported(self):
        def result(line):
            if ':review' in line['custom_id']:
                return {'score':94,'passed':False,'findings':[]}
            return self.provider.default_result(line)
        queue(self.state)
        drive(self.engine,self.provider,result,ticks=10)
        self.assertEqual(self.provider.create_calls,4)
        self.assertEqual(validate_repository(self.config)['published'],0)
        for task in self.state.tasks():
            self.assertEqual(task['status'],'not_ready')
            self.assertIsNotNone(self.state.candidate(task))

    def test_score_99_cannot_override_a_critical_error(self):
        value = {'score':99,'passed':True,'findings':[{'severity':'critical','location':'p1','source_quote':'not',
            'translation_quote':'','suggested_fix':'Restore negation'}]}
        self.assertFalse(accepted_review(value))
        value['findings'] = []
        value['score'] = True
        with self.assertRaises(ContractError): accepted_review(value)

    def test_out_of_order_results_map_by_language_and_article(self):
        queue(self.state,languages='afr,deu')
        drive(self.engine,self.provider)
        keys = {(i['language'],i['id']) for i in self.state.projection(self.config)['articles']}
        self.assertEqual(keys,{('afr',A),('afr',B),('deu',A),('deu',B)})
        self.assertEqual(validate_repository(self.config)['ready'],4)

    def test_duplicate_manual_requests_do_not_duplicate_paid_work(self):
        queue(self.state,'one'); queue(self.state,'two')
        drive(self.engine,self.provider)
        queue(self.state,'three'); drive(self.engine,self.provider)
        self.assertEqual(self.provider.create_calls,2)
        self.assertEqual(len(self.state.tasks()),2)

    def test_dry_run_reserves_nothing_and_makes_no_api_calls(self):
        queue(self.state,dry_run=True)
        drive(self.engine,self.provider)
        self.assertEqual(self.provider.create_calls,0)
        self.assertEqual(self.state.tasks(),[])
        campaign = self.state.campaigns()[0]
        self.assertEqual(len(campaign['selection']),2)
        self.assertEqual(campaign['status'],'planned')

    def test_missing_api_key_still_discovers_and_queues(self):
        queue(self.state)
        self.engine.provider = None
        self.engine.tick()
        self.assertEqual(len(self.state.read('state/source.json')['articles']),2)
        self.assertTrue(all(t['status']=='queued' for t in self.state.tasks()))
        self.assertEqual(self.provider.create_calls,0)

    def test_budget_blocks_before_upload_or_submission(self):
        queue(self.state,budget_usd=0.000001)
        drive(self.engine,self.provider)
        self.assertEqual(self.provider.upload_calls,0)
        self.assertEqual(self.provider.create_calls,0)
        self.assertTrue(all(t['status']=='budget_blocked' for t in self.state.tasks()))
        validate_repository(self.config)

    def test_uncertain_success_is_recovered_without_resubmission(self):
        queue(self.state)
        self.provider.raise_create = 'after'
        self.engine.tick()
        self.assertEqual(self.state.batches()[0]['status'],'submission_unknown')
        self.provider.raise_create = None
        drive(self.engine,self.provider,ticks=8)
        self.assertEqual(self.provider.create_calls,2)
        self.assertEqual(validate_repository(self.config)['ready'],2)

    def test_uncertain_absence_never_triggers_automatic_resubmission(self):
        queue(self.state)
        self.provider.raise_create = 'before'
        drive(self.engine,self.provider,ticks=5)
        self.assertEqual(self.provider.create_calls,1)
        self.assertEqual(self.state.batches()[0]['status'],'submission_unknown')
        self.assertEqual(validate_repository(self.config)['published'],0)

    def test_upload_failure_is_bounded_without_charged_requests(self):
        queue(self.state); self.provider.raise_upload = True
        drive(self.engine,self.provider,ticks=8)
        self.assertEqual(self.provider.upload_calls,3)
        self.assertEqual(self.provider.create_calls,0)
        self.assertTrue(all(t['status']=='not_ready' for t in self.state.tasks()))

    def test_state_is_checkpointed_before_billable_create(self):
        original = self.provider.create
        def create(*args):
            self.assertEqual(self.state.batches()[0]['status'],'submitting')
            self.assertIn('before the billable request',self.git.checkpoints[-1])
            self.assertGreater(self.state.campaigns()[0]['reserved_usd'],0)
            return original(*args)
        self.provider.create = create
        queue(self.state); self.engine.tick()

    def test_partial_expiration_does_not_discard_successful_articles(self):
        queue(self.state); self.engine.tick()
        task_a = next(t for t in self.state.tasks() if t['article_id']==A)
        self.provider.complete_all(lambda line:None if line['custom_id'].startswith(task_a['id']) else self.provider.default_result(line),status='expired')
        drive(self.engine,self.provider)
        self.assertEqual(validate_repository(self.config)['ready'],1)
        self.assertEqual(self.state.read(f'state/tasks/{task_a["id"]}/task.json')['status'],'not_ready')

    def test_duplicate_result_custom_ids_block_the_batch(self):
        queue(self.state); self.engine.tick(); self.provider.complete_all()
        batch = next(iter(self.provider.batches.values()))
        data = self.provider.files[batch['output_file_id']]
        self.provider.files[batch['output_file_id']] = data+b'\n'+data.splitlines()[0]
        self.engine.tick()
        self.assertTrue(all(t['status']=='not_ready' for t in self.state.tasks()))
        self.assertEqual(self.provider.create_calls,1)

    def test_refusal_and_truncation_are_not_published(self):
        queue(self.state); self.engine.tick(); self.provider.complete_all()
        batch = next(iter(self.provider.batches.values()))
        rows = [loads(line) for line in self.provider.files[batch['output_file_id']].splitlines()]
        rows[0]['response']['body']['choices'][0]['finish_reason'] = 'length'
        rows[1]['response']['body']['choices'][0]['message']['refusal'] = 'refused'
        self.provider.files[batch['output_file_id']] = b'\n'.join(canonical(r) for r in rows)
        drive(self.engine,self.provider)
        self.assertEqual(self.provider.create_calls,1)
        self.assertTrue(all(t['status']=='not_ready' for t in self.state.tasks()))

    def test_removing_notice_in_human_commit_marks_reviewed(self):
        queue(self.state); drive(self.engine,self.provider)
        pub = self.state.record('afr',A)['published']
        candidate,tail,text = self.state.publication_candidate(pub)
        self.state.path(pub['html_path']).write_text(candidate['html']+'\n')
        self.engine.tick()
        record = self.state.record('afr',A)
        self.assertTrue(record['published']['human_reviewed'])
        self.assertIsNotNone(record['published']['human_review'])
        self.assertEqual(validate_repository(self.config)['ready'],2)

    def test_bot_cannot_claim_human_review(self):
        queue(self.state); drive(self.engine,self.provider)
        pub = self.state.record('afr',A)['published']
        body,_,_ = self.state.publication_candidate(pub)
        self.state.path(pub['html_path']).write_text(body['html'])
        self.git.bot = True
        with self.assertRaises(ContractError): self.engine.tick()

    def test_ai_review_of_human_reviewed_article_creates_proposal(self):
        queue(self.state); drive(self.engine,self.provider)
        pub = self.state.record('afr',A)['published']
        body,_,_ = self.state.publication_candidate(pub)
        self.state.path(pub['html_path']).write_text(body['html'])
        self.engine.tick()
        before = self.state.path(pub['html_path']).read_bytes()
        queue(self.state,'review',operation='review',issues='all')
        drive(self.engine,self.provider)
        record = self.state.record('afr',A)
        task = self.state.read(f'state/tasks/{record["latest_task"]}/task.json')
        self.assertEqual(task['status'],'proposal')
        self.assertEqual(before,self.state.path(pub['html_path']).read_bytes())
        self.assertTrue(record['published']['human_reviewed'])

    def test_human_review_during_inflight_ai_review_is_protected(self):
        queue(self.state); drive(self.engine,self.provider)
        queue(self.state,'review',operation='review',issues='all')
        self.engine.tick()
        pub = self.state.record('afr',A)['published']
        body,_,_ = self.state.publication_candidate(pub)
        self.state.path(pub['html_path']).write_text(body['html'])
        drive(self.engine,self.provider)
        record = self.state.record('afr',A)
        self.assertTrue(record['published']['human_reviewed'])
        task = self.state.read(f'state/tasks/{record["latest_task"]}/task.json')
        self.assertEqual(task['status'],'proposal')

    def test_failed_ai_rereview_keeps_the_last_good_publication(self):
        queue(self.state); drive(self.engine,self.provider)
        before = self.state.record('afr',A)['published']
        queue(self.state,'review',operation='review',issues='all')
        def result(line):
            if ':review' in line['custom_id']:
                return {'score':91,'passed':False,'findings':[]}
            return self.provider.default_result(line)
        drive(self.engine,self.provider,result,ticks=8)
        after = self.state.record('afr',A)['published']
        self.assertEqual(before,after)
        self.assertEqual(validate_repository(self.config)['ready'],2)

    def test_source_change_marks_stale_without_automatic_spending(self):
        queue(self.state); drive(self.engine,self.provider)
        calls = self.provider.create_calls
        self.upstream.change_source()
        drive(self.engine,self.provider)
        item = next(i for i in self.state.projection(self.config)['articles'] if i['id']==A)
        self.assertEqual(item['status'],'stale')
        self.assertEqual(self.provider.create_calls,calls)
        queue(self.state,'changed'); drive(self.engine,self.provider)
        self.assertEqual(validate_repository(self.config)['ready'],2)
        self.assertEqual(self.provider.create_calls,calls+2)

    def test_new_issue_is_discovered_and_next_selects_it(self):
        queue(self.state); drive(self.engine,self.provider)
        self.upstream.revision = 'b'*40
        self.upstream.issues.append({'id':ISSUE2,'slug':'heartbeat-remnant-2024-winter','source_id':'heartbeat-remnant-2024-winter','date':{},'publication':'Test'})
        item = copy.deepcopy(self.upstream.articles[0])
        identity = '55555555-5555-4555-8555-555555555555'
        item.update(id=identity,issue_id=ISSUE2)
        item['html']['repository_path'] = f'content/articles/{identity}.html'
        item['images'][0]['public_path'] = f'/images/articles/{identity}-1.jpg'
        self.upstream.contents[item['html']['repository_path']] = self.upstream.contents[f'content/articles/{A}.html'].replace(A,identity)
        self.upstream.articles.append(item); self.upstream.rebuild()
        self.engine.tick()
        self.assertEqual(len(self.state.read('state/source.json')['issues']),2)
        queue(self.state,'new-issue'); drive(self.engine,self.provider)
        self.assertEqual(self.state.read('state/campaigns/new-issue.json')['issues'],[ISSUE2])
        self.assertEqual(validate_repository(self.config)['ready'],3)

    def test_withdrawn_source_is_excluded_from_export(self):
        queue(self.state); drive(self.engine,self.provider)
        self.upstream.revision = 'b'*40
        self.upstream.articles = [a for a in self.upstream.articles if a['id'] != A]
        self.upstream.rebuild(); self.engine.tick()
        result = export(self.config,self.root/'.build/export',self.upstream.manifest,'b'*40,translation_revision='c'*40)
        self.assertEqual(result['article_count'],1)
        self.assertEqual(result['omitted'][0]['id'],A)

    def test_export_checks_the_actual_selected_english_manifest(self):
        queue(self.state); drive(self.engine,self.provider)
        self.upstream.change_source()
        result = export(self.config,self.root/'.build/export',self.upstream.manifest,'b'*40,
                        base='/website/',translation_revision='c'*40)
        self.assertEqual(result['article_count'],1)
        index = read_json_local(self.root/'.build/export/index.json')
        output = (self.root/'.build/export'/index['articles'][0]['html']).read_text()
        self.assertIn('/website/images/articles/',output)
        self.assertIn('/website/en/articles/',output)
        self.assertFalse((self.root/'.build/export/state').exists())
        self.assertFalse((self.root/'.build/export/config').exists())
        self.assertFalse((self.root/'.build/export/public').exists())

    def test_export_refuses_overwrite_and_unsafe_destinations(self):
        queue(self.state); drive(self.engine,self.provider)
        for destination in (self.root,self.root/'content',self.root.parent):
            with self.assertRaises(ContractError):
                export(self.config,destination,self.upstream.manifest,'a'*40,translation_revision='c'*40)
        destination = self.root/'.build/occupied'
        destination.mkdir(parents=True); (destination/'keep').write_text('safe')
        with self.assertRaises(ContractError):
            export(self.config,destination,self.upstream.manifest,'a'*40,translation_revision='c'*40)
        self.assertEqual((destination/'keep').read_text(),'safe')

    def test_tampered_snapshot_or_batch_payload_fails_closed(self):
        queue(self.state); self.engine.tick()
        task = self.state.tasks()[0]
        snapshot = self.state.read(task['source_snapshot'])
        snapshot['html'] += 'unauthorized change'
        self.state.write(task['source_snapshot'],snapshot)
        with self.assertRaises(ContractError): validate_repository(self.config)

    def test_cancellation_does_not_start_a_review_or_correction(self):
        queue(self.state); self.engine.tick()
        self.engine.cancel_campaign('request-1')
        drive(self.engine,self.provider)
        self.assertEqual(self.provider.create_calls,1)
        self.assertTrue(all(t['status']=='cancelled' for t in self.state.tasks()))


def read_json_local(path):
    return loads(path.read_bytes())


if __name__ == '__main__': unittest.main()
