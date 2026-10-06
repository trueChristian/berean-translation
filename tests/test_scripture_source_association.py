"""Offline v2 source association regressions; synthetic controls are not provider provenance."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, canonical, digest, json_hash, read_json
from berean_translation.scripture_association import associate, AssociationError, mentions
from berean_translation.scripture_evidence import (
    ScriptureAttention, build_evidence, check_selections, policy, frozen_policy,
    load_evidence, _unique_alignment, repair_complete_selections)
from berean_translation.scripture_provider import ScriptureProviderError
from test_scripture_evidence import source, FakeMCP
from berean_translation.scripture_provider import GetBibleMCP

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = read_json(ROOT / 'tests/fixtures/source-association-archives.json')
VERSE = 'Jesus saith unto her, Go, call thy husband, and come hither.'


class SyntheticChapter:
    """Explicit local test double. Never represents an archived GetBible response."""
    def __init__(self, book, chapter, verses):
        self.book, self.number, self.verses = book, chapter, verses
        self.calls = []
    def chapter(self, abbreviation, book, chapter):
        self.calls.append((abbreviation, book, chapter))
        if (book, chapter) != (self.book, self.number):
            raise ScriptureProviderError('No synthetic control for that chapter')
        return {'result': {'cache': {'cacheable': True, 'expires_at': '2099-01-01T00:00:00Z'},
                           'data': {'book_name': 'Job' if book == 18 else 'John',
                                    'verses': copy.deepcopy(self.verses)}},
                'fixture_kind': 'synthetic_adversarial_control_not_provider_provenance'}


class SourceAssociationTests(unittest.TestCase):
    def setUp(self):
        self.contract = policy(ROOT)
        self.provider = GetBibleMCP(FakeMCP())
    def build(self, markup, tag='de', provider=None):
        return build_evidence(source(markup=markup), tag, self.contract, provider or self.provider)
    def inspect(self, markup):
        src = source(markup=markup)
        return associate(src['html'], src['article']['id'])
    def assert_attention(self, markup, reason='ambiguous_quote_reference'):
        with self.assertRaisesRegex(ScriptureAttention, reason):
            self.build(markup)

    def test_complete_source_inventory_and_immutable_fingerprints(self):
        for article in INVENTORY['articles']:
            src = read_json(ROOT / article['source_path'])
            self.assertEqual(json_hash(src), article['source_canonical_sha256'])
            self.assertEqual(digest((ROOT/article['source_path']).read_bytes()), article['source_file_bytes_sha256'])
            _, refs, quotes, ordinary = associate(src['html'], src['article']['id'])
            expected = [r for p in article['paragraphs'] for r in p['references']
                        if r['kind'] == 'explicit_emphasized_quote']
            self.assertEqual(len(quotes), article['explicit_emphasized_quote_count'])
            self.assertEqual([(q['reference'], q['source_quote']) for q in quotes],
                             [(r['reference'], r['source_quote'].rstrip()) for r in expected])
            self.assertEqual(len(refs), 25 if article['article_id'].startswith('3dd') else 7)
            for q in quotes:
                self.assertNotIn(q['source_quote'], ('seeds', 'mount that might be touched', 'few and evil have been my days.'))
            self.assertEqual(src, read_json(ROOT/article['source_path']))

    def test_real_cloud_scope_excludes_seeds_and_following_prose(self):
        article = INVENTORY['articles'][0]
        src = read_json(ROOT/article['source_path'])
        from berean_translation.scripture_scope import ScopeFragment
        parsed = ScopeFragment(src['html'], src['article']['id'])
        block = parsed.blocks['/article[1]/p[3]']
        quote = next(r for p in article['paragraphs'] if p['paragraph'] == 3 for r in p['references'])['source_quote'].rstrip()
        provider = SyntheticChapter(18, 26, [{'verse':8, 'text':quote+'.'}])
        # The fixture is a verbatim block, not a reworded minimal reproduction.
        import re
        paragraph = re.findall(r'<p(?: [^>]*)?>.*?</p>', src['html'], re.S)[2]
        evidence = self.build(paragraph, 'en', provider)
        self.assertEqual(len(evidence['quotes']), 1)
        self.assertEqual(evidence['quotes'][0]['source_quote'], quote)
        self.assertIn('“seeds”', block)
        self.assertNotIn('seeds', evidence['quotes'][0]['source_quote'])

    def test_real_inquisition_exposes_annotation_before_fetch(self):
        src = read_json(ROOT/INVENTORY['articles'][1]['source_path'])
        class NoCalls:
            def chapter(self, *args): raise AssertionError('Annotation must hold before provider calls')
        with self.assertRaisesRegex(ScriptureAttention, r'source_quote_annotation.*\[that\]'):
            build_evidence(src, 'de', self.contract, NoCalls())

    def test_direct_citation_inside_delimiters_is_a_scripture_quote(self):
        for markup in (f'<p>“{VERSE} —John 4:16.”</p>',
                       f'<p><em>“{VERSE} —John 4:16.”</em></p>',
                       f'<p>John 4:16: “{VERSE}”</p>',
                       f'<p>John 4:16 reads: “{VERSE}”</p>',
                       f'<p>John 4:16, saying, “{VERSE}”</p>',
                       f'<p>In John 4:16 the Bible says “{VERSE}”</p>',
                       f'<p>John 4:16 “{VERSE}”</p>',
                       f'<p>John 4:16—“{VERSE}”</p>',
                       f'<p>“{VERSE}” says John 4:16.</p>',
                       f'<p>“As we read in John 4:16, ‘{VERSE}’”</p>',
                       f'<p>Jesus said, “{VERSE}” (John 4:16).</p>'):
            with self.subTest(markup=markup):
                self.assertEqual(self.build(markup)['quotes'][0]['source_quote'], VERSE)

    def test_human_dialogue_keeps_nested_and_nearby_genuine_quotes(self):
        for markup in (
            f'<p>“I have read this: ‘{VERSE}’ (John 4:16). It is helpful,” said Jacques.</p>',
            f'<p>“I object,” said Jacques. <em>{VERSE} —John 4:16.</em> The discussion continued.</p>',
            f'<p>“As we read in John 4:15, this is useful. <em>{VERSE} —John 4:16.</em> Let us continue.”</p>'):
            with self.subTest(markup=markup):
                self.assertEqual([q['source_quote'] for q in self.build(markup)['quotes']], [VERSE])

    def test_unknown_speech_scope_never_becomes_unchecked_prose(self):
        for markup in ('<p>“God likes everyone according to John 4:16.”</p>',
                       '<p><em>God likes everyone —John 4:16.</em></p>',
                       '<p>“God likes everyone —John 4:16.”</p>'):
            with self.subTest(markup=markup), self.assertRaises(ScriptureAttention):
                self.build(markup)

    def test_plain_prose_substring_does_not_establish_quotation(self):
        _, _, quotes, _ = self.inspect('<p>Our discussion contains Go, call thy husband in ordinary prose as we read in John 4:16.</p>')
        self.assertEqual(quotes, [])

    def test_each_independent_scope_keeps_its_own_citation(self):
        evidence = self.build(f'<p><em>{VERSE} —John 4:16.</em> and <em>Sir, give me this water —John 4:15.</em></p>')
        self.assertEqual([q['reference'] for q in evidence['quotes']], ['John 4:16', 'John 4:15'])
        self.assert_attention(f'<p><em>{VERSE} —John 4:16; John 4:15.</em></p>')
        self.assert_attention(f'<p>“{VERSE}” (John 4:16; John 4:16).</p>')

    def test_wrong_reference_range_and_annotated_words_hold(self):
        self.assert_attention(f'<p><em>{VERSE} —John 4:15.</em></p>', 'printed_reference_mismatch')
        self.assert_attention('<p><em>Go, [call] thy husband —John 4:16.</em></p>', 'source_quote_annotation')
        self.assert_attention('<p><em>Go, call thy husband —Mystery 4:16.</em></p>', 'ambiguous_quote_reference')

    def test_malformed_adjacent_citations_cannot_disappear(self):
        for tail in ('(John 4:16', '(John 4:16;)', '(John 4:16; author note)',
                     '(John 4:16 and John 4:15)', '(John 4:16 / John 4:15)',
                     '(John 4:16-xx)', '(Mystery 4:16)'):
            with self.subTest(tail=tail), self.assertRaises(ScriptureAttention):
                self.build(f'<p>“{VERSE}” {tail}</p>')

    def test_bare_reference_is_not_an_empty_quotation(self):
        for text in ('John 4:16', 'John 4:16.', '(John 4:16)'):
            self.assertEqual(self.build(f'<p>{text}</p>')['quotes'], [])

    def test_whole_cloud_replay_cannot_claim_missing_archived_alignment(self):
        src = read_json(ROOT/INVENTORY['articles'][0]['source_path'])
        class ArchiveOnly:
            def chapter(self, abbreviation, book, number):
                key = f'{abbreviation}/{book}/{number}'
                if key in INVENTORY['missing_lookup_keys']:
                    raise ScriptureProviderError(f'No authentic local archive: {key}')
                raise AssertionError('Unexpected chapter request')
        with self.assertRaisesRegex(ScriptureAttention, 'fetch_failed.*kjv/2/16'):
            build_evidence(src, 'de', self.contract, ArchiveOnly())
        self.assertEqual(INVENTORY['matching_original_archived_lookup_envelopes'], {})

    def test_replaying_both_legacy_sources_preserves_false_association_holds(self):
        for index, book, chapter, verses in (
                (0,18,26,[{'verse':8,'text':'He bindeth up the waters in his thick clouds; and the cloud is not rent under them.'}]),
                (1,40,5,[{'verse':39,'text':'But I say unto you, That ye resist not evil.'}])):
            src = read_json(ROOT/INVENTORY['articles'][index]['source_path'])
            contract = {k:v for k,v in self.contract.items() if k != 'source_association_version'}
            before = canonical(src)
            with self.assertRaisesRegex(ScriptureAttention, 'ambiguous_source_quote'):
                build_evidence(src, 'de', contract, SyntheticChapter(book,chapter,verses))
            self.assertEqual(canonical(src),before)

    def test_untrusted_source_parser_work_is_bounded(self):
        with self.assertRaisesRegex(ScriptureAttention, 'source_association_limit'):
            self.build('<p>' + '<em>ordinary</em>' * 1600 + '</p>')

    def test_aliases_are_finite_and_identity_qualified(self):
        for printed, identity in [('Exo 16:10', 'Exodus 16:10'), ('Pro 4:18', 'Proverbs 4:18'),
                                  ('Jam 5:17', 'James 5:17'), ('1 Kin 19:11-13', '1 Kings 19:11-13'),
                                  ('Act 1:9', 'Acts 1:9')]:
            self.assertEqual(mentions(printed), [(0,len(printed),identity)])
        for text in ('2 Kin 19:11', 'Actuary 1:9', 'Prose 4:18', '4 Exo 16:10'):
            self.assertFalse(any(key.startswith(('Exodus ', 'Proverbs ', 'Acts ', '1 Kings ')) for _,_,key in mentions(text)))

    def test_unavailable_editions_no_fallback(self):
        for tag in ('bn','hi','id','sw','ur'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ScriptureAttention, 'missing_edition'):
                self.build(f'<p>“{VERSE}” (John 4:16).</p>', tag)

    def test_legacy_extraction_unchanged_and_unknown_policy_fails(self):
        markup = f'<p>We call these “seeds”. Later we read <em>{VERSE} —John 4:16.</em></p>'
        for version in (None, '1'):
            contract = copy.deepcopy(self.contract)
            if version is None: contract.pop('source_association_version')
            else: contract['source_association_version'] = version
            with self.assertRaisesRegex(ScriptureAttention, 'ambiguous_source_quote'):
                build_evidence(source(markup=markup), 'de', contract, self.provider)
        self.assertEqual(len(self.build(markup)['quotes']), 1)
        self.contract['source_association_version'] = '999'
        with self.assertRaises(ContractError): frozen_policy({'scripture_quotes':self.contract})
        with self.assertRaisesRegex(ScriptureAttention, 'unknown_contract'):
            self.build(markup)


class SourceAssociationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.source = source(markup='<p>A gardener said “seeds”. <em>Go, call thy husband —John 4:16.</em> More discussion.</p>')
        self.evidence = build_evidence(self.source, 'de', policy(ROOT), GetBibleMCP(FakeMCP()))
        verse = self.evidence['quotes'][0]['target_verses'][0]
        text = 'Gehe hin, rufe deinen Mann'
        self.target_text = text
        self.candidate = {'html': self.source['html'].replace('Go, call thy husband',text),
                          'title':'Teaching','subtitle':None,'section':None}
        from berean_translation.scripture_scope import ScopeFragment
        block = ScopeFragment(self.candidate['html'], self.evidence['article_id']).blocks['/article[1]/p[1]']
        position = block.index(text)
        start = verse['text'].index(text)
        self.claims = [{'quote_id':'q1','block':'/article[1]/p[1]','start':position,'end':position+len(text),
                        'fragments':[[{'verse':16,'start':start,'end':start+len(text)}]]}]
    def check(self, candidate=None, claims=None, src=None):
        return check_selections(self.evidence, candidate or self.candidate, claims or self.claims,
                                normalization_version='2', source=src or self.source)
    def test_partial_mid_paragraph_emphasis_exact_selection_passes(self):
        self.assertTrue(self.check())
    def test_source_edit_fails_frozen_hash(self):
        changed = {**self.source, 'html':self.source['html'].replace('gardener','farmer')}
        with self.assertRaisesRegex(ScriptureAttention, 'quote_scope_changed'):
            self.check(src=changed)
    def test_wrong_missing_duplicate_citation_blocks(self):
        for ref in ('Luke 4:16','John 4:15','John 4:15-16','', 'John 4:16; John 4:16'):
            changed = {**self.candidate,'html':self.candidate['html'].replace('John 4:16',ref)}
            with self.subTest(ref=ref), self.assertRaises(ScriptureAttention): self.check(changed)
    def test_ordinary_prose_occurrence_cannot_claim_the_emphasis_scope(self):
        changed = {**self.candidate,'html':self.candidate['html'].replace('A gardener said “seeds”.',self.target_text)}
        claims = copy.deepcopy(self.claims); claims[0].update(start=0,end=len(self.target_text))
        with self.assertRaisesRegex(ScriptureAttention,'quote_scope_changed'): self.check(changed,claims)
    def test_quotation_cannot_move_to_a_sibling_emphasis(self):
        changed = {**self.candidate,'html':self.candidate['html'].replace('<em>','<strong>').replace('</em>','</strong>')}
        with self.assertRaises(ScriptureAttention): self.check(changed)
    def test_partial_quotes_never_get_automatic_offset_repair(self):
        with self.assertRaisesRegex(ScriptureAttention,'selection_repair_scope'):
            repair_complete_selections(self.evidence,self.candidate,self.claims,normalization_version='2',source=self.source)

    def test_delimited_quote_cannot_replace_a_later_ordinary_quote(self):
        src = source(markup=f'<p>“{VERSE}” (John 4:16). Alice said “ordinary sentence”.</p>')
        evidence = build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))
        verse = evidence['quotes'][0]['target_verses'][0]
        target = verse['text'].strip()
        changed = {'html':source(markup=f'<p>“ordinary sentence”. Alice said “{target}” (John 4:16).</p>')['html']}
        position = len('“ordinary sentence”. Alice said “')
        claims = [{'quote_id':'q1','block':'/article[1]/p[1]','start':position,'end':position+len(target),
                   'fragments':[[{'verse':16,'start':0,'end':len(verse['text'])}]]}]
        with self.assertRaisesRegex(ScriptureAttention,'quote_scope_changed'):
            check_selections(evidence,changed,claims,normalization_version='2',source=src)
        # Deleting the former first delimiter must not turn a moved second
        # quotation into ordinal zero and evade source occurrence accounting.
        changed['html'] = changed['html'].replace('“ordinary sentence”', 'ordinary sentence')
        claims[0]['start'] -= 2; claims[0]['end'] -= 2
        with self.assertRaisesRegex(ScriptureAttention,'quote_scope_changed'):
            check_selections(evidence,changed,claims,normalization_version='2',source=src)

    def test_delimited_quote_cannot_escape_its_inline_dom_scope(self):
        src = source(markup=f'<p><em>“{VERSE}”</em> (John 4:16). Ordinary prose.</p>')
        evidence = build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))
        verse = evidence['quotes'][0]['target_verses'][0]; target = verse['text'].strip()
        changed = {'html':source(markup=f'<p><em>Ordinary prose.</em> “{target}” (John 4:16).</p>')['html']}
        position = len('Ordinary prose. “')
        claims = [{'quote_id':'q1','block':'/article[1]/p[1]','start':position,'end':position+len(target),
                   'fragments':[[{'verse':16,'start':0,'end':len(verse['text'])}]]}]
        with self.assertRaisesRegex(ScriptureAttention,'quote_scope_changed'):
            check_selections(evidence,changed,claims,normalization_version='2',source=src)

    def test_translated_allusion_to_same_identity_keeps_frozen_occurrence(self):
        src = source(markup=f'<p>“As we read in John 4:16, the conversation continued.” <em>{VERSE} —John 4:16.</em></p>')
        evidence = build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))
        verse = evidence['quotes'][0]['target_verses'][0]; target = verse['text'].strip()
        prefix = '“Wie wir in Johannes 4:16 lesen, ging das Gespräch weiter.” '
        candidate = {'html':source(markup=f'<p>{prefix}<em>{target} —Johannes 4:16.</em></p>')['html']}
        claims = [{'quote_id':'q1','block':'/article[1]/p[1]','start':len(prefix),'end':len(prefix)+len(target),
                   'fragments':[[{'verse':16,'start':0,'end':len(verse['text'])}]]}]
        self.assertTrue(check_selections(evidence,candidate,claims,normalization_version='2',source=src))


class AssociationContractTests(unittest.TestCase):
    def test_load_rejects_unknown_or_mismatched_frozen_versions(self):
        src = source()
        evidence = build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))
        class MemoryState:
            def source(self, task): return src
            def read(self, path): return self.campaign if path.startswith('state/campaigns/') else self.evidence
        state = MemoryState()
        for evidence_version, campaign_version in (('2',None), ('2','1'), ('1','2'), ('999','2'), ('2','999')):
            state.evidence = {**evidence,'source_association_version':evidence_version}
            contract = policy(ROOT)
            if campaign_version is None: contract.pop('source_association_version')
            else: contract['source_association_version'] = campaign_version
            state.campaign = {'scripture_quotes':contract}
            sha = json_hash(state.evidence)
            task = {'campaign':'synthetic','scripture_evidence_sha256':sha,'scripture_evidence_path':f'state/scripture/{sha}.json'}
            with self.subTest(evidence=evidence_version,campaign=campaign_version), self.assertRaises(ContractError):
                load_evidence(state,task)


class ArchiveCoverageRegressionTests(unittest.TestCase):
    def test_allusion_cue_cannot_hide_a_following_mismatched_quotation(self):
        for opening in ('We read in John 4:16.', 'Consider this verse in John 4:16.',
                        'We read in (John 4:16).'):
            for quote in (VERSE, 'Jesus asked the woman to bring her husband back.'):
                for body in (f'{opening} “{quote}”', f'“{opening} ‘{quote}’”',
                             f'{opening} Here it is: “{quote}”',
                             f'“{opening} Here it is: ‘{quote}’”'):
                    src = source(markup=f'<p>{body}</p>')
                    with self.subTest(body=body),self.assertRaises(ScriptureAttention):
                        build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))

    def test_complete_unmarked_cited_verse_holds_without_substring_acceptance(self):
        src = source(markup=f'<p>Our discussion repeats {VERSE} This is helpful as we read in John 4:16.</p>')
        before = canonical(src)
        with self.assertRaisesRegex(ScriptureAttention,'unmarked_quote_scope'):
            build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))
        self.assertEqual(canonical(src),before)
        partial = source(markup='<p>Our discussion repeats Go, call thy husband. This is helpful as we read in John 4:16.</p>')
        self.assertEqual(build_evidence(partial,'de',policy(ROOT),GetBibleMCP(FakeMCP()))['quotes'],[])

    def test_complete_verse_only_overlapping_a_partial_scope_remains_held(self):
        src = source(markup='<p>Jesus saith unto her, <em>Go, call thy husband, and come hither. —John 4:16.</em></p>')
        with self.assertRaisesRegex(ScriptureAttention,'unmarked_quote_scope'):
            build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))

    def test_extra_unmarked_complete_occurrence_cannot_borrow_a_proved_scope(self):
        src = source(markup=f'<p><em>{VERSE} —John 4:16.</em> Another occurrence follows: {VERSE}</p>')
        with self.assertRaisesRegex(ScriptureAttention,'unmarked_quote_scope'):
            build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))

    def test_unclaimed_alignment_has_an_explicit_work_bound(self):
        src = source(markup=f'<p><em>{VERSE} —John 4:16.</em> A separate thought follows. “Ordinary example prose.”</p>')
        with patch('berean_translation.scripture_evidence.MAX_UNCLAIMED_ALIGNMENT_WORK', 1):
            with self.assertRaisesRegex(ScriptureAttention,'source_association_limit'):
                build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))

    def test_adversarial_alignment_work_stops_before_unbounded_comparisons(self):
        src = source(markup='<p>“' + ('beta '*500) + '”</p>')
        src['html'] = src['html'].replace('</article>',
            ('<p>“' + 'beta '*500 + '”</p>')*499 +
            ''.join(f'<p>Consider John {chapter}:1.</p>' for chapter in range(1,66)) + '</article>')
        class SyntheticWorkProbe:
            def chapter(self, abbreviation, book, chapter):
                return {'fixture_kind':'synthetic_adversarial_work_probe', 'result':{
                    'cache':{'cacheable':True,'expires_at':'2099-01-01T00:00:00Z'},
                    'data':{'book_name':'John','verses':[{'verse':1,'text':'alpha '*1000}]}}}
        with patch('berean_translation.scripture_evidence._unique_alignment',return_value=None) as align:
            with self.assertRaisesRegex(ScriptureAttention,'source_association_limit'):
                build_evidence(src,'de',policy(ROOT),SyntheticWorkProbe())
        self.assertLessEqual(align.call_count,4)

    def test_exact_english_backstop_holds_an_uncited_marked_repetition(self):
        src = source(markup=f'<p><em>{VERSE} —John 4:16.</em> A separate thought follows. <em>Go, call thy husband</em></p>')
        with self.assertRaisesRegex(ScriptureAttention,'unassociated_source_quote'):
            build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))

    def test_exact_english_backstop_never_guesses_or_rewrites_citation(self):
        src = source(markup=f'<p><em>{VERSE} —John 4:16.</em> A separate thought follows. <em>Sir, give me this water</em></p>')
        before = canonical(src)
        with self.assertRaisesRegex(ScriptureAttention,'unassociated_source_quote'):
            build_evidence(src,'de',policy(ROOT),GetBibleMCP(FakeMCP()))
        self.assertEqual(canonical(src),before)

    def test_split_marked_quotes_cannot_lose_the_first_fragment(self):
        src = source(markup='<p>“Jesus saith unto her,” then, “Go, call thy husband, and come hither.” (John 4:16).</p>')
        with self.assertRaises(AssociationError):
            associate(src['html'],src['article']['id'])

    def test_distinct_marked_scopes_cannot_share_one_citation(self):
        src = source(markup=f'<p><em>{VERSE}</em> John 4:16 <em>Go, call thy husband</em></p>')
        with self.assertRaisesRegex(AssociationError,'distinct marked scopes share one citation'):
            associate(src['html'],src['article']['id'])

    def test_prose_intro_cannot_discharge_the_following_marked_scripture(self):
        src = source(markup=f'<p>In John 4:16 Jesus taught a lesson to the woman. “{VERSE}”</p>')
        with self.assertRaisesRegex(AssociationError,'unowned citations'):
            associate(src['html'],src['article']['id'])

    def test_authentic_source_classes_are_covered_or_explicitly_held(self):
        fixture = read_json(ROOT/'tests/fixtures/scripture-association-coverage.json')
        self.assertEqual(fixture['source_revision'], 'f454b2d575b6a491982a483cd69708bddb270669')
        for case in fixture['cases']:
            with self.subTest(case=case['case']):
                expected = case['expected']
                if expected['status'] == 'attention':
                    with self.assertRaises(AssociationError) as held:
                        associate(case['snippet_html'],case['article_id'])
                    self.assertEqual(held.exception.reason,expected['reason'])
                else:
                    _,refs,quotes,_ = associate(case['snippet_html'],case['article_id'])
                    self.assertEqual([q['reference'] for q in quotes],expected['references'])
                    self.assertTrue(all(r['association_owner'] in ('scripture_scope','allusion') for r in refs))

    def test_coverage_holds_partial_loss_even_after_another_quote_succeeds(self):
        for tail in ('“A second quotation” attributed somewhere in Luke 4:17.',
                     '<em>A second quotation</em> attributed somewhere in Luke 4:17.',
                     '“A second quotation” Ro. 4:17.'):
            src = source(markup=f'<p>“{VERSE}” (John 4:16). {tail}</p>')
            with self.subTest(tail=tail),self.assertRaisesRegex(AssociationError,'marked material has unowned citations'):
                associate(src['html'],src['article']['id'])

    def test_plain_blockquote_supports_only_its_complete_tail_scope(self):
        for marker in ('John 4:16', '<em>John 4:16</em>'):
            src = source(markup=f'<blockquote><p>{VERSE}<br>{marker}</p></blockquote>')
            _,_,quotes,_ = associate(src['html'],src['article']['id'])
            self.assertEqual([q['source_quote'] for q in quotes],[VERSE])
        src = source(markup=f'<blockquote><p>{VERSE} (John 4:16) trailing prose</p></blockquote>')
        with self.assertRaises(AssociationError): associate(src['html'],src['article']['id'])

    def test_multilingual_attribution_uses_frozen_anchor_not_english_bridge(self):
        for tag, native, attribution, prefix in (
            ('de','Johannes','sagt','sagt uns, dass'),
            ('he','יוחנן','אומר','אומר לנו כי')):
            # Deliberately synthetic Hebrew/German words test only structural
            # proof. They are not Bible translations or provider provenance.
            target = 'Synthetischer Zieltext nur für diesen Test.' if tag == 'de' else 'טקסט יעד סינתטי לבדיקת גבולות בלבד.'
            class SyntheticTranslations:
                def chapter(self, abbreviation, book, chapter):
                    return {'fixture_kind':'synthetic_not_provider_provenance', 'result':{
                        'cache':{'cacheable':True,'expires_at':'2099-01-01T00:00:00Z'},
                        'data':{'book_name':'John' if abbreviation=='kjv' else native,
                                'verses':[{'verse':16,'text':VERSE if abbreviation=='kjv' else target}]}}}
            for side in ('before','after'):
                english = (f'John 4:16 tells us that “{VERSE}”' if side=='before' else f'“{VERSE}” says John 4:16.')
                translated = (f'{native} 4:16 {prefix} “{target}”' if side=='before' else f'“{target}” {attribution} {native} 4:16.')
                src = source(markup=f'<p>{english}</p>')
                evidence = build_evidence(src,tag,policy(ROOT),SyntheticTranslations())
                candidate = {'html':source(markup=f'<p>{translated}</p>')['html']}
                start = translated.index(target)
                claims = [{'quote_id':'q1','block':'/article[1]/p[1]','start':start,'end':start+len(target),
                           'fragments':[[{'verse':16,'start':0,'end':len(target)}]]}]
                with self.subTest(language=tag,side=side):
                    self.assertTrue(check_selections(evidence,candidate,claims,normalization_version='2',source=src))
                    changed = {'html':candidate['html'].replace('4:16','4:15')}
                    with self.assertRaises(ScriptureAttention):
                        check_selections(evidence,changed,claims,normalization_version='2',source=src)


if __name__ == '__main__': unittest.main()
