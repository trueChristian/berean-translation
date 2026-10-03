"""Finite book identities and adversarial punctuation/ordinal boundaries."""
from collections import Counter
import unittest

from berean_translation.reference_notation import reference_mentions
from berean_translation.scripture_books import CORE_BOOK_NAMES


def refs(text, language='eng', target='heb'):
    return Counter(key for _, _, key in reference_mentions(text, language,
        target_language=target if language == 'eng' else None))


class ScriptureBookCatalogTests(unittest.TestCase):
    def test_all_66_literal_books_and_osis_codes_preserve_exact_identity(self):
        self.assertEqual(len(CORE_BOOK_NAMES), 66)
        self.assertEqual(len({row[0] for row in CORE_BOOK_NAMES}), 66)
        self.assertEqual(len({row[3] for row in CORE_BOOK_NAMES}), 66)
        for english, german, hebrew, osis in CORE_BOOK_NAMES:
            expected = Counter({english + ' 1:1': 1})
            for language, literal in (('deu', german), ('heb', hebrew)):
                with self.subTest(language=language, book=english):
                    self.assertEqual(refs(english + ' 1:1', target=language), expected)
                    self.assertEqual(refs(literal + ' 1:1', language), expected)
                    self.assertEqual(refs(osis + ' 1:1', target=language), expected)
                    self.assertEqual(refs(osis + '. 1:1', target=language), expected)
                    if osis[0] in '123':
                        self.assertEqual(refs(osis[0] + ' ' + osis[1:] + '. 1:1', target=language), expected)

    def test_verified_roman_numbered_forms_keep_their_epistle_identity(self):
        for english, _, _, osis in CORE_BOOK_NAMES:
            if english[0] not in '123':
                continue
            roman = {'1': 'I', '2': 'II', '3': 'III'}[english[0]]
            expected = Counter({english + ' 1:1': 1})
            for stem in (english[2:], osis[1:]):
                for separator in (' ', '. '):
                    with self.subTest(book=english, stem=stem, separator=separator):
                        self.assertEqual(refs(roman + separator + stem + ' 1:1'), expected)

    def test_numbered_hebrew_ot_book_marks_are_part_of_the_alias(self):
        count = 0
        for english, _, hebrew, _ in CORE_BOOK_NAMES:
            if not hebrew.endswith((' א', ' ב')):
                continue
            count += 1
            for mark in ('', '׳', "'", '’', '‘'):
                with self.subTest(book=english, mark=mark):
                    self.assertEqual(refs(hebrew + mark + ' 3:16', 'heb'), Counter({english + ' 3:16': 1}))
            opposite = ('2' if english[0] == '1' else '1') + english[1:]
            self.assertNotEqual(refs(opposite + ' 3:16'), refs(hebrew + '׳ 3:16', 'heb'))
        self.assertEqual(count, 6)

    def test_unsupported_ordinal_prefixes_cannot_become_a_gospel_suffix(self):
        for language, target in (
            ('deu', '1st Johannes'), ('deu', '2nd Johannes'), ('deu', 'Dritter Johannes'),
            ('deu', 'fünfte Johannes'), ('deu', 'zehnten Johannes'),
            ('heb', 'השניה יוחנן'), ('heb', 'הרביעית יוחנן'), ('heb', 'החמישית יוחנן'),
            ('heb', 'העשירית אל יוחנן'), ('heb', 'Fourth John'), ('heb', 'Fifth John'),
        ):
            with self.subTest(target=target):
                self.assertNotEqual(refs('John 3:16', target=language), refs(target + ' 3:16', language))
        for prefix in ('First', 'Second', 'Third', 'Fourth', 'Fifth', '10th'):
            self.assertNotEqual(refs(prefix + ' John 3:16'), refs('יוחנן 3:16', 'heb'))

    def test_ordinary_short_quote_words_keep_standalone_label_numbers(self):
        for source, target, language in (
            ('I am the vine.', 'Ich bin der Weinstock.', 'deu'),
            ('A man came here.', 'Ein Mann kam hierher.', 'deu'),
            ('O Lord, help me.', 'O Herr, hilf mir.', 'deu'),
            ('For I am the vine.', 'כי אני הגפן.', 'heb'),
            ('This is the wine.', 'זה היין החדש.', 'heb'),
        ):
            candidate = ('Vers 5: „' + target + '“' if language == 'deu' else 'פסוק 5: ״' + target + '״')
            with self.subTest(source=source, target=target):
                self.assertEqual(refs('Verse 5, “' + source + '”', target=language), Counter({'verse-label 5': 1}))
                self.assertEqual(refs(candidate, language), Counter({'verse-label 5': 1}))
                self.assertNotEqual(refs('Verse 6, “' + source + '”', target=language), refs(candidate, language))

    def test_list_final_verse_never_disappears_before_a_quote(self):
        for first in ('I am the vine.', 'A man came here.', 'O Lord, help me.'):
            with self.subTest(first=first):
                source = 'Ephesians 5:22,24: “' + first + '”'
                self.assertEqual(refs(source, target='deu'), Counter({'Ephesians 5:22,24': 1}))
                self.assertNotEqual(refs(source, target='deu'), refs('Epheser 5:22: „Ich bin der Weinstock.“', 'deu'))
        for tail in ('"21" words here', '“IV” words here', '“unfinished', 'words', ''):
            self.assertNotEqual(refs('Ephesians 5:22,24: ' + tail, target='deu'),
                                refs('Epheser 5:22', 'deu'))

    def test_numeric_quoted_endpoints_cannot_vanish_after_single_or_range_references(self):
        for numbers in ('5:22', '5:22-24', '5:22,24'):
            for tail in ('"21" words here', '“21” words here', '״21״ נשים כאן',
                         '״ט״ז״ נשים כאן', '“IV” words here'):
                for language, book in (('deu', 'Epheser'), ('heb', 'אל האפסים')):
                    with self.subTest(language=language, numbers=numbers, tail=tail):
                        self.assertNotEqual(refs('Ephesians ' + numbers, target=language),
                                            refs(book + ' ' + numbers + ': ' + tail, language))

    def test_malformed_outer_and_quote_wrapped_nested_labels_are_not_ignored(self):
        source = refs('Verse 21, “Submit yourselves.”', target='deu')
        for target in ('Vers 0: Vers 21: „Ordnet euch einander unter.“',
                       'Vers 9999: Vers 21: „Ordnet euch einander unter.“',
                       'Vers 22: „Vers 21: „Ordnet euch einander unter.“',
                       'Vers 21: „Vers 21: „Ordnet euch einander unter.“'):
            with self.subTest(target=target):
                self.assertNotEqual(source, refs(target, 'deu'))

    def test_short_is_alias_requires_its_authentic_parenthesized_context(self):
        self.assertEqual(refs('(Is 65:14)'), Counter({'Isaiah 65:14': 1}))
        self.assertEqual(refs('(Is. 65:14)'), refs('(ישעיהו 65:14)', 'heb'))
        self.assertEqual(refs('The time is 9:00.'), refs('השעה 9:00.', 'heb'))
        self.assertEqual(refs('Is 9:00 the time?'), refs('השעה 9:00?', 'heb'))
        self.assertNotEqual(refs('(Is 65:14)'), refs('(ירמיהו 65:14)', 'heb'))
        self.assertNotEqual(refs('Is 65:14'), refs('ישעיהו 65:14', 'heb'))
