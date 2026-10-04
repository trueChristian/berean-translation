"""Offline GetBible contract/quotation tests. No paid APIs or network calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import canonical, digest, json_hash, loads, read_json
from berean_translation.scripture_provider import GetBibleMCP, ScriptureProviderError
from berean_translation.scripture_evidence import (
    APPROVED_EDITIONS, ScriptureAttention, build_evidence, check_selections,
    freeze_scripture_evidence, load_evidence, normalize_scripture_candidate, adopt_scripture_selection_audit,
    policy, validate_scripture_candidate)
from berean_translation.requests import build_request
from berean_translation.state import State
from support import A, setup, queue

ROOT = Path(__file__).resolve().parents[1]
BLOCK = '/article[1]/p[1]'


def fixture(edition):
    return read_json(ROOT/f'tests/fixtures/getbible/get_scripture-{edition}.json')


def source(quote=None, reference='John 4:16', markup=None):
    if quote is None:
        quote = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
    html = markup if markup is not None else f'<p>“{quote}” ({reference}).</p>'
    return {'article':{'id':A,'title':'Teaching','subtitle':None,'section':None,'images':[]},
            'html':f'<article data-article-id="{A}">{html}</article>','revision':'a'*40}


class FakeMCP:
    def __init__(self):
        self.calls=[]
        self.failure=None
    def __call__(self,name,args):
        self.calls.append((name,args))
        if self.failure:
            raise self.failure
        return fixture(args['translation'])


class ScriptureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.policy=policy(ROOT)
        self.fake=FakeMCP()
        self.provider=GetBibleMCP(self.fake)
    def build(self, src=None, tag='de'):
        return build_evidence(src or source(),tag,self.policy,self.provider)
    def candidate(self,bundle,text=None,parts=None):
        verse=bundle['quotes'][0]['target_verses'][0]
        text = verse['text'].strip() if text is None else text
        parts = [{'verse':verse['verse'],'start':0,'end':len(verse['text'])}] if parts is None else parts
        html=f'<article data-article-id="{A}"><p>“{text}” (John 4:16).</p></article>'
        candidate={'html':html,'title':'Lehre','subtitle':None,'section':None}
        claims=[{'quote_id':'q1','block':BLOCK,'start':1,'end':1+len(text),'fragments':[parts]}]
        return candidate,claims
    def test_exact_approved_map_and_rights_provenance(self):
        self.assertEqual({k:v.get('abbreviation') for k,v in self.policy['edition_map']['locales'].items()},APPROVED_EDITIONS)
        self.assertIn('Copyrighted',self.policy['edition_map']['locales']['af']['distribution_license'])
        self.assertEqual(self.policy['edition_map_provenance']['source_commit'],'0b0a8fc52cbbd47bef8e1f84328a825d169d0543')
    def test_source_editions_and_v2_are_explicit(self):
        evidence=self.build()
        self.assertEqual([args['translation'] for _,args in self.fake.calls],['kjv','luther1545'])
        self.assertTrue(all(args['api_version']=='v2' for _,args in self.fake.calls))
        self.assertEqual(evidence['quotes'][0]['reference'],'John 4:16')
        for lookup in evidence['lookups'].values():
            self.assertEqual(lookup['result_sha256'],json_hash(lookup['result']))
    def test_complete_quote_exact_candidate_passes(self):
        bundle=self.build();candidate,claims=self.candidate(bundle)
        self.assertTrue(check_selections(bundle,candidate,claims))
    def test_wrong_target_book_same_numbers_fails(self):
        bundle=self.build();candidate,claims=self.candidate(bundle)
        candidate['html']=candidate['html'].replace('John 4:16','Luke 4:16')
        with self.assertRaisesRegex(ScriptureAttention,'citation_identity_changed'):
            check_selections(bundle,candidate,claims)
    def test_provider_native_book_name_preserves_identity(self):
        bundle=self.build();candidate,claims=self.candidate(bundle)
        candidate['html']=candidate['html'].replace('John 4:16','Johannes 4:16')
        self.assertTrue(check_selections(bundle,candidate,claims))
    def test_target_reference_cannot_change_chapter_or_verse(self):
        bundle=self.build();candidate,claims=self.candidate(bundle)
        for reference in ('John 4:15','John 5:16','John 4:15-16'):
            changed={**candidate,'html':candidate['html'].replace('John 4:16',reference)}
            with self.subTest(reference=reference),self.assertRaisesRegex(ScriptureAttention,'citation_identity_changed'):
                check_selections(bundle,changed,claims)
    def test_generated_scripture_words_fail(self):
        bundle=self.build();candidate,claims=self.candidate(bundle)
        candidate['html']=candidate['html'].replace('Mann','Frau')
        with self.assertRaisesRegex(ScriptureAttention,'quote_words_changed'):
            check_selections(bundle,candidate,claims)
    def test_substring_claim_cannot_hide_expanded_delimited_quote(self):
        bundle=self.build(source('Go, call thy husband'))
        verse=bundle['quotes'][0]['target_verses'][0];full=verse['text'].strip()
        selected='rufe deinen Mann';start=verse['text'].index(selected)
        candidate,claims=self.candidate(bundle,text=full,parts=[{'verse':16,'start':start,'end':start+len(selected)}])
        claims[0]['start']=1+full.index(selected);claims[0]['end']=claims[0]['start']+len(selected)
        with self.assertRaisesRegex(ScriptureAttention,'quote_scope_changed'):
            check_selections(bundle,candidate,claims)
    def test_partial_quote_uses_only_retrieved_substring(self):
        bundle=self.build(source('Go, call thy husband'))
        self.assertEqual(bundle['quotes'][0]['alignment']['kind'],'partial')
        verse=bundle['quotes'][0]['target_verses'][0]
        selected='Gehe hin, rufe deinen Mann';start=verse['text'].index(selected)
        candidate,claims=self.candidate(bundle,text=selected,parts=[{'verse':16,'start':start,'end':start+len(selected)}])
        self.assertTrue(check_selections(bundle,candidate,claims))
    def test_complete_quote_cannot_be_shortened(self):
        bundle=self.build();verse=bundle['quotes'][0]['target_verses'][0]
        selected='Gehe hin, rufe deinen Mann';start=verse['text'].index(selected)
        candidate,claims=self.candidate(bundle,text=selected,parts=[{'verse':16,'start':start,'end':start+len(selected)}])
        with self.assertRaisesRegex(ScriptureAttention,'incomplete_verse_selection'):
            check_selections(bundle,candidate,claims)
    def test_ellipsis_requires_ordered_evidence_and_preserves_mark(self):
        bundle=self.build(source('Jesus saith unto her … and come hither.'))
        verse=bundle['quotes'][0]['target_verses'][0]
        first='JEsus spricht zu ihr';last='und komm her!'
        text=first+' … '+last
        candidate,claims=self.candidate(bundle,text=text)
        claims[0]['fragments']=[[{'verse':16,'start':0,'end':len(first)}],
            [{'verse':16,'start':verse['text'].index(last),'end':verse['text'].index(last)+len(last)}]]
        self.assertTrue(check_selections(bundle,candidate,claims))
        claims[0]['fragments'].reverse()
        with self.assertRaisesRegex(ScriptureAttention,'selection_order'):
            check_selections(bundle,candidate,claims)
    def test_john4_printed16_actually15_16_needs_attention(self):
        verses=fixture('kjv')['structuredContent']['data']['verses']
        with self.assertRaisesRegex(ScriptureAttention,'printed_reference_mismatch'):
            self.build(source(' '.join(v['text'] for v in verses)))
    def test_wrong_citation_is_never_rewritten(self):
        src=source('The woman saith unto him, Sir, give me this water')
        before=canonical(src)
        with self.assertRaises(ScriptureAttention):self.build(src)
        self.assertEqual(before,canonical(src))
    def test_unavailable_editions_never_fall_back(self):
        for tag in ('bn','hi','id','sw','ur'):
            with self.subTest(tag=tag),self.assertRaisesRegex(ScriptureAttention,'missing_edition'):
                self.build(tag=tag)
        self.assertEqual(self.fake.calls,[])
    def test_unknown_locale_fails_closed(self):
        with self.assertRaisesRegex(ScriptureAttention,'missing_edition'):self.build(tag='xx')
    def test_offline_fetch_failure_no_fallback(self):
        self.fake.failure=TimeoutError('offline')
        with self.assertRaisesRegex(ScriptureAttention,'fetch_failed'):self.build()
        self.assertEqual(len(self.fake.calls),1)
    def test_ordinary_prose_and_allusion_not_replaced(self):
        bundle=self.build(source(markup='<p>Consider the discussion in John 4:16.</p>'))
        self.assertEqual(bundle['quotes'],[])
        self.assertEqual(bundle['references'][0]['classification'],'reference_only')
        self.assertEqual([args['translation'] for _,args in self.fake.calls],['kjv'])
    def test_ordinary_quote_without_reference_is_not_bible_text(self):
        bundle=self.build(source(markup='<p>The gardener said, “Plant a tree.”</p>'))
        self.assertEqual(bundle['quotes'],[]);self.assertEqual(self.fake.calls,[])
    def test_unmarked_complete_verse_identified(self):
        kjv=fixture('kjv')['structuredContent']['data']['verses'][1]['text']
        bundle=self.build(source(markup=f'<p>{kjv} (John 4:16).</p>'))
        self.assertEqual(bundle['quotes'][0]['alignment']['kind'],'complete')
    def test_ambiguous_partial_fails_closed(self):
        with self.assertRaisesRegex(ScriptureAttention,'ambiguous_source_quote'):
            self.build(source('Jesus asked her to return'))
    def test_multiple_reference_associations_require_attention(self):
        with self.assertRaisesRegex(ScriptureAttention,'ambiguous_quote_reference'):
            self.build(source(reference='John 4:16; John 4:15'))
    def test_synodal_psalm_versification_never_guessed(self):
        with self.assertRaisesRegex(ScriptureAttention,'unverified_versification'):
            self.build(source('Some trust in chariots, and some in horses','Psalm 20:7'),tag='ru')
        self.assertEqual(self.fake.calls,[])
    def test_full_evidence_limit_never_truncates_article(self):
        self.policy['max_evidence_bytes']=500
        with self.assertRaisesRegex(ScriptureAttention,'evidence_size_limit'):self.build()
    def test_provider_rejects_wrong_edition_and_no_hash_check(self):
        for mutation in ('edition','hash'):
            result=fixture('kjv')
            if mutation=='edition':result['structuredContent']['scope']['translation']='sse'
            else:result['structuredContent']['consistency_checked']=False
            provider=GetBibleMCP(lambda *_:result)
            with self.subTest(mutation=mutation),self.assertRaises(ScriptureProviderError):
                provider.chapter('kjv',43,4)
    def test_mcp_error_and_missing_structured_data_rejected(self):
        for result in ({'isError':True},{'content':[]}):
            with self.subTest(result=result),self.assertRaises(ScriptureProviderError):
                GetBibleMCP(lambda *_:result).chapter('kjv',43,4)
    def test_query_explicit_identity_no_invented_hash(self):
        result={'isError':False,'structuredContent':{'translation':'kjv','references':'John 4:16',
            'data':{},'source':{'api_version':'v2','status_code':200,'url':'https://query.getbible.net/v2/kjv/John%204%3A16'}}}
        evidence=GetBibleMCP(lambda *_:result).query('kjv','John 4:16')
        self.assertNotIn('hash',evidence['result'])
    def test_explicit_arguments_required(self):
        for args in ({'translation':'kjv'},{'api_version':'v2'},{'translation':'kjv','api_version':'v3'}):
            with self.assertRaises(ScriptureProviderError):self.provider.call('get_scripture',args)


class ScriptureRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config,self.state,self.upstream,self.paid,_,self.engine=setup(self.root)
        self.engine.discover()
        self.campaign=self.engine.accept_request(queue(self.state,languages='deu'))
        self.task=self.state.read(f'state/tasks/{self.campaign["tasks"][0]}/task.json')
        src=source();sha=json_hash(src)
        self.state.write(f'state/sources/{sha}.json',src)
        self.task['source_snapshot']=f'state/sources/{sha}.json'
        self.policy=policy(ROOT)
        self.task.update(freeze_scripture_evidence(self.engine,src,'deu',frozen_policy=self.policy,provider=GetBibleMCP(FakeMCP())))
        self.campaign['scripture_quotes']=self.policy
        self.state.save_campaign(self.campaign);self.state.save_task(self.task)
        evidence=load_evidence(self.state,self.task)
        helper=ScriptureEvidenceTests();self.candidate,self.selections=helper.candidate(evidence)
    def test_new_contract_in_every_stage_and_bound_includes_all_evidence(self):
        result={**self.candidate,'scripture_selections':self.selections}
        normalized=normalize_scripture_candidate(self.state,self.task,result)
        self.state.save_candidate(self.task,normalized)
        for stage in ('translate','correct','review1','review2'):
            with self.subTest(stage=stage):
                task={**self.task,'stage':stage}
                line,_,bound=build_request(self.config,self.state,task)
                payload=loads(line['body']['messages'][1]['content'])
                self.assertEqual(payload['scripture_evidence'],load_evidence(self.state,task))
                self.assertEqual(payload['source']['html'],source()['html'])
                self.assertEqual(bound,len(canonical(line['body']))+4096)
                self.assertNotIn('tools',line['body'])
                self.assertEqual(line['url'],'/v1/chat/completions')
                if stage.startswith('review'):self.assertIn('scripture_selection_audit',payload)
        self.assertEqual(self.paid.create_calls,0);self.assertEqual(self.paid.upload_calls,0)
    def test_candidate_audit_tampering_blocks_publication(self):
        normalize_scripture_candidate(self.state,self.task,{**self.candidate,'scripture_selections':self.selections})
        changed={**self.candidate,'title':'Changed'}
        with self.assertRaisesRegex(ScriptureAttention,'selection_changed'):
            validate_scripture_candidate(self.state,self.task,changed)
    def test_missing_claims_cannot_pass(self):
        with self.assertRaisesRegex(ScriptureAttention,'selection_shape'):
            normalize_scripture_candidate(self.state,self.task,self.candidate)
    def test_evidence_tampering_blocks_request(self):
        value=self.state.read(self.task['scripture_evidence_path']);value['edition']['abbreviation']='kjv'
        self.state.write(self.task['scripture_evidence_path'],value)
        with self.assertRaisesRegex(ScriptureAttention,'evidence_changed'):
            build_request(self.config,self.state,self.task)
    def test_expired_evidence_does_not_refresh_frozen_history(self):
        value=self.state.read(self.task['scripture_evidence_path'])
        for lookup in value['lookups'].values():lookup['result']['cache']['expires_at']='2000-01-01T00:00:00Z'
        sha=json_hash(value);path=f'state/scripture/{sha}.json';self.state.write(path,value)
        self.task.update(scripture_evidence_path=path,scripture_evidence_sha256=sha)
        with self.assertRaisesRegex(ScriptureAttention,'evidence_expired'):
            build_request(self.config,self.state,self.task)
        self.assertEqual(self.state.read(path),value)
    def test_human_controlled_article_never_fetches(self):
        record=self.state.record('deu',A);record['published']={'human_reviewed':True};self.state.save_record(record)
        fake=FakeMCP()
        with self.assertRaisesRegex(ScriptureAttention,'human_controlled'):
            freeze_scripture_evidence(self.engine,source(),'deu',frozen_policy=self.policy,provider=GetBibleMCP(fake))
        self.assertEqual(fake.calls,[])
    def test_historical_request_contract_unchanged(self):
        del self.campaign['scripture_quotes'];self.state.save_campaign(self.campaign)
        line,_,_=build_request(self.config,self.state,self.task)
        payload=loads(line['body']['messages'][1]['content'])
        self.assertNotIn('scripture_evidence',payload)
        self.assertEqual(line['body']['messages'][0]['content'],self.campaign['prompts']['translation'])
        self.assertEqual(set(line['body']['response_format']['json_schema']['schema']['properties']),{'html','title','subtitle','section'})

    def test_planning_history_can_read_expired_evidence_without_a_fake_audit(self):
        value=self.state.read(self.task['scripture_evidence_path'])
        for lookup in value['lookups'].values():lookup['result']['cache']['expires_at']='2000-01-01T00:00:00Z'
        sha=json_hash(value);path=f'state/scripture/{sha}.json';self.state.write(path,value)
        self.task.update(scripture_evidence_path=path,scripture_evidence_sha256=sha,stage='review1')
        self.state.planning=True
        line,_,_=build_request(self.config,self.state,self.task)
        payload=loads(line['body']['messages'][1]['content'])
        self.assertIsNone(payload['scripture_selection_audit'])
        self.assertIsNone(self.state.read(f'state/tasks/{self.task["id"]}/scripture-selections.json'))
    def test_prior_valid_audit_can_be_adopted_without_inventing_selections(self):
        normalize_scripture_candidate(self.state,self.task,{**self.candidate,'scripture_selections':self.selections})
        self.state.save_candidate(self.task,self.candidate)
        next_task={**self.task,'id':self.task['id']+'-next'}
        self.state.save_candidate(next_task,self.candidate)
        self.assertTrue(adopt_scripture_selection_audit(self.state,next_task,self.task))
        validate_scripture_candidate(self.state,next_task,self.candidate)
        self.assertEqual(self.state.read(f'state/tasks/{next_task["id"]}/scripture-selections.json')['selections'],self.selections)
    def test_missing_prior_claims_require_repair(self):
        self.state.save_candidate(self.task,self.candidate)
        self.assertFalse(adopt_scripture_selection_audit(self.state,self.task))
        self.assertIsNone(self.state.read(f'state/tasks/{self.task["id"]}/scripture-selections.json'))
    def test_no_detected_quotes_can_adopt_a_checked_empty_audit(self):
        src=source(markup='<p>Ordinary teaching.</p>');sha=json_hash(src)
        self.state.write(f'state/sources/{sha}.json',src);self.task['source_snapshot']=f'state/sources/{sha}.json'
        self.task.update(freeze_scripture_evidence(self.engine,src,'deu',frozen_policy=self.policy,provider=GetBibleMCP(FakeMCP())))
        candidate={**self.candidate,'html':src['html']};self.state.save_candidate(self.task,candidate)
        self.assertTrue(adopt_scripture_selection_audit(self.state,self.task))
        validate_scripture_candidate(self.state,self.task,candidate)
    def test_selection_audit_byte_limit_is_enforced(self):
        self.campaign['scripture_quotes']['max_selection_audit_bytes']=1
        self.state.save_campaign(self.campaign)
        with self.assertRaisesRegex(ScriptureAttention,'selection_size_limit'):
            normalize_scripture_candidate(self.state,self.task,{**self.candidate,'scripture_selections':self.selections})
