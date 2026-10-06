"""Offline metadata-only repair, with immutable production-artifact provenance."""
import copy
import unittest
from pathlib import Path

from berean_translation.common import ContractError, canonical, json_hash, loads, read_json
from berean_translation.cycle_budget import enforce_candidate_bound
from berean_translation.html import validate_translation
from berean_translation.requests import build_request
from berean_translation.scripture_evidence import (
    ScriptureAttention, adopt_scripture_selection_audit, check_selections,
    freeze_scripture_evidence, frozen_policy, load_evidence, normalize_scripture_candidate,
    repair_complete_selections, validate_scripture_candidate)
from berean_translation.stage_budget import enforce
import test_scripture_evidence as fixtures


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = read_json(ROOT/'tests/fixtures/scripture-selection-repair.json')


class ReadOnlyArtifactState:
    """Only a private in-memory overlay is writable, including hypothetical opt-in."""
    def __init__(self):
        self.overlay = {}

    def read(self, path, default=None):
        return copy.deepcopy(self.overlay.get(path,read_json(ROOT/path,default)))

    def write(self, path, value):
        self.overlay[path] = copy.deepcopy(value)

    def source(self, task):
        return self.read(task['source_snapshot'])

    def candidate(self, task):
        return self.read(f'state/tasks/{task["id"]}/candidate.json')


def artifact(case, *, opt_in=True):
    state = ReadOnlyArtifactState()
    task = state.read(case['task_path'])
    result = state.read(case['result_path'])['result']
    if opt_in:
        path = f'state/campaigns/{task["campaign"]}.json'
        campaign = state.read(path)
        campaign['scripture_quotes']['selection_normalization_version'] = '1'
        state.write(path,campaign)
    return state,task,result,load_evidence(state,task)


class ArtifactNormalizationTests(unittest.TestCase):
    def test_full_immutable_artifact_provenance(self):
        for case in MANIFEST['cases']:
            with self.subTest(language=case['language']):
                self.assertEqual(json_hash(read_json(ROOT/case['result_path'])),case['result_sha256'])
                self.assertEqual(json_hash(read_json(ROOT/case['evidence_path'])),case['evidence_sha256'])
                self.assertEqual(json_hash(read_json(ROOT/case['source_path'])),case['source_sha256'])
                task = read_json(ROOT/case['task_path'])
                self.assertEqual(task['source_snapshot'],case['source_path'])
                self.assertEqual(task['scripture_evidence_path'],case['evidence_path'])

    def test_hebrew_korean_norwegian_complete_runtime_gate_replay(self):
        for case in MANIFEST['cases']:
            if not case['recoverable_metadata']:
                continue
            with self.subTest(language=case['language']):
                state,task,result,evidence = artifact(case)
                original = canonical(result)
                candidate = normalize_scripture_candidate(state,task,result)
                self.assertEqual(candidate,{k:result[k] for k in ('html','title','subtitle','section')})
                self.assertEqual(canonical(result),original)
                enforce(task,candidate)
                enforce_candidate_bound(candidate,task['cycle_budget']['max_candidate_bytes'])
                validate_translation(state.source(task),candidate,language=task['language'])
                validate_scripture_candidate(state,task,candidate)
                audit = state.read(f'state/tasks/{task["id"]}/scripture-selections.json')
                self.assertEqual(audit['normalization']['original_selections'],result['scripture_selections'])
                self.assertEqual(audit['normalization']['raw_result_sha256'],json_hash(result))
                self.assertTrue(audit['normalization']['independent_review_required'])
                self.assertTrue(check_selections(evidence,candidate,audit['selections']))
                # No acceptance, historical task rewrite, source update, or
                # provider result change can occur through this replay.
                self.assertEqual(set(state.overlay),{
                    f'state/campaigns/{task["campaign"]}.json',
                    f'state/tasks/{task["id"]}/scripture-selections.json'})
                self.assertEqual(state.read(case['task_path']),task)
                self.assertEqual(state.read(case['result_path'])['result'],result)

    def test_historical_campaigns_still_hold_the_original_results(self):
        for case in MANIFEST['cases']:
            state,task,result,_ = artifact(case,opt_in=False)
            with self.subTest(language=case['language']),self.assertRaises(ScriptureAttention):
                normalize_scripture_candidate(state,task,result)
            self.assertEqual(state.overlay,{})

    def test_french_wrong_verse_range_remains_blocked(self):
        case = next(c for c in MANIFEST['cases'] if c['language']=='fra')
        state,task,result,evidence = artifact(case)
        before = canonical(result)
        with self.assertRaisesRegex(ScriptureAttention,'selection_repair_identity'):
            normalize_scripture_candidate(state,task,result)
        self.assertEqual(canonical(result),before)
        self.assertNotIn(f'state/tasks/{task["id"]}/scripture-selections.json',state.overlay)
        # Even fabricating the frozen verse IDs cannot hide the real 3–9 text.
        for part,verse in zip(result['scripture_selections'][0]['fragments'][0],evidence['quotes'][0]['target_verses']):
            part.update(verse=verse['verse'],start=0,end=len(verse['text']))
        with self.assertRaisesRegex(ScriptureAttention,'quote_words_changed'):
            normalize_scripture_candidate(state,task,result)

    def test_french_guillemet_padding_is_only_outer_scope_whitespace(self):
        case = next(c for c in MANIFEST['cases'] if c['language']=='fra')
        _,_,result,evidence = artifact(case)
        quote = evidence['quotes'][2]
        evidence['quotes'] = [quote]
        evidence['references'] = [r for r in evidence['references'] if r['block']==quote['block']]
        claims = [result['scripture_selections'][2]]
        self.assertTrue(check_selections(evidence,result,claims))
        expected = ' '.join(v['text'].strip() for v in quote['target_verses'])
        self.assertIn('« '+expected+' »',result['html'])
        for text in ('ADDED ', '\u200b', '\u202e'):
            changed = copy.deepcopy(result)
            changed['html'] = changed['html'].replace(expected,text+expected)
            with self.subTest(text=text),self.assertRaises(ScriptureAttention):
                repair_complete_selections(evidence,changed,claims)

    def test_real_quote_mutations_cannot_be_repaired(self):
        case = next(c for c in MANIFEST['cases'] if c['language']=='nob')
        _,_,result,evidence = artifact(case)
        quote = evidence['quotes'][0]
        expected = ' '.join(v['text'].strip() for v in quote['target_verses'])
        verse = quote['target_verses'][3]['text'].strip()
        replacements = {
            'omitted verse':expected.replace(verse+' ',''),
            'omitted word':expected.replace('skaper ',''),
            'changed punctuation':expected.replace('ungdoms dager,','ungdoms dager;'),
            'internal whitespace':expected.replace('din skaper','din  skaper'),
            'changed unicode':expected.replace('ånden','a\u030anden'),
            'expanded scope':expected+' ADDED',
            'duplicate quote':expected+'» «'+expected,
            'omitted quote':'',
            'reversed verses':' '.join(v['text'].strip() for v in reversed(quote['target_verses'])),
        }
        for label,replacement in replacements.items():
            changed = copy.deepcopy(result)
            changed['html'] = changed['html'].replace(expected,replacement)
            with self.subTest(mutation=label),self.assertRaises(ScriptureAttention):
                repair_complete_selections(evidence,changed,changed['scripture_selections'])

    def test_wrong_or_extra_citation_cannot_be_repaired(self):
        case = next(c for c in MANIFEST['cases'] if c['language']=='nob')
        _,_,result,evidence = artifact(case)
        original = 'Forkynnerens bok 12:1-7'
        for replacement in ('John 12:1-7','Forkynnerens bok 11:1-7',
                            'Forkynnerens bok 12:1-6',original+'; John 3:16',original+'; '+original):
            changed = copy.deepcopy(result)
            changed['html'] = changed['html'].replace(original,replacement)
            with self.subTest(reference=replacement),self.assertRaisesRegex(ScriptureAttention,'citation_identity_changed'):
                repair_complete_selections(evidence,changed,changed['scripture_selections'])

    def test_invalid_claims_cannot_be_repaired(self):
        case = next(c for c in MANIFEST['cases'] if c['language']=='kor')
        _,_,result,evidence = artifact(case)
        mutations = {
            'missing quote':lambda s:s.pop(),
            'unknown quote':lambda s:s[0].update(quote_id='q4'),
            'duplicate quote':lambda s:s[0].update(quote_id='q2'),
            'wrong block':lambda s:s[0].update(block='/article[1]/p[11]'),
            'missing verse':lambda s:s[0]['fragments'][0].pop(),
            'duplicate verse':lambda s:s[0]['fragments'][0][0].update(verse=2),
            'reordered verses':lambda s:s[0]['fragments'][0].reverse(),
            'out of range verse':lambda s:s[0]['fragments'][0][0].update(verse=999),
            'fabricated fragment':lambda s:s[0]['fragments'].append(s[0]['fragments'][0]),
            'negative offset':lambda s:s[0].update(start=-1),
            'unbounded offset':lambda s:s[0].update(end=10**9),
            'noninteger offset':lambda s:s[0].update(start=True),
            'unbounded verse offset':lambda s:s[0]['fragments'][0][0].update(end=10**9),
            'partial verse start':lambda s:s[0]['fragments'][0][0].update(start=1),
            'noninteger verse':lambda s:s[0]['fragments'][0][0].update(verse=True),
        }
        for label,mutate in mutations.items():
            claims = copy.deepcopy(result['scripture_selections'])
            mutate(claims)
            with self.subTest(mutation=label),self.assertRaises(ScriptureAttention):
                repair_complete_selections(evidence,result,claims)


class SelectionNormalizationTests(unittest.TestCase):
    setUp = fixtures.ScriptureRequestTests.setUp

    def result(self):
        result = {**copy.deepcopy(self.candidate),'scripture_selections':copy.deepcopy(self.selections)}
        result['scripture_selections'][0]['start'] = 0
        return result

    def test_new_frozen_policy_and_unknown_normalizer_version(self):
        self.assertEqual(self.policy['selection_normalization_version'],'1')
        self.campaign['scripture_quotes']['selection_normalization_version'] = '999'
        with self.assertRaisesRegex(ContractError,'normalization contract'):
            frozen_policy(self.campaign)

    def test_normalization_preserves_raw_result_and_is_revalidated(self):
        result = self.result()
        before = canonical(result)
        self.assertEqual(normalize_scripture_candidate(self.state,self.task,result),self.candidate)
        self.assertEqual(canonical(result),before)
        validate_scripture_candidate(self.state,self.task,self.candidate)
        path = f'state/tasks/{self.task["id"]}/scripture-selections.json'
        audit = self.state.read(path)
        for field,value in [('raw_result_sha256','0'*64),('original_failure','invented'),
                            ('independent_review_required',False),('version','999')]:
            changed = copy.deepcopy(audit)
            changed['normalization'][field] = value
            self.state.write(path,changed)
            with self.subTest(field=field),self.assertRaisesRegex(ScriptureAttention,'selection_changed'):
                validate_scripture_candidate(self.state,self.task,self.candidate)

    def test_normalized_selection_audit_limit_includes_original_claims(self):
        result = self.result()
        normalize_scripture_candidate(self.state,self.task,result)
        path = f'state/tasks/{self.task["id"]}/scripture-selections.json'
        size = len(canonical(self.state.read(path)))
        self.campaign['scripture_quotes']['max_selection_audit_bytes'] = size-1
        self.state.save_campaign(self.campaign)
        with self.assertRaisesRegex(ScriptureAttention,'selection_size_limit'):
            normalize_scripture_candidate(self.state,self.task,result)

    def test_normalization_provenance_cannot_be_removed_or_relabelled(self):
        result = self.result()
        normalize_scripture_candidate(self.state,self.task,result)
        path = f'state/tasks/{self.task["id"]}/scripture-selections.json'
        original = self.state.read(path)
        for field in ('normalization','input_result_sha256'):
            changed = copy.deepcopy(original)
            del changed[field]
            self.state.write(path,changed)
            with self.subTest(field=field),self.assertRaisesRegex(ScriptureAttention,'selection_changed'):
                validate_scripture_candidate(self.state,self.task,self.candidate)
        self.state.write(path,original)
        # An internally consistent replacement must still agree with the raw
        # result that receive() archived before normalization.
        self.state.write(f'state/tasks/{self.task["id"]}/results/translate.json',{'result':{
            **result,'scripture_selections':self.selections}})
        with self.assertRaisesRegex(ScriptureAttention,'archived provider result'):
            validate_scripture_candidate(self.state,self.task,self.candidate)

    def test_adoption_preserves_original_normalization_provenance(self):
        result = self.result()
        normalize_scripture_candidate(self.state,self.task,result)
        self.state.save_candidate(self.task,self.candidate)
        next_task = {**self.task,'id':self.task['id']+'-next'}
        self.state.save_candidate(next_task,self.candidate)
        self.assertTrue(adopt_scripture_selection_audit(self.state,next_task,self.task))
        validate_scripture_candidate(self.state,next_task,self.candidate)
        previous = self.state.read(f'state/tasks/{self.task["id"]}/scripture-selections.json')
        adopted = self.state.read(f'state/tasks/{next_task["id"]}/scripture-selections.json')
        self.assertEqual(previous['normalization'],adopted['normalization'])

    def test_empty_quote_legacy_adoption_hashes_application_assembled_input(self):
        src = fixtures.source(markup='<p>Ordinary teaching.</p>')
        sha = json_hash(src)
        self.state.write(f'state/sources/{sha}.json',src)
        self.task['source_snapshot'] = f'state/sources/{sha}.json'
        self.task.update(freeze_scripture_evidence(
            self.engine,src,'deu',frozen_policy=self.policy,
            provider=fixtures.GetBibleMCP(fixtures.FakeMCP())))
        # Legacy candidates have only four fields. There is no original
        # five-field provider response or prior Scripture audit to adopt.
        legacy_candidate = {**self.candidate,'html':src['html']}
        self.state.save_candidate(self.task,legacy_candidate)
        archive_path = f'state/tasks/{self.task["id"]}/results/translate.json'
        self.assertIsNone(self.state.read(archive_path))
        self.assertTrue(adopt_scripture_selection_audit(self.state,self.task))
        audit = self.state.read(f'state/tasks/{self.task["id"]}/scripture-selections.json')
        assembled_input = {**legacy_candidate,'scripture_selections':[]}
        self.assertEqual(audit['input_result_sha256'],json_hash(assembled_input))
        self.assertNotEqual(audit['input_result_sha256'],json_hash(legacy_candidate))
        self.assertNotIn('provider_result_sha256',audit)
        self.assertNotIn('normalization',audit)
        self.assertIsNone(self.state.read(archive_path))
        self.assertEqual(self.state.candidate(self.task),legacy_candidate)
        validate_scripture_candidate(self.state,self.task,legacy_candidate)

    def test_normalized_result_still_requires_independent_review(self):
        result = self.result()
        row = {'custom_id':self.task['id']+':translate','error':None,
               'response':{'status_code':200,'request_id':'req-offline','body':{
                   'model':self.task['model'],'id':'chat-offline',
                   'choices':[{'finish_reason':'stop','message':{'content':canonical(result).decode(),'refusal':None}}],
                   'usage':{'prompt_tokens':200,'completion_tokens':100}}}}
        self.engine.receive(self.task,row)
        saved = self.state.read(f'state/tasks/{self.task["id"]}/task.json')
        self.assertEqual((saved['stage'],saved['status']),('review1','queued'))
        self.assertEqual(self.state.read(f'state/tasks/{self.task["id"]}/results/translate.json')['result'],result)
        self.assertIsNone(self.state.record(saved['language'],saved['article_id'])['published'])
        line,_,_ = build_request(self.config,self.state,saved)
        payload = loads(line['body']['messages'][1]['content'])
        self.assertEqual(payload['scripture_selection_audit']['normalization']['original_selections'],result['scripture_selections'])
        self.assertEqual((self.paid.create_calls,self.paid.upload_calls),(0,0))

    def test_frozen_generation_request_bytes_are_unchanged(self):
        with_policy,_,_ = build_request(self.config,self.state,self.task)
        del self.campaign['scripture_quotes']['selection_normalization_version']
        self.state.save_campaign(self.campaign)
        historical,_,_ = build_request(self.config,self.state,self.task)
        self.assertEqual(canonical(historical),canonical(with_policy))
        with self.assertRaisesRegex(ScriptureAttention,'quote_scope_changed'):
            normalize_scripture_candidate(self.state,self.task,self.result())

    def test_partial_and_ellipsis_offset_failures_stay_held(self):
        helper = fixtures.ScriptureEvidenceTests()
        helper.setUp()
        for src in (fixtures.source('Go, call thy husband'),fixtures.source('Jesus saith unto her … and come hither.')):
            evidence = helper.build(src)
            candidate,claims = helper.candidate(evidence)
            with self.subTest(source=src['html']),self.assertRaisesRegex(ScriptureAttention,'selection_repair_scope'):
                repair_complete_selections(evidence,candidate,claims)

    def test_repair_preserves_authorized_reference_only_citations_in_same_block(self):
        evidence = load_evidence(self.state,self.task)
        reference = copy.deepcopy(evidence['references'][0])
        reference.update(identity='John 3:16',printed_text='John 3:16',classification='reference_only')
        evidence['references'].append(reference)
        result = self.result()
        result['html'] = result['html'].replace('</p>',' See John 3:16.</p>')
        repaired = repair_complete_selections(evidence,result,result['scripture_selections'])
        self.assertTrue(check_selections(evidence,result,repaired,require_exact_citations=True))
        for replacement in ('',' See John 3:16; John 3:16.',' See Luke 3:16.',
                            ' See John 4:16.',' See John 3:17.'):
            changed = {**result,'html':result['html'].replace(' See John 3:16.',replacement)}
            with self.subTest(reference=replacement),self.assertRaisesRegex(ScriptureAttention,'citation_identity_changed'):
                repair_complete_selections(evidence,changed,changed['scripture_selections'])
        # The original subset gate retains its historical behavior; only the
        # new repair proof requires every recorded same-block reference.
        historical = {**result,'html':result['html'].replace(' See John 3:16.','')}
        self.assertTrue(check_selections(evidence,historical,repaired))
