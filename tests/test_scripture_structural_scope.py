"""Offline v2 scope proofs against complete, immutable production artifacts."""
import copy
import re
import unittest
from pathlib import Path

from berean_translation.common import ContractError, canonical, digest, json_hash, loads, read_json
from berean_translation.config import Config
from berean_translation.cycle_budget import enforce_candidate_bound
from berean_translation.html import Fragment, validate_translation
from berean_translation.requests import build_request
from berean_translation.scripture_evidence import (
    ScriptureAttention, _selection_text, check_selections, load_evidence,
    adopt_scripture_selection_audit, normalize_scripture_candidate,
    repair_complete_selections, validate_scripture_candidate)
from berean_translation.scripture_scope import MAX_SCOPE_HTML_BYTES
from berean_translation.stage_budget import enforce
from berean_translation.validation import validate_repository
from support import A, drive
import test_autonomous_scripture as autonomous_fixtures
import test_scripture_evidence as scripture_fixtures
from test_scripture_selection_normalization import ReadOnlyArtifactState


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = read_json(ROOT/'tests/fixtures/scripture-structural-scope.json')


def artifact(case, version='2'):
    state = ReadOnlyArtifactState()
    task = state.read(f'state/tasks/{case["task_id"]}/task.json')
    result = state.read(f'state/tasks/{task["id"]}/results/correct.json')['result']
    if version is not None:
        path = f'state/campaigns/{task["campaign"]}.json'
        campaign = state.read(path)
        campaign['scripture_quotes']['selection_normalization_version'] = version
        state.write(path,campaign)
    return state,task,result,load_evidence(state,task)


def case_for(task_prefix):
    return next(case for case in MANIFEST['cases'] if case['task_id'].startswith(task_prefix))


def v2_repair(state, task, result, evidence):
    return repair_complete_selections(evidence,result,result['scripture_selections'],
                                      normalization_version='2',source=state.source(task))


class StructuralArtifactTests(unittest.TestCase):
    def test_full_production_artifacts_and_raw_provider_body_are_pinned(self):
        self.assertEqual(MANIFEST['translation_pin'],'a282270816511ceab7890a1a642dfdf132c1e387')
        self.assertEqual(len(MANIFEST['cases']),25)
        for case in MANIFEST['cases']:
            with self.subTest(task=case['task_id']):
                for path,hashes in case['artifacts'].items():
                    raw = (ROOT/path).read_bytes()
                    self.assertEqual(digest(raw),hashes['blob_sha256'])
                    self.assertEqual(json_hash(loads(raw)),hashes['canonical_sha256'])
                state,task,result,evidence = artifact(case)
                archived = state.read(f'state/tasks/{task["id"]}/attempts/correct.json')['response']
                self.assertEqual(loads(archived['content']),result)
                self.assertEqual(digest(archived['content']),case['attempt']['content_sha256'])
                self.assertEqual(len(archived['content'].encode('utf-8')),case['attempt']['content_bytes'])
                self.assertEqual(archived['finish_reason'],'stop')
                self.assertFalse(archived['content_evidence_truncated'])
                self.assertEqual(task['translation_attempts'],2)
                self.assertEqual(task['review_attempts'],0)
                self.assertEqual(evidence['source_sha256'],json_hash(state.source(task)))

    def test_all_25_full_runtime_gate_replays_remain_in_memory(self):
        passed = 0
        for case in MANIFEST['cases']:
            with self.subTest(task=case['task_id'],language=case['language']):
                state,task,result,evidence = artifact(case)
                original = canonical(result)
                candidate = {key:result[key] for key in ('html','title','subtitle','section')}
                if not case['expected_v2_deterministic_pass']:
                    with self.assertRaises(ContractError):
                        normalized = normalize_scripture_candidate(state,task,result)
                        validate_translation(state.source(task),normalized,language=task['language'])
                    self.assertEqual(canonical(result),original)
                    continue
                self.assertEqual(normalize_scripture_candidate(state,task,result),candidate)
                enforce(task,candidate)
                if 'cycle_budget' in task:
                    enforce_candidate_bound(candidate,task['cycle_budget']['max_candidate_bytes'])
                validate_translation(state.source(task),candidate,language=task['language'])
                validate_scripture_candidate(state,task,candidate)
                audit_path = f'state/tasks/{task["id"]}/scripture-selections.json'
                audit = state.read(audit_path)
                self.assertEqual(audit['input_result_sha256'],json_hash(result))
                if 'normalization' in audit:
                    self.assertEqual(audit['normalization']['version'],'2')
                    self.assertEqual(audit['normalization']['original_selections'],result['scripture_selections'])
                    self.assertEqual(audit['normalization']['raw_result_sha256'],json_hash(result))
                    self.assertTrue(audit['normalization']['independent_review_required'])
                self.assertTrue(check_selections(evidence,candidate,audit['selections'],
                                                normalization_version='2',source=state.source(task)))
                line,_,_ = build_request(Config(ROOT),state,{**task,'stage':'review2'})
                payload = loads(line['body']['messages'][1]['content'])
                self.assertEqual(payload['scripture_selection_audit'],audit)
                self.assertEqual(payload['scripture_evidence'],evidence)
                self.assertEqual(payload['source']['html'],state.source(task)['html'])
                self.assertEqual(canonical(result),original)
                self.assertEqual(state.read(f'state/tasks/{task["id"]}/results/correct.json')['result'],result)
                self.assertEqual(state.read(f'state/tasks/{task["id"]}/task.json'),task)
                self.assertEqual(set(state.overlay),{f'state/campaigns/{task["campaign"]}.json',audit_path})
                passed += 1
        # Seventeen quotation artifacts plus the one reference-only correction.
        self.assertEqual(passed,18)

    def test_old_contracts_retain_all_22_quotation_holds(self):
        for case in MANIFEST['cases']:
            if case['classification'] in ('reference_parser_only','line_break_structure_changed'):
                continue
            for version in (None,'1'):
                with self.subTest(task=case['task_id'],version=version):
                    state,task,result,_ = artifact(case,version)
                    with self.assertRaises(ScriptureAttention):
                        normalize_scripture_candidate(state,task,result)
                    self.assertNotIn(f'state/tasks/{task["id"]}/scripture-selections.json',state.overlay)

    def test_four_real_claims_beyond_old_utf8_bounds_are_proven(self):
        for prefix in ('f3ec97','ecce2d','7256ec','dae9ed'):
            state,task,result,evidence = artifact(case_for(prefix))
            quote = evidence['quotes'][0]
            claim = result['scripture_selections'][0]
            block = ''.join(Fragment(result['html'],task['article_id']).text_by_block[quote['block']])
            self.assertTrue(claim['end'] > len(block.encode('utf-8')) or any(
                part['end'] > len(verse['text'].encode('utf-8'))
                for part,verse in zip(claim['fragments'][0],quote['target_verses'])))
            normalize_scripture_candidate(state,task,result)
            validate_scripture_candidate(state,task,{k:result[k] for k in ('html','title','subtitle','section')})

    def test_expanded_audit_provenance_cannot_be_removed_relabelled_or_changed(self):
        state,task,result,_ = artifact(case_for('f3ec97'))
        candidate = normalize_scripture_candidate(state,task,result)
        path = f'state/tasks/{task["id"]}/scripture-selections.json'
        original = state.read(path)
        mutations = [lambda a:a.pop('normalization'),lambda a:a.pop('input_result_sha256'),
                     lambda a:a['normalization'].update(version='1'),
                     lambda a:a['normalization'].update(original_failure='invented'),
                     lambda a:a['normalization'].update(independent_review_required=False),
                     lambda a:a['normalization'].update(raw_result_sha256='0'*64),
                     lambda a:a['selections'][0].update(start=0),
                     lambda a:a.update(evidence_sha256='0'*64)]
        for number,mutate in enumerate(mutations):
            changed = copy.deepcopy(original)
            mutate(changed)
            state.write(path,changed)
            with self.subTest(mutation=number),self.assertRaises(ScriptureAttention):
                validate_scripture_candidate(state,task,candidate)
        # Even internally consistent fabricated originals must match the full
        # archived provider result, which stays immutable throughout replay.
        changed = copy.deepcopy(original)
        changed['normalization']['original_selections'][0]['end'] += 1
        raw_hash = json_hash({**candidate,'scripture_selections':changed['normalization']['original_selections']})
        changed['input_result_sha256'] = raw_hash
        changed['normalization']['raw_result_sha256'] = raw_hash
        state.write(path,changed)
        with self.assertRaisesRegex(ScriptureAttention,'archived provider result'):
            validate_scripture_candidate(state,task,candidate)

    def test_adoption_retains_original_v2_claims_and_requires_same_source_evidence(self):
        state,task,result,_ = artifact(case_for('f3ec97'))
        candidate = normalize_scripture_candidate(state,task,result)
        next_task = {**task,'id':task['id']+'-hypothetical'}
        state.write(f'state/tasks/{next_task["id"]}/candidate.json',candidate)
        self.assertTrue(adopt_scripture_selection_audit(state,next_task,task))
        validate_scripture_candidate(state,next_task,candidate)
        before = state.read(f'state/tasks/{task["id"]}/scripture-selections.json')
        after = state.read(f'state/tasks/{next_task["id"]}/scripture-selections.json')
        self.assertEqual(before,after)
        state.overlay[f'state/tasks/{next_task["id"]}/candidate.json']['html'] += 'changed'
        self.assertFalse(adopt_scripture_selection_audit(state,next_task,task))


class StructuralScopeNegatives(unittest.TestCase):
    def setUp(self):
        self.state,self.task,self.result,self.evidence = artifact(case_for('f3ec97'))
        self.quote = self.evidence['quotes'][0]
        self.expected = ' '.join(v['text'].strip() for v in self.quote['target_verses'])
        block = ''.join(Fragment(self.result['html'],self.task['article_id']).text_by_block[self.quote['block']])
        self.tail = block[block.index(self.expected)+len(self.expected):]

    def assert_rejected(self, result, evidence=None):
        with self.assertRaises(ContractError):
            v2_repair(self.state,self.task,result,evidence or self.evidence)

    def test_exact_text_outside_original_scope_never_qualifies(self):
        replacements = {
            'prose instead of emphasis':self.expected+'<em>'+self.tail+'</em>',
            'prose before quotation':'<em>ADDED '+self.expected+self.tail+'</em>',
            'prose after quotation':'<em>'+self.expected+' ADDED'+self.tail+'</em>',
            'citation outside emphasis':'<em>'+self.expected+'</em>'+self.tail,
            'duplicate in prose':self.expected+'<em>'+self.expected+self.tail+'</em>',
            'no quote at original scope':'<em>ordinary prose'+self.tail+'</em>',
        }
        original = '<em>'+self.expected+self.tail+'</em>'
        self.assertIn(original,self.result['html'])
        for label,replacement in replacements.items():
            result = copy.deepcopy(self.result)
            result['html'] = result['html'].replace(original,replacement)
            with self.subTest(mutation=label):
                self.assert_rejected(result)
        # Same exact words elsewhere in the article cannot repair a moved scope.
        result = copy.deepcopy(self.result)
        result['html'] = result['html'].replace(self.expected,'').replace('</p>',self.expected+'</p>',1)
        self.assert_rejected(result)
        result = copy.deepcopy(self.result)
        result['html'] = result['html'].replace('</p>',self.expected+'</p>',1)
        self.assert_rejected(result)

    def test_no_word_punctuation_unicode_or_scope_edits(self):
        variants = {
            'omitted verse':' '.join(v['text'].strip() for v in self.quote['target_verses'][:-1]),
            'omitted word':self.expected.replace('Señor, ',''),
            'punctuation':self.expected[:-1]+';',
            'unicode normalization':self.expected.replace('Tú','Tu\u0301'),
            'internal whitespace':self.expected.replace('Tú, Señor','Tú,  Señor'),
            'reordered verses':' '.join(v['text'].strip() for v in reversed(self.quote['target_verses'])),
            'ellipsis':self.expected[:-4]+'…',
            'zero width':self.expected[:20]+'\u200b'+self.expected[20:],
        }
        for label,value in variants.items():
            self.assertNotEqual(value,self.expected)
            result = copy.deepcopy(self.result)
            result['html'] = result['html'].replace(self.expected,value)
            with self.subTest(mutation=label):
                self.assert_rejected(result)

    def test_whole_citation_tail_must_have_one_exact_identity(self):
        for tail in ('', ' –Heb 1:10-11.', ' –John 1:10-12.', self.tail+self.tail,
                     self.tail+' John 3:16.', self.tail+' ADDED', ' unclear '+self.tail,
                     self.tail+'\u200b', self.tail+' 42', ' –Unknown 1:10-12.',
                     ' ...'+self.tail, ' . . .'+self.tail, ' ['+self.tail, ' ;;'+self.tail,
                     ' –(Heb 1:10-12.', ' –[Heb 1:10-12).', ' –((Heb 1:10-12)).',
                     ' –Heb 1:10-12...', ' –(Heb 1:10-12.).'):
            result = copy.deepcopy(self.result)
            result['html'] = result['html'].replace(self.expected+self.tail,self.expected+tail)
            with self.subTest(tail=tail):
                self.assert_rejected(result)

    def test_finite_citation_tail_allows_balanced_localized_wrappers(self):
        for tail in (' –(Heb 1:10-12).',' — [Heb 1:10-12].',' (Heb 1:10-12.)',
                     ' (Heb 1:10-12)', '——Heb 1:10-12。'):
            result = copy.deepcopy(self.result)
            result['html'] = result['html'].replace(self.expected+self.tail,self.expected+tail)
            with self.subTest(tail=tail):
                self.assertTrue(v2_repair(self.state,self.task,result,self.evidence))

    def test_long_whitespace_tail_is_parsed_without_ambiguous_regex_backtracking(self):
        from berean_translation.scripture_scope import citation_tail_matches
        padding = ' '*100_000
        self.assertTrue(citation_tail_matches(padding+'— '+padding+'(Heb 1:10-12).'+padding,
                                            'Hebrews 1:10-12',evidence=self.evidence))
        for value in (padding+'[ [Heb 1:10-12.', ' –(Heb 1:10-12'+padding+'... )',
                      padding+' ... Heb 1:10-12.'):
            self.assertFalse(citation_tail_matches(value,'Hebrews 1:10-12',evidence=self.evidence))

    def test_blockquote_shape_spans_original_inline_subtrees(self):
        state,task,result,evidence = artifact(case_for('794cf7'))
        repaired = v2_repair(state,task,result,evidence)
        self.assertEqual(repaired[0]['start'],0)
        for insert in ('ADDED ','\u200b'):
            changed = copy.deepcopy(result)
            changed['html'] = re.sub(r'(<blockquote>\s*<p>)',r'\g<1>'+insert,changed['html'],count=1)
            self.assertNotEqual(changed['html'],result['html'])
            with self.assertRaises(ContractError):
                v2_repair(state,task,changed,evidence)

    def test_partial_or_ellipsis_alignment_is_not_repairable(self):
        for field,value in (('kind','partial'),('ellipsis',['…']),('leading_ellipsis',True),('trailing_ellipsis',True)):
            evidence = copy.deepcopy(self.evidence)
            evidence['quotes'][0]['alignment'][field] = value
            with self.subTest(field=field):
                self.assert_rejected(self.result,evidence)

    def test_missing_wrong_duplicate_or_reordered_claim_identities_fail(self):
        mutations = [lambda c:c.pop(),lambda c:c[0].update(block='/article[1]/p[3]'),
                     lambda c:c[0].update(quote_id='q2'),lambda c:c[0]['fragments'][0].pop(),
                     lambda c:c[0]['fragments'][0].reverse(),
                     lambda c:c[0]['fragments'][0][0].update(verse=11),
                     lambda c:c[0]['fragments'][0][0].update(verse=True),
                     lambda c:c[0].update(end=True),
                     lambda c:c[0]['fragments'][0][0].update(start=True)]
        for number,mutate in enumerate(mutations):
            result = copy.deepcopy(self.result)
            mutate(result['scripture_selections'])
            with self.subTest(mutation=number):
                self.assert_rejected(result)

    def test_source_fingerprint_and_malformed_html_fail_closed(self):
        source = self.state.source(self.task)
        source['html'] = source['html'].replace('And, Thou','Changed, Thou')
        with self.assertRaisesRegex(ScriptureAttention,'snapshot'):
            repair_complete_selections(self.evidence,self.result,self.result['scripture_selections'],
                                       normalization_version='2',source=source)
        for html in (self.result['html'].replace('</em>','</strong>',1),self.result['html'][:-10]):
            result = {**self.result,'html':html}
            self.assert_rejected(result)

    def test_resource_bounds_do_not_assign_authority_to_finite_offsets(self):
        result = copy.deepcopy(self.result)
        result['scripture_selections'][0].update(start=10**80,end=10**81)
        for part in result['scripture_selections'][0]['fragments'][0]:
            part['end'] = 10**80
        self.assertEqual(v2_repair(self.state,self.task,result,self.evidence),
                         v2_repair(self.state,self.task,self.result,self.evidence))
        result['scripture_selections'][0]['quote_id'] = 'q'*40_000
        with self.assertRaisesRegex(ScriptureAttention,'selection_size_limit'):
            v2_repair(self.state,self.task,result,self.evidence)
        self.assert_rejected({**self.result,'html':'x'*(MAX_SCOPE_HTML_BYTES+1)})
        result = copy.deepcopy(self.result)
        result['html'] = result['html'].replace(self.expected,'<em>'*130+self.expected+'</em>'*130)
        self.assert_rejected(result)

    def test_malformed_finite_offsets_have_no_authority_over_independent_proof(self):
        exact = v2_repair(self.state,self.task,self.result,self.evidence)
        for start,end in ((-10,-100),(100,10),(10**80,-10**80),(5,5),(0,0)):
            result = copy.deepcopy(self.result)
            result['scripture_selections'][0].update(start=start,end=end)
            for part in result['scripture_selections'][0]['fragments'][0]:
                part.update(start=start,end=end)
            with self.subTest(start=start,end=end):
                self.assertEqual(v2_repair(self.state,self.task,result,self.evidence),exact)
                with self.assertRaises(ScriptureAttention):
                    repair_complete_selections(self.evidence,result,result['scripture_selections'])
                for fault in ('words','scope','verse'):
                    changed = copy.deepcopy(result)
                    if fault == 'words':
                        changed['html'] = changed['html'].replace(self.expected,self.expected[:-1])
                    elif fault == 'scope':
                        changed['html'] = changed['html'].replace('<em>'+self.expected,
                                                               self.expected+'<em>')
                    else:
                        changed['scripture_selections'][0]['fragments'][0][0]['verse'] = 999
                    self.assert_rejected(changed)


class WhitespaceContinuityTests(unittest.TestCase):
    def test_full_raw_mandarin_claims_pass_without_metadata_repair_only_on_v2(self):
        state,task,result,evidence = artifact(case_for('c85a7c'))
        quote = evidence['quotes'][0]
        claim = result['scripture_selections'][0]
        for part,verse in zip(claim['fragments'][0],quote['target_verses']):
            self.assertEqual(verse['text'][part['end']:],' ')
            self.assertEqual(part['start'],0)
        with self.assertRaisesRegex(ScriptureAttention,'unauthorized_omission'):
            check_selections(evidence,result,result['scripture_selections'])
        self.assertTrue(check_selections(evidence,result,result['scripture_selections'],
                                        normalization_version='2',source=state.source(task)))
        candidate = normalize_scripture_candidate(state,task,result)
        audit = state.read(f'state/tasks/{task["id"]}/scripture-selections.json')
        self.assertNotIn('normalization',audit)
        self.assertEqual(audit['selections'],result['scripture_selections'])
        validate_scripture_candidate(state,task,candidate)

    def test_continuity_ignores_only_existing_outer_whitespace(self):
        _,_,result,evidence = artifact(case_for('c85a7c'))
        quote = evidence['quotes'][0]
        claim = result['scripture_selections'][0]
        expected = ' '.join(v['text'].strip() for v in quote['target_verses'])
        self.assertEqual(_selection_text(quote,claim,allow_outer_whitespace=True),expected)
        for index in (0,1,2):
            changed = copy.deepcopy(claim)
            changed['fragments'][0][index]['end'] -= 1
            with self.subTest(verse=index),self.assertRaises(ScriptureAttention):
                _selection_text(quote,changed,allow_outer_whitespace=True)
        for index in (0,1,2):
            changed = copy.deepcopy(claim)
            changed['fragments'][0][index]['start'] = 1
            with self.subTest(start=index),self.assertRaises(ScriptureAttention):
                _selection_text(quote,changed,allow_outer_whitespace=True)
        for excluded in ('x','.','\u200b','\u200e','…'):
            changed_quote = copy.deepcopy(quote)
            changed_quote['target_verses'][0]['text'] += excluded
            with self.subTest(excluded=excluded),self.assertRaises(ScriptureAttention):
                _selection_text(changed_quote,claim,allow_outer_whitespace=True)


class RepeatedFrozenScopeTests(unittest.TestCase):
    def documents(self, mixed=False):
        english = scripture_fixtures.fixture('kjv')['structuredContent']['data']['verses'][1]['text'].strip()
        target = scripture_fixtures.fixture('luther1545')['structuredContent']['data']['verses'][1]['text'].strip()
        block = '<blockquote><p>{text} –{reference}.</p></blockquote>'
        second = '<p>“{text}” ({reference}).</p>' if mixed else block
        body = block+second+'<p>Ordinary conclusion.</p>'
        source = scripture_fixtures.source(markup=body.format(text=english,reference='John 4:16'))
        helper = scripture_fixtures.ScriptureEvidenceTests()
        helper.setUp()
        evidence = helper.build(source)
        candidate = {'html':source['html'].replace(english,target).replace('John 4:16','Johannes 4:16'),
                     'title':'Teaching','subtitle':None,'section':None}
        selections = [{'quote_id':q['id'],'block':q['block'],'start':1 if q['delimited'] else 0,
                       'end':len(target)+(1 if q['delimited'] else 0),
                       'fragments':[[{'verse':v['verse'],'start':0,'end':len(v['text'])}
                                     for v in q['target_verses']]]} for q in evidence['quotes']]
        return source,evidence,candidate,selections,target

    def test_same_text_in_two_explicit_source_scopes_and_mixed_delimiters_is_valid(self):
        for mixed in (False,True):
            with self.subTest(mixed=mixed):
                source,evidence,candidate,selections,_ = self.documents(mixed)
                self.assertEqual(len(evidence['quotes']),2)
                self.assertTrue(check_selections(evidence,candidate,selections))
                self.assertTrue(check_selections(evidence,candidate,selections,
                                                normalization_version='2',source=source))
                broken = copy.deepcopy(selections)
                for claim in broken:
                    claim.update(start=-999,end=-1000)
                    for part in claim['fragments'][0]:
                        part.update(start=500,end=-200)
                self.assertEqual(repair_complete_selections(evidence,candidate,broken,
                                                           normalization_version='2',source=source),selections)

    def test_repeated_source_scopes_do_not_authorize_an_extra_or_moved_occurrence(self):
        for mixed in (False,True):
            source,evidence,candidate,selections,target = self.documents(mixed)
            variants = {
                'extra prose':candidate['html'].replace('Ordinary conclusion.',target),
                'duplicate within scope':candidate['html'].replace(target,target+' '+target,1),
                'missing first scope':candidate['html'].replace(target,'',1),
                'moved first scope':candidate['html'].replace(target,'',1).replace('Ordinary conclusion.',target),
                'changed first scope':candidate['html'].replace(target,target[:-1],1),
            }
            for label,html in variants.items():
                with self.subTest(mixed=mixed,mutation=label),self.assertRaises(ScriptureAttention):
                    repair_complete_selections(evidence,{**candidate,'html':html},selections,
                                               normalization_version='2',source=source)


class StructuralScopeLifecycleTests(unittest.TestCase):
    """Real Engine/State transitions, fixture-only Scripture and Batch providers."""
    def setUp(self):
        autonomous_fixtures.AutonomousScriptureTests.setUp(self)
        self.printed_english = self.english.strip().rstrip('.')
        path = f'content/articles/{A}.html'
        self.upstream.contents[path] = self.upstream.contents[path].replace(
            f'<p>“{self.english}” (John 4:16).</p>',
            f'<p>Teaching. <em>{self.printed_english} –John 4:16.</em></p>')
        self.assertIn('Teaching. <em>',self.upstream.contents[path])
        self.upstream.rebuild()
        self.engine.discover()
        self.change_punctuation = False

    def responder(self, line):
        result = self.provider.default_result(line)
        if line['custom_id'].split(':')[1].startswith('review'):
            return result
        text = self.target.strip()
        if self.change_punctuation:
            text = text[:-1] if text.endswith('.') else text+';'
        result['html'] = result['html'].replace(self.printed_english,text).replace('John 4:16','Johannes 4:16')
        result['scripture_selections'] = [{'quote_id':'q1','block':'/article[1]/p[1]',
            'start':-999,'end':0,'fragments':[[{'verse':16,'start':99,'end':-1}]]}]
        return result

    def test_automatic_v2_repair_advances_once_to_review_and_only_then_publishes(self):
        self.engine.tick()
        task = self.state.tasks()[0]
        campaign = self.state.campaigns()[0]
        self.assertTrue(campaign['autonomous'])
        self.assertEqual(campaign['scripture_quotes']['selection_normalization_version'],'2')
        self.assertEqual(task['translation_attempts'],1)
        self.provider.complete_all(self.responder)
        self.engine.tick()
        task = self.state.tasks()[0]
        self.assertEqual((task['stage'],task['status']),('review1','in_batch'))
        self.assertEqual((task['translation_attempts'],task['review_attempts']),(1,1))
        self.assertIsNone(self.state.record('deu',A)['published'])
        self.assertFalse(self.state.projection(self.config)['articles'])
        audit = self.state.read(f'state/tasks/{task["id"]}/scripture-selections.json')
        original = self.state.read(f'state/tasks/{task["id"]}/results/translate.json')['result']
        self.assertEqual(audit['normalization']['original_selections'],original['scripture_selections'])
        self.assertTrue(audit['normalization']['independent_review_required'])
        self.assertEqual(audit['normalization']['raw_result_sha256'],json_hash(original))
        review_batch = next(batch for batch in self.state.batches() if batch['stage']=='review1')
        line = loads(self.state.path(f'state/batches/{review_batch["id"]}/input.jsonl').read_bytes().splitlines()[0])
        payload = loads(line['body']['messages'][1]['content'])
        self.assertEqual(payload['scripture_selection_audit'],audit)
        self.assertEqual(self.provider.create_calls,2)
        self.provider.complete_all(self.responder)
        self.engine.tick()
        task = self.state.tasks()[0]
        self.assertEqual(task['status'],'complete')
        self.assertEqual((task['translation_attempts'],task['review_attempts']),(1,1))
        self.assertEqual([event['stage'] for event in task['events']],['translate','review1'])
        self.assertEqual(len(self.state.projection(self.config)['articles']),1)
        self.assertEqual(self.provider.create_calls,2)
        validate_repository(self.config)

    def test_changed_punctuation_exhausts_only_original_bounded_attempts_and_stays_held(self):
        self.change_punctuation = True
        drive(self.engine,self.provider,self.responder,ticks=3)
        tasks = self.state.tasks()
        self.assertEqual(len(tasks),1)
        task = tasks[0]
        self.assertEqual(task['status'],'not_ready')
        self.assertEqual((task['translation_attempts'],task['review_attempts']),(2,0))
        self.assertEqual([event['stage'] for event in task['events']],['translate','correct'])
        self.assertIsNone(self.state.record('deu',A)['published'])
        self.assertFalse(self.state.projection(self.config)['articles'])
        self.assertIsNone(self.state.read(f'state/tasks/{task["id"]}/scripture-selections.json'))
        self.assertEqual(self.provider.create_calls,2)
        self.assertIsNotNone(self.state.read(f'state/tasks/{task["id"]}/results/correct.json'))
        validate_repository(self.config)


if __name__ == '__main__':
    unittest.main()
