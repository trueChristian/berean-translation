from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from berean_translation.common import ContractError, canonical, digest, loads, safe_path, write_json
from berean_translation.config import Config
from berean_translation.html import Fragment, notice, reference_numbers, rewrite_export_urls, validate_translation
from berean_translation.provider import OpenAIProvider
from berean_translation.queue import enqueue_github
from berean_translation.source import translation_key
from berean_translation.validation import validate_repository
from support import A,B,ISSUE,setup,queue,drive,REPO_ROOT


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config,self.state,self.upstream,self.provider,self.git,self.engine = setup(self.root)
        self.engine.discover()
        self.source = self.upstream.client.snapshot(A)
        self.candidate = {k:self.source['article'].get(k) for k in ('title','subtitle','section')}
        self.candidate['html'] = self.source['html']

    def test_real_key_cli_refuses_nondurable_submission(self):
        from berean_translation.cli import main
        with patch.dict('os.environ',{'OPENAI_API_KEY':'not-a-real-key'}):
            with patch('berean_translation.cli.OpenAIProvider') as provider:
                self.assertEqual(main(['--root',str(self.root),'tick']),1)
                provider.assert_not_called()

    def test_status_report_shows_issue_language_and_corrections(self):
        queue(self.state,languages='afr,deu')
        drive(self.engine,self.provider)
        report = (self.root/'STATUS.md').read_text()
        self.assertIn('## Language readiness',report)
        self.assertIn('## Issue / language work',report)
        self.assertIn('`heartbeat-remnant-2024-summer` | `afr` | 2 / 2',report)
        self.assertIn('`heartbeat-remnant-2024-summer` | `deu` | 2 / 2',report)
        self.assertIn('Corrected candidates',report)

    def test_invalid_queue_is_visible_and_is_not_retried(self):
        queue(self.state,issues='nonexistent')
        self.engine.tick()
        self.assertIn('## Rejected requests',(self.root/'STATUS.md').read_text())
        self.engine.accept_request = MagicMock(side_effect=AssertionError('Retried invalid request'))
        self.engine.tick()
        self.engine.accept_request.assert_not_called()
        self.assertEqual(self.provider.create_calls,0)

    def test_all_twenty_language_codes_and_aliases(self):
        self.assertEqual(len(self.config.languages),20)
        self.assertEqual(self.config.select_languages('af,de,nl,no,zh'),['afr','deu','nld','nob','cmn'])
        self.assertEqual(self.config.select_languages('ar,he,ur'),['ara','heb','urd'])
        self.assertTrue(all(self.config.languages[k]['dir']=='rtl' for k in ('ara','heb','urd')))
        with self.assertRaises(ContractError): self.config.select_languages('zz')
        with self.assertRaises(ContractError): self.config.select_languages('afr,af')

    def test_all_notices_are_separate_escaped_metadata(self):
        for code,language in self.config.languages.items():
            value = notice(language,A,'<model & name>',self.config.runtime['english_route'])
            self.assertIn('data-translation-notice="ai"',value)
            self.assertIn('OpenAI',value)
            self.assertIn('&lt;model &amp; name&gt;',value)
            self.assertNotIn('<model & name>',value)
            self.assertIn(f'/en/articles/{A}/',value)
            self.assertIn(f'lang="{language["tag"]}"',value)

    def test_scripture_numbers_and_localized_digits(self):
        self.assertEqual(reference_numbers('John 3:16–18'),reference_numbers('يوحنا ٣:١٦-١٨'))
        self.assertNotEqual(reference_numbers('John 3:16'),reference_numbers('John 3:17'))
        bad = copy.deepcopy(self.candidate); bad['html'] = bad['html'].replace('3:16','3:17')
        with self.assertRaises(ContractError): validate_translation(self.source,bad)

    def test_html_active_content_attributes_and_links_are_blocked(self):
        for value in (
            self.source['html'].replace('<p>','<p onclick="alert(1)">'),
            self.source['html'].replace('<p>','<script>alert(1)</script><p>'),
            self.source['html'].replace('/images/articles/','https://evil.example/'),
            self.source['html'].replace(A,B),
            self.source['html'].replace('</p>',''),
            self.source['html'].replace('alt="A tree"','alt=""'),
            self.source['html'].replace('<em>grace</em>','grace'),
            self.source['html'].replace('<p>Faith and <em>grace</em>. John 3:16–18.</p>','<p></p>'),
        ):
            with self.subTest(value=value):
                candidate = {**self.candidate,'html':value}
                with self.assertRaises(ContractError): validate_translation(self.source,candidate)

    def test_null_and_empty_metadata_cannot_be_invented(self):
        bad = {**self.candidate,'subtitle':'Invented subtitle'}
        with self.assertRaises(ContractError): validate_translation(self.source,bad)
        self.source['article']['title'] = ''
        bad = {**self.candidate,'title':'Invented title'}
        with self.assertRaises(ContractError): validate_translation(self.source,bad)

    def test_asset_rewrite_never_replaces_matching_prose(self):
        text = f'<article data-article-id="{A}"><p>literal src="/images/articles/{A}-1.jpg"</p><img src="/images/articles/{A}-1.jpg" alt="x"></article>'
        result = rewrite_export_urls(text,'/website/',self.config.runtime['english_route'],A)
        self.assertIn(f'literal src="/images/articles/{A}-1.jpg"',result)
        self.assertIn(f'<img src="/website/images/articles/{A}-1.jpg"',result)
        for bad in ('website','//evil/','/../','/with space/','/site?x/','/site'):
            with self.assertRaises(ContractError): rewrite_export_urls(text,bad,'/en/articles/{article_id}/',A)

    def test_paths_and_json_reject_traversal_symlinks_and_duplicate_keys(self):
        for path in ('../outside','/absolute','a/../b','a\\b','a//b'):
            with self.assertRaises(ContractError): safe_path(self.root,path)
        target = self.root/'link'; target.symlink_to(self.root.parent,target_is_directory=True)
        with self.assertRaises(ContractError): safe_path(self.root,'link/file')
        for text in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
            with self.assertRaises(ContractError): loads(text)

    def test_source_verifies_raw_file_bytes_not_reserialized_objects(self):
        self.assertTrue(self.upstream.contents['index.json'].startswith('{\n'))
        self.engine.discover()
        self.upstream.revision = 'b'*40
        self.upstream.contents['index.json'] += '\n'
        with self.assertRaises(ContractError): self.engine.discover()

    def test_source_html_tampering_is_detected(self):
        self.upstream.revision = 'b'*40
        self.upstream.contents[f'content/articles/{A}.html'] += 'tampered'
        self.engine.discover()
        with self.assertRaises(ContractError): self.upstream.client.snapshot(A)

    def test_ineligible_article_is_not_accepted(self):
        self.upstream.revision = 'b'*40
        self.upstream.articles[0]['rights']['status'] = 'excluded'
        self.upstream.rebuild()
        with self.assertRaises(ContractError): self.engine.discover()

    def test_image_bytes_do_not_trigger_new_translation(self):
        left = self.source['fingerprints']
        right = {**left,'images':[{'sha256':'f'*64}]}
        self.assertEqual(translation_key(left),translation_key(right))

    def test_unknown_issue_selection_is_recorded_without_spending(self):
        queue(self.state,issues='does-not-exist')
        self.engine.tick()
        self.assertEqual(self.provider.create_calls,0)
        self.assertIsNotNone(self.state.read('state/queue-errors/request-1.json'))
        self.assertEqual(self.state.tasks(),[])

    def test_multiple_issue_aliases_are_not_silently_duplicated(self):
        with self.assertRaises(ContractError):
            self.engine.select_issues(ISSUE+',heartbeat-remnant-2024-summer',['afr'],'translate')

    def test_bad_price_configuration_fails_before_requests(self):
        models = copy.deepcopy(self.config.models)
        models['gpt-4.1-mini']['input_batch_usd_per_million'] = -1
        write_json(self.root/'config/models.json',models)
        with self.assertRaises(ContractError): Config(self.root)

    def test_orphaned_content_and_stale_index_are_rejected(self):
        (self.root/'content/unexpected.html').write_text('<p>orphan</p>')
        with self.assertRaises(ContractError): validate_repository(self.config)
        (self.root/'content/unexpected.html').unlink()
        self.state.write('index.json',{'not':'an index'})
        with self.assertRaises(ContractError): validate_repository(self.config)

    def test_partial_notice_removal_is_not_a_human_review(self):
        queue(self.state); drive(self.engine,self.provider)
        pub = self.state.record('afr',A)['published']
        path = self.state.path(pub['html_path'])
        path.write_text(path.read_text().replace('OpenAI','another provider'))
        with self.assertRaises(ContractError): self.engine.tick()

    def test_rate_budget_and_prompts_are_frozen_per_campaign(self):
        queue(self.state); self.engine.tick()
        batch = self.state.batches()[0]
        campaign = self.state.campaigns()[0]
        original_prompt = campaign['prompts']['review']
        (self.root/'prompts/review.txt').write_text('Changed prompt for future campaigns')
        self.provider.complete_all(); self.engine.tick()
        review_batch = next(b for b in self.state.batches() if b['stage']=='review1')
        body = loads(self.state.path(f'state/batches/{review_batch["id"]}/input.jsonl').read_bytes().splitlines()[0])['body']
        self.assertEqual(body['messages'][0]['content'],original_prompt)

    def test_queue_conflict_retry_does_not_duplicate_a_request(self):
        request = queue(self.state)
        calls = []
        def transport(method,url,token,data=None):
            calls.append(method)
            if method=='PUT':
                raise HTTPError(url,422,'already exists',None,None)
            import base64
            return {'content':base64.b64encode(canonical(request)).decode()}
        result = enqueue_github(self.config,request,'trueChristian/berean-translation','test-not-a-token',transport)
        self.assertTrue(result['already_queued'])
        self.assertEqual(calls,['PUT','GET'])

    def test_queue_authentication_failure_is_visible(self):
        request = queue(self.state)
        def transport(method,url,token,data=None):
            raise HTTPError(url,403,'Forbidden',None,None)
        with self.assertRaisesRegex(ContractError,'403'):
            enqueue_github(self.config,request,'trueChristian/berean-translation','fake',transport)


class ProviderContractTests(unittest.TestCase):
    def test_official_sdk_adapter_calls_batch_endpoints(self):
        client = SimpleNamespace(files=MagicMock(),batches=MagicMock())
        client.files.create.return_value = SimpleNamespace(id='file-123')
        client.files.content.return_value = SimpleNamespace(content=b'{}\n')
        client.batches.create.return_value = {'id':'batch-123'}
        client.batches.retrieve.return_value = {'id':'batch-123','status':'completed'}
        client.batches.list.return_value = [{'id':'batch-123','metadata':{'submission_key':'key'}}]
        client.batches.cancel.return_value = {'id':'batch-123','status':'cancelling'}
        provider = OpenAIProvider(client=client)
        self.assertEqual(provider.upload(b'{}\n','input.jsonl'),'file-123')
        self.assertEqual(provider.create('file-123','key','campaign')['id'],'batch-123')
        self.assertEqual(client.batches.create.call_args.kwargs['endpoint'],'/v1/chat/completions')
        self.assertEqual(client.batches.create.call_args.kwargs['completion_window'],'24h')
        self.assertEqual(provider.content('file-123'),b'{}\n')
        self.assertEqual(len(provider.find('key')),1)
        self.assertEqual(provider.retrieve('batch-123')['status'],'completed')
        self.assertEqual(provider.cancel('batch-123')['status'],'cancelling')

    def test_installed_official_sdk_supports_the_required_resources_without_network(self):
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest('Official OpenAI SDK distribution is unavailable in this offline workspace; CI installs the pinned SDK')
        client = OpenAI(api_key='unit-test-not-a-real-key',max_retries=0,base_url='https://api.openai.com/v1')
        try:
            for resource,methods in ((client.batches,('create','retrieve','list','cancel')),
                                     (client.files,('create','content'))):
                for method in methods:
                    self.assertTrue(callable(getattr(resource,method)))
            self.assertEqual(client.max_retries,0)
        finally:
            client.close()


class WorkflowTests(unittest.TestCase):
    def test_workflows_match_the_registry_and_do_not_expose_keys_to_enqueuers(self):
        import yaml
        config = Config(REPO_ROOT)
        for name in ('ai-translate.yml','ai-review.yml'):
            document = yaml.load((REPO_ROOT/'.github/workflows'/name).read_text(),Loader=yaml.BaseLoader)
            inputs = document['on']['workflow_dispatch']['inputs']
            self.assertLessEqual(len(inputs),10)
            self.assertEqual(set(inputs['language']['options']),{'all',*config.languages})
            self.assertEqual(set(inputs['model']['options']),set(config.models))
            self.assertEqual(set(inputs['review_model']['options']),set(config.models))
            self.assertNotIn('concurrency',document)
            self.assertNotIn('OPENAI_API_KEY',json.dumps(document))
            self.assertEqual(inputs['dry_run']['default'],'true')
        worker = yaml.load((REPO_ROOT/'.github/workflows/ai-worker.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(worker['concurrency']['cancel-in-progress'],'false')
        self.assertEqual(worker['permissions']['contents'],'write')
        self.assertIn('head_repository.full_name',worker['jobs']['worker']['if'])
        self.assertNotIn('pull_request_target',worker['on'])

    def test_all_action_references_are_immutable_and_ci_has_no_paid_secret(self):
        import re,yaml
        for path in (REPO_ROOT/'.github/workflows').glob('*.yml'):
            document = yaml.load(path.read_text(),Loader=yaml.BaseLoader)
            for job in document['jobs'].values():
                for step in job.get('steps',[]):
                    if 'uses' in step:
                        self.assertRegex(step['uses'],r'^[\w/-]+@[a-f0-9]{40}$')
        ci = (REPO_ROOT/'.github/workflows/ci.yml').read_text()
        self.assertNotIn('secrets.OPENAI_API_KEY',ci)
        self.assertIn('contents: read',ci)


if __name__ == '__main__': unittest.main()
