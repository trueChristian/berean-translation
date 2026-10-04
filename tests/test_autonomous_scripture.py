"""Production admission hooks with offline GetBible and Batch fixtures."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from berean_translation.common import ContractError, canonical, loads, read_json
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.validation import validate_repository
from support import A, REPO_ROOT, drive, queue, setup
from test_scripture_evidence import FakeMCP, fixture


class AutonomousScriptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        shutil.copytree(REPO_ROOT/'data', self.root/'data')
        shutil.copytree(REPO_ROOT/'docs/third-party', self.root/'docs/third-party')
        self.config.runtime['autonomous_translation'].update(enabled=True, page_size=2, max_active_tasks=2)
        self.config.runtime['automatic_new_translation'] = True
        self.config.runtime['scripture_quotes_enabled'] = True
        self.config.languages = {'deu':self.config.languages['deu']}
        self.upstream.articles = self.upstream.articles[:1]
        self.english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
        self.target = fixture('luther1545')['structuredContent']['data']['verses'][1]['text']
        self.upstream.contents[f'content/articles/{A}.html'] = self.upstream.contents[f'content/articles/{A}.html'].replace(
            'Faith and <em>grace</em>. John 3:16–18.', f'“{self.english}” (John 4:16).')
        self.upstream.rebuild(); self.engine.discover()
        self.fake = FakeMCP(); self.engine.scripture_provider = GetBibleMCP(self.fake)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            mock = patch(target, side_effect=AssertionError('No real network in integration tests'))
            mock.start(); self.addCleanup(mock.stop)

    def responder(self, line):
        result = self.provider.default_result(line)
        if line['custom_id'].split(':')[1].startswith('review'):
            return result
        target = self.target.strip()
        result['html'] = result['html'].replace(self.english, target).replace('John 4:16', 'Johannes 4:16')
        result['scripture_selections'] = [{'quote_id':'q1', 'block':'/article[1]/p[1]',
            'start':1, 'end':1+len(target), 'fragments':[[{'verse':16,'start':0,'end':len(self.target)}]]}]
        return result

    def test_enqueue_translate_review_and_publish_with_frozen_evidence(self):
        drive(self.engine, self.provider, self.responder, ticks=5)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 1)
        task = self.state.tasks()[0]
        campaign = self.state.campaigns()[0]
        self.assertEqual(campaign['scripture_quotes']['version'], '1')
        self.assertIn('scripture_evidence_sha256', task)
        self.assertTrue(self.state.read(f'state/tasks/{task["id"]}/scripture-selections.json'))
        self.assertTrue(all(request[1]['api_version'] == 'v2' for request in self.fake.calls))
        validate_repository(self.config)

    def test_new_review_contract_and_scripture_evidence_freeze_together(self):
        self.config.runtime['review_contract_version'] = 2
        drive(self.engine, self.provider, self.responder, ticks=5)
        campaign = self.state.campaigns()[0]
        self.assertEqual(campaign['review_contract_version'], 2)
        self.assertEqual(campaign['execution']['review_contract_version'], 2)
        self.assertEqual(campaign['scripture_quotes']['version'], '1')
        task = self.state.tasks()[0]
        self.assertEqual(task['status'], 'complete')
        result = self.state.read(f'state/tasks/{task["id"]}/results/review1.json')
        self.assertTrue(result['result']['findings_complete'])
        review_batch = next(b for b in self.state.batches() if b['stage'] == 'review1')
        line = loads(self.state.path(f'state/batches/{review_batch["id"]}/input.jsonl').read_bytes().splitlines()[0])
        payload = loads(line['body']['messages'][1]['content'])
        schema = line['body']['response_format']['json_schema']['schema']
        self.assertIn('scripture_selection_audit', payload)
        self.assertEqual(schema['properties']['findings']['maxItems'], 30)
        self.assertIn('findings_complete', schema['required'])
        validate_repository(self.config)

    def test_unknown_prepared_scripture_contract_cannot_upload_or_create(self):
        self.config.runtime['autonomous_translation']['enabled'] = False
        self.config.runtime['automatic_new_translation'] = False
        self.engine.accept_request(queue(self.state, 'manual-quotes', languages='deu'))
        with patch.object(self.engine, 'submit'):
            self.engine.prepare()
        batch = self.state.batches()[0]
        campaign = self.state.campaigns()[0]
        original = copy.deepcopy(campaign['scripture_quotes'])
        for bad in ({**original, 'version':'999'}, None, False, {}):
            campaign['scripture_quotes'] = bad
            self.state.save_campaign(campaign)
            with self.subTest(policy=bad), self.assertRaises(ContractError):
                self.engine.submit(batch)
            self.assertEqual(self.provider.upload_calls, 0)
            self.assertEqual(self.provider.create_calls, 0)
            self.assertEqual(self.state.batches()[0]['status'], 'prepared')

    def test_saved_final_review_without_quote_audit_repairs_saved_text_first(self):
        self.config.runtime['scripture_quotes_enabled'] = False
        self.config.runtime['autonomous_translation']['enabled'] = False
        self.config.runtime['automatic_new_translation'] = False
        request = queue(self.state, 'legacy', languages='deu')
        campaign = self.engine.accept_request(request)
        previous = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        source = self.state.source(previous)
        self.state.save_candidate(previous, {'html':source['html'], 'title':source['article']['title'],
                                           'subtitle':source['article']['subtitle'], 'section':source['article']['section']})
        previous.update(stage='review2', translation_model_actual='gpt-4.1-mini')
        self.engine.finish(previous, 'budget_blocked')
        before = canonical(self.state.read(f'state/tasks/{previous["id"]}/task.json'))
        self.config.runtime.update(scripture_quotes_enabled=True, automatic_new_translation=True)
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.engine.tick()
        task = next(t for t in self.state.tasks() if t.get('autonomous'))
        self.assertEqual(task['stage'], 'correct')
        self.assertEqual(task['scripture_resume_reason'], 'saved_candidate_requires_quotation_audit')
        self.assertEqual(canonical(self.state.read(f'state/tasks/{previous["id"]}/task.json')), before)
        drive(self.engine, self.provider, self.responder, ticks=4)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 1)
        validate_repository(self.config)

    def test_read_failure_is_resumable_and_never_admits_paid_work(self):
        from berean_translation.scripture_provider import ScriptureProviderError
        self.fake.failure = ScriptureProviderError('Offline read failure')
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, 0)
        self.assertFalse(self.state.campaigns())
        self.assertFalse(list((self.root/'state/queue-errors').glob('*.json')))
        self.fake.failure = None
        drive(self.engine, self.provider, self.responder, ticks=4)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 1)

    def test_missing_approved_edition_stays_held_without_billing(self):
        self.config.languages = {'hin': read_json(REPO_ROOT/'config/languages.json')['hin']}
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, 0)
        self.assertFalse(self.state.campaigns())
        holds = list((self.root/'state/automatic-holds').glob('*.json'))
        self.assertEqual(read_json(holds[0])['reason'], 'missing_edition')

    def test_attention_holds_do_not_block_other_language_pages(self):
        languages = read_json(REPO_ROOT/'config/languages.json')
        self.config.languages = {'hin':languages['hin'], 'deu':languages['deu']}
        self.config.runtime['autonomous_translation'].update(page_size=1, max_active_tasks=1)
        drive(self.engine, self.provider, self.responder, ticks=6)
        published = self.state.projection(self.config)['articles']
        self.assertEqual([item['language'] for item in published], ['deu'])
        frontier = self.state.read('RECOVERY.json')
        self.assertEqual(next(row for row in frontier['items'] if row['language']=='hin')['reason'], 'missing_edition')

    def test_funded_review_advances_before_slow_new_evidence_admission(self):
        from support import B
        from berean_translation.scripture_provider import ScriptureProviderError
        self.engine.tick(); self.provider.complete_all(self.responder)
        article = copy.deepcopy(self.upstream.articles[0]); article.update(id=B, sequence=2)
        article['html']['repository_path'] = f'content/articles/{B}.html'
        article['images'] = [{**image, 'public_path':image['public_path'].replace(A,B)} for image in article['images']]
        self.upstream.articles.append(article)
        self.upstream.contents[f'content/articles/{B}.html'] = self.upstream.contents[f'content/articles/{A}.html'].replace(A,B)
        self.upstream.rebuild()
        alive = [True]
        def delayed_failure(name, arguments):
            alive[0] = False
            raise ScriptureProviderError('Simulated read reaches worker time boundary')
        self.engine.scripture_provider = GetBibleMCP(delayed_failure)
        self.engine.tick(continue_work=lambda:alive[0])
        task = next(t for t in self.state.tasks() if t['article_id']==A)
        self.assertEqual((task['stage'], task['status']), ('review1','in_batch'))
        self.assertEqual(self.provider.create_calls, 2)
        self.assertFalse([t for t in self.state.tasks() if t['article_id']==B])

    def legacy_held(self):
        self.config.runtime.update(scripture_quotes_enabled=False, automatic_new_translation=False)
        self.config.runtime['autonomous_translation']['enabled'] = False
        campaign = self.engine.accept_request(queue(self.state, 'legacy-held', languages='deu'))
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        source = self.state.source(task)
        self.state.save_candidate(task, {'html':source['html'], **{
            k:source['article'][k] for k in ('title','subtitle','section')}})
        task.update(stage='review2', translation_model_actual='gpt-4.1-mini', failure_kind='quality_rejection',
            findings=[{'severity':'major','location':'p','source_quote':self.english,
                       'translation_quote':self.english,'suggested_fix':'Use the approved quotation words'}])
        self.engine.finish(task, 'not_ready', 'Final review failed')
        campaign['status'] = 'finished'; self.state.save_campaign(campaign)
        self.config.runtime['scripture_quotes_enabled'] = True
        return self.state.read(f'state/tasks/{task["id"]}/task.json'), campaign

    def test_exact_recovery_prefetch_failure_cannot_allocate_or_partially_accept(self):
        from berean_translation.scripture_provider import ScriptureProviderError
        from berean_translation.scripture_evidence import ScriptureAttention
        previous, original = self.legacy_held()
        request = {'id':'exact', 'operation':'review', 'recovery_of_campaign':original['id'],
            'previous_task_ids':[previous['id']], 'model':'gpt-6-luna', 'review_model':'gpt-6-luna',
            'budget_usd':1, 'dry_run':False}
        self.state.write('state/queue/exact.json', request)
        self.fake.failure = ScriptureProviderError('offline failure')
        with self.assertRaises(ScriptureAttention): self.engine.accept_request(request)
        self.assertIsNone(self.state.read('state/campaigns/exact.json'))
        self.assertEqual(len(self.state.tasks()), 1)
        self.assertEqual(self.provider.create_calls, 0)
        self.state.derive(self.config); validate_repository(self.config)

    def test_exact_recovery_quote_repair_can_complete_with_original_cap_intact(self):
        previous, original = self.legacy_held()
        request = {'id':'exact', 'operation':'review', 'recovery_of_campaign':original['id'],
            'previous_task_ids':[previous['id']], 'model':'gpt-6-luna', 'review_model':'gpt-6-luna',
            'budget_usd':1, 'dry_run':False}
        self.state.write('state/queue/exact.json', request)
        campaign = self.engine.accept_request(request)
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.assertEqual(task['stage'], 'correct')
        drive(self.engine, self.provider, self.responder, ticks=4)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 1)
        self.assertEqual(self.state.read(f'state/campaigns/{original["id"]}.json')['budget_usd'], 5)
        validate_repository(self.config)

    def test_exact_recovery_interruption_can_abort_new_evidence_fields(self):
        previous, original = self.legacy_held()
        request = {'id':'exact', 'operation':'review', 'recovery_of_campaign':original['id'],
            'previous_task_ids':[previous['id']], 'model':'gpt-6-luna', 'review_model':'gpt-6-luna',
            'budget_usd':1, 'dry_run':False}
        self.state.write('state/queue/exact.json', request)
        original_save = self.engine.state.save_task
        def interrupt(task):
            original_save(task)
            if task['campaign'] == 'exact': raise RuntimeError('simulated staging stop')
        with patch.object(self.engine.state, 'save_task', side_effect=interrupt):
            with self.assertRaises(RuntimeError): self.engine.accept_request(request)
        self.engine.cancel_campaign('exact')
        self.assertEqual(self.state.read('state/campaigns/exact.json')['status'], 'acceptance_aborted')
        self.assertEqual(self.provider.create_calls, 0)
        self.state.derive(self.config); validate_repository(self.config)

    def test_rejected_downstream_quote_audit_preserves_valid_global_history(self):
        previous, _ = self.legacy_held()
        from berean_translation import continuation
        request = {'id':'gh-999', 'operation':'repair', 'model':'gpt-6.1-sol', 'review_model':'gpt-6.1-sol',
            'budget_usd':3, 'dry_run':False, 'max_articles':1, 'requested_by':'fixture-owner',
            'continuation_policy':continuation.policy(), 'manual_authorization':{
                'kind':'github_workflow_dispatch','repository':'fixture/repo',
                'workflow_ref':'fixture/repo/.github/workflows/ai-repair.yml@refs/heads/main',
                'run_id':'999','actor':'fixture-owner'}}
        self.state.write('state/queue/gh-999.json', request)
        campaign = self.engine.accept_request(request)
        self.engine.prepare()
        def invalid(line):
            result = self.responder(line)
            result['scripture_selections'] = []
            return result
        self.provider.complete_all(invalid); self.engine.collect()
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.assertEqual(task['status'], 'not_ready')
        self.assertIn(self.target.strip(), self.state.candidate(task)['html'])
        self.assertEqual(self.provider.create_calls, 1)
        self.state.derive(self.config); validate_repository(self.config)

    def test_evidence_expiring_during_provider_batch_resumes_saved_review(self):
        from datetime import datetime, timezone
        from berean_translation import scripture_evidence, autonomous
        self.engine.tick(); self.provider.complete_all(self.responder)
        self.engine.collect()
        class Expired(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2100,1,1,tzinfo=timezone.utc)
        with patch.object(scripture_evidence, 'datetime', Expired):
            self.engine.prepare()
        previous = self.state.tasks()[0]
        self.assertEqual(previous['failure_kind'], 'scripture_evidence_expired')
        self.assertEqual(previous['stage'], 'review1')
        self.assertEqual(autonomous.resume_stage(self.state, previous), ('review1', None))
        before = self.provider.create_calls
        self.engine.tick()
        child = next(t for t in self.state.tasks() if t.get('automatic_previous_task') == previous['id'])
        self.assertEqual(child['stage'], 'review1')
        self.assertEqual(self.provider.create_calls, before + 1)
        self.provider.complete_all(self.responder); self.engine.tick()
        self.assertEqual(len(self.state.projection(self.config)['articles']), 1)
        validate_repository(self.config)

    def test_prepared_batch_cannot_create_after_evidence_expiry(self):
        from datetime import datetime, timezone
        from berean_translation import scripture_evidence
        with patch.object(self.engine, 'submit'):
            self.engine.tick()
        batch = self.state.batches()[0]
        class Expired(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2100,1,1,tzinfo=timezone.utc)
        with patch.object(scripture_evidence, 'datetime', Expired):
            self.engine.submit(batch)
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(self.state.batches()[0]['status'], 'cancelled_before_submission')
        self.state.derive(self.config); validate_repository(self.config)

    def test_expiry_during_intent_checkpoint_is_checked_before_create(self):
        from datetime import datetime, timezone
        from berean_translation import scripture_evidence
        expired = [False]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2100,1,1,tzinfo=timezone.utc) if expired[0] else datetime.now(timezone.utc)
        checkpoint = self.engine.checkpoint
        def advance(message):
            checkpoint(message)
            if 'submission intent' in message: expired[0] = True
        with patch.object(scripture_evidence, 'datetime', Clock), patch.object(self.engine, 'checkpoint', side_effect=advance):
            self.engine.tick()
        self.assertEqual(self.provider.upload_calls, 1)
        self.assertEqual(self.provider.create_calls, 0)
        self.assertTrue(self.state.batches()[0]['create_not_called'])
        self.state.derive(self.config); validate_repository(self.config)

    def test_human_exclusion_precedes_evidence_fetch(self):
        with patch.object(self.engine, 'human_protected', return_value=True):
            self.engine.tick()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.provider.create_calls, 0)


if __name__ == '__main__': unittest.main()
