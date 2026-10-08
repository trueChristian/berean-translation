"""Differential and fail-closed tests for opt-in component association v3.

All chapters here are small synthetic controls or existing offline fixtures.
Authentic Jacques envelope replay is a separate, recorded offline audit.
"""
import copy
import itertools
import random
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from berean_translation.common import ContractError, canonical, json_hash
from berean_translation.scripture_association import associate
from berean_translation.scripture_component_evidence import (
    component_policy, validate_component_evidence)
from berean_translation.scripture_evidence import (
    MAX_UNCLAIMED_ALIGNMENT_WORK, ScriptureAttention, _unique_alignment,
    build_evidence, check_selections, frozen_policy, policy)
from berean_translation.scripture_matching import Budget, Chapter, Pattern, Quote, check_backstop
from berean_translation.scripture_provider import GetBibleMCP
from test_scripture_component_evidence import example
from test_scripture_evidence import FakeMCP, fixture, source

ROOT = Path(__file__).resolve().parents[1]


def align(quote, verses):
    budget = Budget(MAX_UNCLAIMED_ALIGNMENT_WORK)
    chapter = Chapter(verses, budget)
    result = Quote(quote, budget).alignment(chapter, budget)
    return result, budget


class ExactAlignmentTests(unittest.TestCase):
    def assert_equivalent(self, quote, verses):
        before = canonical([quote, verses])
        expected = _unique_alignment(quote, verses)
        actual, budget = align(quote, verses)
        self.assertEqual(actual, expected, (quote, verses))
        self.assertEqual(budget.work, sum(budget.charges.values()))
        self.assertLessEqual(budget.work, MAX_UNCLAIMED_ALIGNMENT_WORK)
        self.assertEqual(canonical([quote, verses]), before)

    def test_exhaustive_small_repeated_corpora_and_verse_boundaries(self):
        sequences = [tuple(x) for length in range(5)
                     for x in itertools.product(('a', 'b'), repeat=length)]
        for corpus in sequences:
            for boundary in range(len(corpus) + 1):
                verses = [{'verse': 2, 'text': ' '.join(corpus[:boundary])},
                          {'verse': 7, 'text': ' '.join(corpus[boundary:])}]
                for quote in sequences:
                    self.assert_equivalent(' '.join(quote), verses)

    def test_seeded_differential_punctuation_apostrophes_and_ellipsis(self):
        rng = random.Random(86031)
        words = ['a', 'b', 'c', "Man's", 'MAN’S', '24', 'déjà', 'α', 'word-word', "'tis"]
        for _ in range(1500):
            verses = [{'verse': n + 1, 'text': ' '.join(rng.choices(words, k=rng.randrange(9)))}
                      for n in range(rng.randrange(1, 5))]
            if rng.randrange(2):
                quote = rng.choice(verses)['text']
            else:
                pieces = [' '.join(rng.choices(words, k=rng.randrange(6)))
                          for _ in range(rng.randrange(1, 4))]
                quote = rng.choice(['…', '...', '. . .', '.\n.\t.', '....']).join(pieces)
            quote = rng.choice(['', '… ', '  ']) + quote + rng.choice(['', ' …', '!?  '])
            self.assert_equivalent(quote, verses)

    def test_unique_fragments_are_global_ordered_and_nonoverlapping(self):
        verses = [{'verse': 4, 'text': 'one two three four five six one two three'}]
        for quote in ('one two three … four five six', 'four five six … one two three',
                      'two three four … three four five', 'five six one … two three four',
                      'two three four … five six one', '… two three four … five six one …'):
            self.assert_equivalent(quote, verses)
        self.assertIsNone(align('one two three … four five six', verses)[0])
        self.assertIsNone(align('two three four … three four five', verses)[0])
        self.assertIsNotNone(align('two three four … five six one', verses)[0])

    def test_complete_flags_exact_coordinates_and_short_whole_verse_exception(self):
        verses = [{'verse': 7, 'text': '  Jesus WEPT! '},
                  {'verse': 9, 'text': "  One man’s word, two three. "}]
        for quote in ('Jesus wept', "man's word", "ONE MAN'S WORD", 'Jesus wept … two three',
                      '… Jesus wept …', 'Jesus wept! One man’s word, two three.'):
            self.assert_equivalent(quote, verses)
        self.assertIsNotNone(align('Jesus wept', verses)[0])
        self.assertIsNone(align("man's word", verses)[0])
        result, _ = align("ONE MAN'S WORD", verses)
        self.assertEqual(result['fragments'][0]['start'], {'verse': 9, 'start': 2, 'end': 5})
        self.assertEqual(result['fragments'][0]['source_text'], "ONE MAN'S WORD")
        self.assertEqual(align('Jesus wept! One man’s word, two three.', verses)[0]['kind'], 'complete')
        self.assertEqual(align('… Jesus wept! One man’s word, two three.', verses)[0]['kind'], 'partial')

    def test_kmp_lists_overlapping_matches_and_long_repeated_prefixes(self):
        budget = Budget(MAX_UNCLAIMED_ALIGNMENT_WORK)
        self.assertEqual(list(Pattern(['a', 'a', 'a'], budget).positions(['a'] * 5, budget)), [0, 1, 2])
        verses = [{'verse': 1, 'text': 'one ' * 1000 + 'two'}]
        self.assert_equivalent('one ' * 80 + 'three', verses)
        self.assert_equivalent('one ' * 80 + 'two', verses)


class MatcherBoundTests(unittest.TestCase):
    def test_normalization_is_rejected_before_regex_or_allocation(self):
        budget = Budget(MAX_UNCLAIMED_ALIGNMENT_WORK)
        with patch('berean_translation.scripture_matching.WORDS') as words:
            with self.assertRaisesRegex(ScriptureAttention, 'source_association_limit'):
                budget.tokens('a' * 400_000)
            words.finditer.assert_not_called()
        self.assertEqual(budget.work, 0)
        self.assertEqual(budget.memory, 0)

    def test_memory_is_bounded_before_tokenization(self):
        budget = Budget(MAX_UNCLAIMED_ALIGNMENT_WORK, memory_limit=8)
        with patch('berean_translation.scripture_matching.WORDS') as words:
            with self.assertRaisesRegex(ScriptureAttention, 'memory bound'):
                budget.tokens('abc')
            words.finditer.assert_not_called()
        self.assertLessEqual(budget.memory, budget.memory_limit)

    def test_comparisons_stop_without_spending_past_the_bound(self):
        budget = Budget(MAX_UNCLAIMED_ALIGNMENT_WORK)
        pattern = Pattern(['a', 'a', 'b'], budget)
        budget.maximum = budget.work + 30
        with self.assertRaisesRegex(ScriptureAttention, 'work bound'):
            list(pattern.positions(['a'] * 100, budget))
        self.assertLessEqual(budget.work, budget.maximum)

    def test_exact_budget_boundary_and_long_token_comparison_are_charged(self):
        baseline = Budget(10_000)
        self.assertFalse(baseline.equal('a' * 99 + 'b', 'a' * 100))
        self.assertEqual(baseline.work, 101)
        self.assertFalse(Budget(101).equal('a' * 99 + 'b', 'a' * 100))
        with self.assertRaisesRegex(ScriptureAttention, 'work bound'):
            Budget(100).equal('a' * 99 + 'b', 'a' * 100)


class ComponentMatcherVersionTests(unittest.TestCase):
    def test_only_explicit_component_version_three_opts_in(self):
        src, default, _, _ = example()
        self.assertEqual(policy(ROOT)['source_association_version'], '2')
        self.assertEqual(default['source_association_version'], '2')
        explicit = component_policy(ROOT, src, default['authored_components']['plans'],
                                    source_association_version='3')
        self.assertEqual(frozen_policy({'scripture_quotes': explicit}), explicit)
        legacy = {**policy(ROOT), 'source_association_version': '3'}
        with self.assertRaises(ContractError):
            frozen_policy({'scripture_quotes': legacy})
        with self.assertRaisesRegex(ScriptureAttention, 'unknown_contract'):
            build_evidence(src, 'de', legacy, GetBibleMCP(FakeMCP()))
        for version in (None, '1', '4', 3):
            with self.subTest(version=version), self.assertRaises(ContractError):
                component_policy(ROOT, src, default['authored_components']['plans'],
                                 source_association_version=version)

    def test_old_component_evidence_is_identical_and_new_replays_with_scope_proof(self):
        src, old_contract, candidate, selections = example()
        with patch('berean_translation.scripture_matching.check_backstop', side_effect=AssertionError('v2 changed')):
            old = build_evidence(src, 'de', old_contract, GetBibleMCP(FakeMCP()))
        self.assertNotIn('source_association_work', old)
        new_contract = {**old_contract, 'source_association_version': '3'}
        cached = SimpleNamespace(chapter=lambda edition, book, chapter:
            copy.deepcopy(old['lookups'][f'{edition}/{book}/{chapter}']))
        new = build_evidence(src, 'de', new_contract, cached)
        comparable = copy.deepcopy(new)
        comparable.pop('source_association_work')
        comparable.update(created_at=old['created_at'], source_association_version='2',
                          component_contract_sha256=old['component_contract_sha256'])
        self.assertEqual(comparable, old)
        with patch.object(GetBibleMCP, '_remote', side_effect=AssertionError('offline replay only')):
            self.assertEqual(validate_component_evidence(src, new, new_contract), new)
            proof = check_selections(new, candidate, selections, source=src, component_contract=new_contract)
        self.assertEqual(proof['candidate_sha256'], json_hash(candidate))
        moved = copy.deepcopy(candidate)
        moved['html'] = moved['html'].replace('John 4:16', 'John 4:15')
        with self.assertRaises(ContractError):
            check_selections(new, moved, selections, source=src, component_contract=new_contract)

    def test_work_audit_and_version_cannot_be_forged_during_replay(self):
        src, contract, _, _ = example()
        contract['source_association_version'] = '3'
        evidence = build_evidence(src, 'de', contract, GetBibleMCP(FakeMCP()))
        for change in ('work', 'version', 'remove'):
            changed = copy.deepcopy(evidence)
            if change == 'work': changed['source_association_work']['work_used'] -= 1
            elif change == 'version': changed['source_association_version'] = '2'
            else: changed.pop('source_association_work')
            with self.subTest(change=change), self.assertRaisesRegex(ScriptureAttention, 'component_evidence_changed'):
                validate_component_evidence(src, changed, contract)

    def test_existing_cited_and_uncited_fail_closed_checks_still_run(self):
        verse = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
        cases = [
            (f'<p>Our discussion repeats {verse} This is helpful as we read in John 4:16.</p>',
             'unmarked_quote_scope'),
            ('<p>We read in John 4:16.</p><p>A person said “Go, call thy husband”.</p>',
             'unassociated_source_quote'),
            ('<p>We read in John 4:16.</p><p><em>Sir, give me this water</em></p>',
             'unassociated_source_quote'),
        ]
        for markup, reason in cases:
            src = source(markup=markup)
            for version in ('2', '3'):
                contract = component_policy(ROOT, src, [], source_association_version=version)
                with self.subTest(markup=markup, version=version), self.assertRaisesRegex(ScriptureAttention, reason):
                    build_evidence(src, 'de', contract, GetBibleMCP(FakeMCP()))

    def test_cited_coverage_checks_every_occurrence_including_overlaps(self):
        block = '/article[1]/p[1]'
        ref = {'block': block, 'identity': 'John 4:16'}
        verses = [{'verse': 16, 'text': 'One two one.'}]
        # The first occurrence is proved, but an overlapping second occurrence
        # is not. KMP must preserve the old sliding-window coverage hold.
        parsed = SimpleNamespace(blocks={block: 'One two one two one.'})
        scopes = [{'block': block, 'source_start': 0, 'source_end': 11}]
        with self.assertRaisesRegex(ScriptureAttention, 'unmarked_quote_scope'):
            check_backstop(parsed, [ref, ref], scopes, [('kjv/43/4', verses)],
                           lambda _: (43, 4, [16]), [], maximum=MAX_UNCLAIMED_ALIGNMENT_WORK)
        scopes[0]['source_end'] = len(parsed.blocks[block])
        audit = check_backstop(parsed, [ref, ref], scopes, [('kjv/43/4', verses)],
                               lambda _: (43, 4, [16]), [], maximum=MAX_UNCLAIMED_ALIGNMENT_WORK)
        self.assertEqual(audit['cited_checks'], 1)
        self.assertEqual(audit['charges']['matches'], 2)

    def test_overbound_component_v3_never_falls_back_or_skips_checks(self):
        src, contract, _, _ = example()
        contract['source_association_version'] = '3'
        with patch('berean_translation.scripture_evidence.MAX_UNCLAIMED_ALIGNMENT_WORK', 1):
            with self.assertRaisesRegex(ScriptureAttention, 'source_association_limit'):
                build_evidence(src, 'de', contract, GetBibleMCP(FakeMCP()))


if __name__ == '__main__':
    unittest.main()
