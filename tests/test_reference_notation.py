"""Offline tests for citation-scoped, language-qualified notation equivalence."""
import json
from collections import Counter
from pathlib import Path
import unittest

from berean_translation.reference_notation import reference_mentions


class ReferenceNotationTests(unittest.TestCase):
    def references(self, text, language='eng', target_language=None):
        return Counter(key for _, _, key in reference_mentions(
            text, language, target_language=target_language))

    def assert_equivalent(self, source, candidate, language):
        self.assertEqual(self.references(source, target_language=language),
                         self.references(candidate, language))

    def assert_changed(self, source, candidate, language):
        self.assertNotEqual(self.references(source, target_language=language),
                            self.references(candidate, language))

    def test_authentic_and_synthetic_fixture_cases(self):
        path = Path(__file__).parent / 'fixtures' / 'localized_reference_cases.json'
        for case in json.loads(path.read_text())['cases']:
            with self.subTest(case=case['id']):
                left = self.references(case['source'], target_language=case['language'])
                right = self.references(case['candidate'], case['language'])
                self.assertEqual(left == right, case['expected_equal'])

    def test_german_qualified_comma_colon_and_book_spacing(self):
        for source, target in (
            ('Genesis 8:22', '1.Mose8,22'),
            ('Genesis8:22', '1. Mose 8:22'),
            ('Matthew 24:6-7', 'Matthäus24,6–7'),
            ('1 Corinthians7:10-11', '1. Korinther 7,10–11'),
            ('Deuteronomy 24:1-4', '5.Mose24,1-4'),
            ('Psalms 139:13-15', 'Psalm139,13–15'),
            ('Matthew 1:18', 'MATTHÄUS 1,18'),
        ):
            with self.subTest(target=target):
                self.assert_equivalent(source, target, 'deu')

    def test_source_paired_existing_publication_aliases(self):
        cases = (
            ('1 Corinthians7:5', '1 Korinther7:5', 'deu'),
            ('2 Corinthians4:17-18', '2 Korinther4:17-18', 'deu'),
            ('1 Thessalonians4:13-14', '1 Thessalonicher4:13-14', 'deu'),
            ('2 Timothy1:7', '2 Timotheus1:7', 'deu'),
            ('Deuteronomy24:1-4', 'Deuteronomium24:1-4', 'deu'),
            ('1 Corinthians7:5', 'א׳ קורינתיים 7:5', 'heb'),
            ('1 Timothy1:7', 'א׳ טימותאוס 1:7', 'heb'),
            ('2 Timothy1:7', 'ב׳ טימותאוס 1:7', 'heb'),
            ('2 Timothy1:7', '2 טימותיוס 1:7', 'heb'),
            ('1 Corinthians7:5', '1 קורינתים 7:5', 'heb'),
        )
        for source, target, language in cases:
            with self.subTest(target=target):
                self.assert_equivalent(source, target, language)
        self.assert_changed('1 Timothy1:7', 'ב׳ טימותאוס 1:7', 'heb')

    def test_english_book_names_can_remain_in_localized_citations(self):
        for language in ('deu', 'heb'):
            for source in ('John3:16', 'Matthew 1:18,19', 'Psalm 20:7b'):
                with self.subTest(language=language, source=source):
                    self.assert_equivalent(source, source, language)

    def test_unsupported_books_keep_existing_number_protection(self):
        self.assert_equivalent('Daniel 3:4', 'דניאל 3:4', 'heb')
        self.assert_equivalent('Daniel 3:4', 'Daniel 3:4', 'heb')
        self.assert_changed('Daniel 3:4', 'דניאל 3:5', 'heb')
        self.assert_changed('Matthew 3:4', 'דניאל 3:4', 'heb')

    def test_hebrew_strict_canonical_letter_numbers(self):
        examples = ((1, 'א׳'), (8, 'ח׳'), (11, 'י״א'), (14, 'י״ד'),
                    (15, 'ט״ו'), (16, 'ט״ז'), (22, 'כ״ב'), (99, 'צ״ט'),
                    (115, 'קט״ו'), (116, 'קט״ז'), (139, 'קל״ט'),
                    (400, 'ת׳'), (500, 'ת״ק'), (999, 'תתקצ״ט'))
        for number, hebrew in examples:
            with self.subTest(number=number):
                self.assert_equivalent(f'Psalm {number}:22', f'תהילים {hebrew}:כ״ב', 'heb')
                self.assert_equivalent(f'Psalm 1:{number}', f'תהילים א׳:{hebrew}', 'heb')
        self.assert_equivalent('Genesis 8:22', 'בראשית ח\':כ"ב', 'heb')
        self.assert_equivalent('Psalm 139:13-15', 'ותהילים קל״ט:13-15', 'heb')

    def test_hebrew_mixed_decimal_letter_verses_and_subverses(self):
        self.assert_equivalent('Job 14:5', 'איוב י״ד:5', 'heb')
        self.assert_equivalent('Genesis 8:22', 'בראשית 8:כ״ב', 'heb')
        self.assert_equivalent('Psalm 20:7a; Psalm 20:7b', 'תהילים כ׳:ז׳א; תהילים כ׳:ז׳ב', 'heb')
        self.assert_equivalent('Psalm 20:22a', 'תהילים כ׳:כ״בא', 'heb')
        self.assert_equivalent('Psalm 20:7a-7b', 'תהילים כ׳:ז׳א-ז׳ב', 'heb')
        self.assert_changed('Psalm 20:7a', 'תהילים כ׳:ז׳ב', 'heb')
        self.assert_changed('Psalm 20:7a', 'תהילים כ׳:ז׳', 'heb')

    def test_hebrew_postpositive_ordinal_books(self):
        for source, target in (
            ('2 Timothy1:7', 'טימותיאוס ב׳ א׳:7'),
            ('1 Thessalonians4:13-14', 'תסלוניקים א׳ ד׳:13–14'),
            ('2 Corinthians4:17-18', 'קורינתים ב׳ ד׳:17-18'),
        ):
            with self.subTest(target=target):
                self.assert_equivalent(source, target, 'heb')
        self.assert_changed('1 Timothy1:7', 'טימותיאוס ב׳ א׳:7', 'heb')

    def test_hebrew_colonless_forms_require_explicit_book_and_quoted_chapter(self):
        self.assert_equivalent('John 11:4', 'יוחנן י״א 4', 'heb')
        self.assert_equivalent('Matthew 9:2-8', 'מתי ט׳ 2–8', 'heb')
        self.assert_changed('John 11:4', 'יוחנן 11 4', 'heb')
        self.assert_changed('John 11:4', 'יוחנן יא 4', 'heb')
        self.assert_changed('John 11:4', 'י״א 4', 'heb')
        self.assertEqual(self.references('11 4, י״א words 4, ordinary Hebrew שלום עולם', 'heb'), Counter())

    def test_hebrew_ordinary_words_are_not_number_tokens(self):
        self.assertEqual(self.references('יוחנן אמר: כן. מתי יבוא: מחר.', 'heb'), Counter())
        self.assert_changed('John 11:4', 'יוחנן יא:ד', 'heb')
        self.assert_changed('John 11:4', 'יוחנן יא:4', 'heb')

    def test_hebrew_redundant_chapter_requires_exact_consistency(self):
        self.assert_equivalent('Matthew 5:14', 'מתי ה׳ 5:14', 'heb')
        for target in ('מתי ו׳ 5:14', 'מתי ה׳ 6:14', 'מתי ה׳ 0:14',
                       'מתי ה׳ 9999:14', 'מתי ה׳ 5a:14'):
            with self.subTest(target=target):
                self.assert_changed('Matthew 5:14', target, 'heb')
                self.assertTrue(any(key.startswith('!invalid[heb]') for key in self.references(target, 'heb')))

    def test_malformed_hebrew_numerals_never_disappear(self):
        for malformed in ('י״ה', 'י״ו', 'א״י', 'י׳ד', 'יד', 'ן׳',
                          'י״ד״', 'ז׳אב', 'תתת׳'):
            with self.subTest(token=malformed):
                target = f'מתי א׳:{malformed}'
                self.assert_changed('Matthew 1:14', target, 'heb')
                self.assertTrue(any(key.startswith('!invalid[heb]') for key in self.references(target, 'heb')))
        self.assert_changed('', 'א״י:ד׳', 'heb')
        self.assert_changed('Genesis 8:22', 'בראשית ח:כב', 'heb')

    def test_adjacent_lists_equal_ranges_without_expanding_them(self):
        for source, target in (
            ('Matthew1:18,19', 'Matthäus1,18-19'),
            ('Matthew1:18, 19, 20', 'Matthäus1,18-20'),
            ('Matthew1:18-19,20', 'Matthäus1,18-20'),
            ('Matthew1:18,19-20', 'Matthäus1,18-20'),
            ('Matthew1:18,20,21', 'Matthäus1,18,20-21'),
        ):
            with self.subTest(source=source):
                self.assert_equivalent(source, target, 'deu')
        self.assertEqual(self.references('John 1:1-999'), Counter({'John 1:1-999': 1}))

    def test_lists_cannot_expand_fill_gaps_drop_duplicates_or_merge_citations(self):
        for source, target in (
            ('Matthew1:18', 'Matthäus1,18-19'),
            ('Matthew1:18,20', 'Matthäus1,18-20'),
            ('Matthew1:18,18,19', 'Matthäus1,18-19'),
            ('Matthew1:18-20,19-21', 'Matthäus1,18-21'),
            ('Matthew1:19,18', 'Matthäus1,18-19'),
            ('Matthew1:18; Matthew1:19', 'Matthäus1,18-19'),
        ):
            with self.subTest(source=source):
                self.assert_changed(source, target, 'deu')

    def test_reversed_huge_and_malformed_range_tails_are_invalid(self):
        for target in ('Matthäus1,18-17', 'Matthäus1,18-99999999999999999999',
                       'Matthäus1,18-', 'Matthäus1,18--19', 'Matthäus1,18-abc',
                       'Matthäus1,18,,19', 'Matthäus1,18.19',
                       'Matthäus1,18-2:19', 'Matthäus1,18xyz'):
            with self.subTest(target=target):
                self.assert_changed('Matthew1:18', target, 'deu')
                self.assertTrue(any(key.startswith('!invalid[deu]') for key in self.references(target, 'deu')))
        huge = '9' * 5000
        self.assertTrue(self.references(f'John 1:1-{huge}'))

    def test_occurrences_and_book_identity_are_preserved(self):
        self.assert_changed('John3:16 John3:16', 'Johannes3,16', 'deu')
        self.assert_changed('John3:16', 'Johannes3,16 Johannes3,16', 'deu')
        self.assert_changed('John3:16', 'Matthäus3,16', 'deu')
        self.assert_changed('John3:16 Matthew3:17', 'Johannes3,17 Matthäus3,16', 'deu')
        self.assert_changed('John3:16', 'מתי ג׳:16', 'heb')
        self.assert_changed('John3:16', 'יוחנן ג׳:17', 'heb')

    def test_counters_are_intended_for_each_block_not_whole_document(self):
        source_blocks = ['John3:16', 'Matthew3:17']
        target_blocks = ['Matthäus3,17', 'Johannes3,16']
        self.assert_equivalent(' '.join(source_blocks), ' '.join(target_blocks), 'deu')
        for source, target in zip(source_blocks, target_blocks):
            self.assert_changed(source, target, 'deu')

    def test_german_bare_commas_require_complete_citation_parentheses(self):
        self.assert_equivalent('(2:7), (2:14), (2:19-20), (2:20), (3:3)',
                               '(2,7), (2,14), (2,19–20), (2,20), (3,3)', 'deu')
        self.assertEqual(self.references('3,9% and 7.595, 2,7; (3,9%); (cost 2,7)', 'deu'), Counter())
        self.assert_changed('(2:7)', '2,7', 'deu')
        self.assert_changed('(2:7)', '(2,8)', 'deu')
        self.assert_changed('', '(2,7)', 'deu')

    def test_language_is_explicit_not_guessed_from_prose(self):
        self.assertEqual(self.references('Matthäus1,18', 'eng'), Counter())
        self.assertEqual(self.references('בראשית ח׳:כ״ב', 'eng'), Counter())
        self.assert_changed('Genesis8:22', 'בראשית ח׳:כ״ב', 'deu')
        self.assert_changed('Matthew1:18', 'Matthäus1,18', 'heb')

    def test_decimal_colon_fallback_and_clock_spans_remain_available(self):
        self.assertEqual(self.references('UnknownBook8:22 and bare 3:16'),
                         Counter({'8:22': 1, '3:16': 1}))
        text = 'At 4:30 p.m., then John3:16.'
        mentions = reference_mentions(text, 'eng', target_language='deu')
        self.assertEqual([(text[a:b], key) for a, b, key in mentions],
                         [('4:30', '4:30'), ('John3:16', 'John 3:16')])
        # This parser never exempts a clock without the source-backed caller.
        self.assertEqual(self.references('um 16:30 Uhr', 'deu'), Counter({'16:30': 1}))
        self.assertEqual(self.references('4:30-31'), Counter({'4:30-31': 1}))

    def test_numbered_john_identity_cannot_match_the_gospel_suffix(self):
        for language, localized in (('deu', 'Johannes'), ('heb', 'יוחנן')):
            for number in (1, 2, 3):
                numbered = f'{number}. {localized}' if language == 'deu' else f'{number} {localized}'
                with self.subTest(language=language, number=number):
                    self.assert_changed('John 3:16', f'{numbered} 3:16', language)
                    self.assert_changed(f'{number} John 3:16', f'{localized} 3:16', language)
                    self.assert_equivalent(f'{number} John 1:1', f'{numbered} 1:1', language)
                    other = number % 3 + 1
                    self.assert_changed(f'{other} John 1:1', f'{numbered} 1:1', language)
            self.assert_changed('John 3:16', f'4. {localized} 3:16', language)
            self.assert_changed('John 3:16', f'IV {localized} 3:16', language)
        for target in ('1.Johannes1:1', '1Johannes1:1', '1 Johannes 1:1'):
            self.assert_equivalent('1John1:1', target, 'deu')
        self.assert_equivalent('John 1:4', 'יוחנן א׳:4', 'heb')
        self.assert_changed('1 John 1:4', 'יוחנן א׳:4', 'heb')
        self.assert_changed('John 1:4', 'יוחנן א׳ 4', 'heb')
        self.assert_changed('John 1:4', 'יוחנן א׳ 1:4', 'heb')
        self.assert_changed('John 3:16', 'ב יוחנן 3:16', 'heb')
        self.assert_equivalent('1John4:19,20', '1John4:19,20', 'heb')
        # A preceding citation's verse is not an ordinal attached to the book.
        self.assert_equivalent('Genesis 1:3 John 3:16', '1. Mose 1:3 Johannes 3:16', 'deu')

    def test_bare_german_amounts_outside_parentheses_are_not_citations(self):
        for target in ('(2,14) Euro', '(2,14)%', '(2,14) EUR', '(2,14) €',
                       '(2,14) Prozent', '(2,14) kg', '€ (2,14)',
                       'USD (2,14)', 'Preis (2,14)', '(2,14) ₹'):
            with self.subTest(target=target):
                self.assert_changed('(2:14)', target, 'deu')
                self.assertEqual(self.references(target, 'deu'), Counter())
        self.assert_equivalent('(2:14) He said', '(2,14) Er sagte', 'deu')

    def test_unmarked_bare_zero_minute_clocks_are_exactly_protected(self):
        for language in ('deu', 'heb'):
            for clock in ('9:00', '09:00', '0:00', '00:00', '23:00', '9:01'):
                with self.subTest(language=language, clock=clock):
                    self.assert_equivalent(f'Meet at {clock}.', f'Treffen um {clock}.', language)
            self.assert_changed('Meet at 9:00.', 'Treffen um 9:01.', language)
            self.assert_changed('Meet at 9:00.', 'Treffen um 10:00.', language)
        self.assert_changed('John 9:00', 'Johannes 9:00', 'deu')
        self.assert_changed('John 9:0', 'Johannes 9:0', 'deu')
        self.assert_changed('9:00', '(9,00)', 'deu')

    def test_unsupported_numeric_continuations_never_truncate_to_a_valid_prefix(self):
        for tail in ('/19', '+19', ' + 19', '//19', '=19', '*19', '~19',
                     '^19', '|19', '⁄19', '∕19', '＋19', '.19', ';19', '; 19', '&19', '·19', '／19', '⋅19', '×19', '÷19', '≤19', '⸺19'):
            with self.subTest(tail=tail):
                target = f'Matthäus 1,18{tail}'
                self.assert_changed('Matthew 1:18', target, 'deu')
                self.assertTrue(any(key.startswith('!invalid[deu]') for key in self.references(target, 'deu')))
        self.assert_changed('Matthew1:18', 'Matthäus1,18−19', 'deu')
        self.assert_changed('Matthew1:18', 'מתי א׳:י״ח־י״ט', 'heb')
        self.assert_equivalent('Matthew1:18-19', 'Matthäus1,18−19', 'deu')
        self.assert_equivalent('Matthew1:18-19', 'מתי א׳:י״ח־י״ט', 'heb')
        self.assert_equivalent('Matthew1:18.', 'Matthäus1,18.', 'deu')
        self.assert_equivalent('Matthew1:18; John3:16', 'Matthäus1,18; Johannes3:16', 'deu')
        self.assert_equivalent('Matthew1:18; 2:3', 'Matthäus1,18; 2:3', 'deu')
        self.assert_equivalent('Romans14:17; 1 Corinthians1:2', 'Römer14,17; 1. Korinther1,2', 'deu')
        self.assert_equivalent('Matthew1:18; 1 Peter1:2', 'Matthäus1,18; 1. Petrus1:2', 'deu')
        self.assert_equivalent('John1:18; 1 Peter1:2', 'יוחנן א׳:18; 1 פטרוס א׳:2', 'heb')
        self.assert_equivalent('Matthew1:18. 19 people', 'Matthäus1,18. 19 Menschen', 'deu')
        self.assert_changed('Matthew1:18', 'מתי א׳:י״ח/י״ט', 'heb')
        self.assert_changed('Matthew1:18', 'מתי א׳:י״ח;י״ט', 'heb')

    def test_unicode_digits_and_typographic_dashes_preserve_input_offsets(self):
        text = '前置 John ٣:١٦–١٨; Psalm 20:7a.'
        mentions = reference_mentions(text, 'eng')
        self.assertEqual([(text[a:b], key) for a, b, key in mentions],
                         [('John ٣:١٦–١٨', 'John 3:16-18'), ('Psalm 20:7a', 'Psalm 20:7a')])


if __name__ == '__main__':
    unittest.main()
