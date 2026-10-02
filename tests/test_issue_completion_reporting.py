"""Offline whole-issue lifecycle; mocked outputs are not linguistic validation."""
import copy
import tempfile
import unittest
from pathlib import Path
from berean_translation.common import ContractError, canonical
from berean_translation.downstream import accept, ledger
from berean_translation.recovery import money
from berean_translation.validation import validate_repository
from support import A, ISSUE, setup, queue, drive


class IssueCompletionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.config,self.state,self.upstream,self.provider,self.git,self.engine=setup(self.root)

    def test_entire_issue_all_languages_held_repair_review_publish(self):
        queue(self.state, languages='all', issues='all', budget_usd=5)
        held_ids=set()
        def first_line(line):
            identity,stage=line['custom_id'].split(':')
            task=self.state.read(f'state/tasks/{identity}/task.json')
            if task['language'] in ('afr','deu') and task['article_id']==A and stage.startswith('review'):
                held_ids.add(identity)
                return {'score':90,'passed':False,'findings':[{'severity':'major','location':'p',
                    'source_quote':'Faith','translation_quote':'Candidate','suggested_fix':'Preserve the source meaning'}]}
            return self.provider.default_result(line)
        drive(self.engine,self.provider,first_line)
        expected=2*len(self.config.languages)
        self.assertEqual(len(self.state.projection(self.config)['articles']),expected-2)
        original=self.state.read('state/campaigns/request-1.json')
        self.assertEqual(original['status'],'finished')
        before={i:self.state.read(f'state/tasks/{i}/task.json') for i in held_ids}
        text=self.state.path('STATUS.md').read_text()
        self.assertIn('not that every requested translation passed',text)
        self.assertIn('2 held',text)
        self.assertIn('live deployment is verified separately',text)
        self.config.runtime['automatic_downstream_recovery'].update(enabled=True,total_budget_usd=2)
        request={'id':'whole-issue-repair','operation':'repair','model':'gpt-6.1-sol',
                 'review_model':'gpt-6.1-sol','budget_usd':2,'dry_run':False,'max_articles':2}
        repaired=accept(self.engine,request)
        self.assertEqual(len(repaired['tasks']),2)
        self.assertEqual(repaired['review_output_tokens'],8192)
        for _ in range(3):
            self.engine.prepare(); self.provider.complete_all(); self.engine.collect()
        self.state.derive(self.config)
        self.assertEqual(len(self.state.projection(self.config)['articles']),expected)
        for identity in repaired['tasks']:
            task=self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual((task['translation_attempts'],task['review_attempts']),(1,1))
            self.assertEqual(task['status'],'complete')
        self.assertEqual(self.state.read('state/campaigns/request-1.json'),original)
        for identity,task in before.items(): self.assertEqual(self.state.read(f'state/tasks/{identity}/task.json'),task)
        self.assertEqual(ledger(self.state)[0],2)
        validate_repository(self.config)

    def test_finished_budget_blocked_is_held_not_completed_and_does_not_spend(self):
        queue(self.state,languages='all',budget_usd=.000001)
        self.engine.tick()
        self.assertEqual(self.provider.create_calls,0)
        campaign=self.state.campaigns()[0]
        self.assertEqual(campaign['status'],'finished')
        self.assertTrue(all(t['status']=='budget_blocked' for t in self.state.tasks()))
        report=self.state.path('STATUS.md').read_text()
        self.assertIn(f'{len(self.state.tasks())} held',report)
        self.assertNotIn('40 complete',report)
        self.assertFalse(self.state.projection(self.config)['articles'])

    def test_reporting_does_not_rewrite_history_and_distinguishes_other_outcomes(self):
        queue(self.state); drive(self.engine,self.provider)
        tasks=self.state.tasks()
        tasks[0]['status']='proposal'; tasks[1]['status']='cancelled'
        for task in tasks:self.state.save_task(task)
        before={str(path):path.read_bytes() for path in self.state.path('state').rglob('*.json')}
        self.state.derive(self.config)
        self.assertEqual(before,{str(path):path.read_bytes() for path in self.state.path('state').rglob('*.json')})
        text=self.state.path('STATUS.md').read_text()
        self.assertIn('1 proposal',text);self.assertIn('1 cancelled',text)
        self.assertIn('Processing state',text)

    def test_failed_rereview_keeps_current_publication_distinct_from_task_outcome(self):
        queue(self.state); drive(self.engine,self.provider)
        published=copy.deepcopy(self.state.projection(self.config)['articles'])
        queue(self.state,'rereview',operation='review')
        def reject(line):
            if ':review' in line['custom_id']:return {'score':90,'passed':False,'findings':[]}
            return self.provider.default_result(line)
        drive(self.engine,self.provider,reject)
        self.assertEqual(self.state.projection(self.config)['articles'],published)
        text=self.state.path('STATUS.md').read_text()
        self.assertIn('2 held',text)
        self.assertEqual(len(self.state.projection(self.config)['articles']),2)

    def test_recovery_report_uses_exact_lifetime_budget_boundary(self):
        self.engine.discover()
        self.engine.accept_request(queue(self.state))
        # Seed two held candidates without preparing or submitting a batch.
        for task in self.state.tasks():
            source=self.state.source(task)
            self.state.save_candidate(task,{'html':source['html'],
                **{key:source['article'].get(key) for key in ('title','subtitle','section')}})
            task.update(status='not_ready',stage='review2',failure_kind='quality_rejection',
                        translation_model_actual=task['model'],translation_attempts=2,review_attempts=2)
            self.state.save_task(task)
        self.assertEqual(len(self.state.tasks()),2)
        policy=self.config.runtime['automatic_downstream_recovery']
        policy.update(enabled=True,total_budget_usd=.3,campaign_budget_usd=.2)
        request={'id':'budget-boundary-first','operation':'repair','model':policy['model'],
                 'review_model':policy['review_model'],'budget_usd':.1,'dry_run':False,'max_articles':1}
        first=accept(self.engine,request)
        self.assertEqual(len(first['tasks']),1)
        self.assertEqual(ledger(self.state)[0],money('.1'))
        before={str(path):path.read_bytes() for path in self.state.path('state').rglob('*.json')}
        next_request={**request,'id':'budget-boundary-next','budget_usd':.2}

        for cap,status in ((.299999,'Budget blocked:'),(.3,'Enabled:')):
            with self.subTest(cap=cap):
                policy['total_budget_usd']=cap
                self.state.derive(self.config)
                text=self.state.path('STATUS.md').read_text()
                self.assertIn(status,text)
                self.assertIn('Accepted lifetime recovery allocations: $0.100000 / $0.30.',text)
                self.assertEqual(before,{str(path):path.read_bytes() for path in self.state.path('state').rglob('*.json')})
                validate_repository(self.config)
                if cap < .3:
                    with self.assertRaisesRegex(ContractError,'lifetime spending envelope exhausted'):
                        accept(self.engine,next_request)

        second=accept(self.engine,next_request)
        self.assertEqual(len(second['tasks']),1)
        self.assertEqual(ledger(self.state)[0],money('.3'))
        self.assertEqual(self.provider.create_calls,0)
        self.assertEqual(self.provider.upload_calls,0)
