"""Complete pinned runtime candidates and narrowly delimited reference forms."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import unittest
from collections import Counter
from pathlib import Path

from berean_translation.common import ContractError
from berean_translation.html import Fragment, protected_reference_numbers, validate_translation
from berean_translation.reference_notation import reference_mentions
from support import REPO_ROOT
from test_localized_reference_lifecycle import documents


FIXTURE = REPO_ROOT / 'tests/fixtures/remaining_reference_cases.json'


class RemainingReferenceCasesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())

    def archive(self, item):
        archive = {}
        for kind, provenance in item['provenance'].items():
            path = Path(provenance['path'])
            self.assertFalse(path.is_absolute())
            self.assertNotIn('..', path.parts)
            raw = (REPO_ROOT / path).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance['sha256'])
            self.assertEqual(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
                             provenance['git_blob_sha'])
            self.assertEqual(provenance['url'],
                f'https://github.com/{self.fixture["repository"]}/blob/'
                f'{self.fixture["revision"]}/{path.as_posix()}')
            archive[kind] = json.loads(raw)
        task, source = archive['task'], archive['source']
        self.assertEqual(task['id'], item['task_id'])
        self.assertEqual(task['article_id'], item['article_id'])
        self.assertEqual(task['language'], item['language'])
        self.assertEqual(task['source_snapshot'], item['provenance']['source']['path'])
        self.assertEqual(source['article']['id'], item['article_id'])
        self.assertEqual(source['revision'], item['source_revision'])
        self.assertEqual((task['status'], task['stage']), ('not_ready', 'correct'))
        self.assertEqual((task['translation_attempts'], task['review_attempts']),
                         (item['translation_attempts'], item['review_attempts']))
        self.assertEqual(archive['candidate'], archive['correct']['result'])
        return archive

    def history(self):
        paths = set()
        for item in self.fixture['archives']:
            paths.update((REPO_ROOT / f'state/tasks/{item["task_id"]}').rglob('*'))
            paths.add(REPO_ROOT / item['provenance']['source']['path'])
        return {path: path.read_bytes() for path in paths if path.is_file()}

    def check(self, source, candidate, language='heb', equal=True):
        pair = documents('<p>' + html.escape(source) + '</p>',
                         '<p>' + html.escape(candidate) + '</p>')
        before = copy.deepcopy(pair)
        if equal:
            validate_translation(*pair, language=language)
        else:
            with self.assertRaises(ContractError):
                validate_translation(*pair, language=language)
        self.assertEqual(pair, before)

    def test_five_complete_corrections_keep_54_references_and_genuine_korean_error_fails(self):
        frozen = self.history()
        self.assertEqual(len(self.fixture['archives']), 6)
        positives = [item for item in self.fixture['archives'] if item['expected_equal']]
        self.assertEqual(sum(item['audited_reference_count'] for item in positives), 54)
        for item in self.fixture['archives']:
            with self.subTest(task=item['task_id']):
                archive = self.archive(item)
                before = copy.deepcopy(archive)
                if item['expected_equal']:
                    validate_translation(archive['source'], archive['correct']['result'],
                                         language=item['language'])
                else:
                    with self.assertRaisesRegex(ContractError, 'missing: 4:16; extra: 4:15-16'):
                        validate_translation(archive['source'], archive['correct']['result'],
                                             language=item['language'])
                source = Fragment(archive['source']['html'], item['article_id'])
                candidate = Fragment(archive['candidate']['html'], item['article_id'])
                counts, differences = [0, 0], []
                for path, parts in source.text_by_block.items():
                    counters = protected_reference_numbers(''.join(parts),
                        ''.join(candidate.text_by_block[path]), language=item['language'])
                    if counters[0] != counters[1]:
                        differences.append(path)
                    for side, counter in enumerate(counters):
                        counts[side] += sum(counter.values())
                self.assertEqual(counts, [item['audited_reference_count'] + item['audited_chapter_count']] * 2)
                self.assertEqual(differences, [] if item['expected_equal'] else ['/article[1]/p[18]'])
                self.assertEqual(archive, before)
                self.assertEqual(self.archive(item), before)
        # Recognition neither reopens held tasks nor creates a semantic review.
        self.assertEqual(self.history(), frozen)

    def test_ten_affected_blocks_are_exact_pinned_excerpts(self):
        archives = {item['task_id']: (item, self.archive(item)) for item in self.fixture['archives']}
        self.assertEqual(len(self.fixture['cases']), 10)
        for case in self.fixture['cases']:
            item, archive = archives[case['task_id']]
            with self.subTest(case=case['id']):
                source = Fragment(archive['source']['html'], item['article_id'])
                candidate = Fragment(archive['candidate']['html'], item['article_id'])
                self.assertEqual(case['source'], ''.join(source.text_by_block[case['block']]))
                self.assertEqual(case['candidate'], ''.join(candidate.text_by_block[case['block']]))
                self.check(case['source'], case['candidate'], item['language'], item['expected_equal'])
                counters = protected_reference_numbers(case['source'], case['candidate'],
                                                       language=item['language'])
                self.assertEqual(dict(counters[0]), case['expected_references'])
                self.assertEqual(counters[0] == counters[1], item['expected_equal'])

    def test_full_article_mutations_reject_identity_number_and_occurrence_changes(self):
        replacements = {
            'ba0cba0a5b0898d4a20a228d588147ff': ('Joh 7:38', (
                'Jes 7:38', 'Joh 8:38', 'Joh 7:39', '1 Joh 7:38',
                'Joh 7:38,39', 'Joh 7:38-39', 'Joh 7:38; Joh 7:38', '')),
            '5224509e3fb7a3e590ad484919643fe2': ('Römer 13, den', (
                'Römer 14, den', 'Matthäus 13, den', '1. Petrus 13, den',
                'Römer 13, den höheren Gewalten. Römer 13, den', 'den',
                'Römer 13:14, den', 'Römer 13.14, den', 'Römer 13/14, den',
                'Römer 13, den höheren Gewalten. 4 Römer 14, den',
                'Römer 13, den höheren Gewalten. IV Römer 14, den',
                'Römer 13, den höheren Gewalten. vierte Römer 14, den')),
            '14c60ac21c173af352ecc70847aba6f3': ('הראשונה ליוחנן ה׳:ג׳', (
                'יוחנן ה׳:ג׳', '2 יוחנן ה׳:ג׳', 'הראשונה ליוחנן ו׳:ג׳',
                'הראשונה ליוחנן ה׳:ד׳', 'הראשונה ליוחנן ה׳:ג׳,ד׳',
                'הראשונה ליוחנן ה׳:ג׳; הראשונה ליוחנן ה׳:ג׳', '')),
            '446fe5b3a410b09f0c97f0c90d60034a': ('משלי טז:יח', (
                'תהילים טז:יח', 'משלי יז:יח', 'משלי טז:יט', 'משלי טז:יח–יט',
                'משלי טז:יח,יט', 'משלי טז:יח. — משלי טז:יח', '',
                'משלי טז:יח. — 4 משלי טז:יח', 'משלי טז:יח. — IV משלי טז:יח',
                'משלי טז:יח. — הרביעית משלי טז:יח')),
            'c8362da56fc558cdc98f7e90472ea65a': ('טיטוס ב׳:15', (
                'גלטים ב׳:15', 'טיטוס ג׳:15', 'טיטוס ב׳:16', 'טיטוס ב׳:15,16',
                'טיטוס ב׳:15; טיטוס ב׳:15', '')),
        }
        for item in self.fixture['archives']:
            if item['task_id'] not in replacements:
                continue
            archive = self.archive(item)
            original = archive['candidate']
            before, alternatives = replacements[item['task_id']]
            self.assertEqual(original['html'].count(before), 1)
            for after in alternatives:
                with self.subTest(task=item['task_id'], after=after):
                    candidate = {**original, 'html': original['html'].replace(before, after)}
                    with self.assertRaisesRegex(ContractError, 'Scripture chapter/verse'):
                        validate_translation(archive['source'], candidate, language=item['language'])

    def test_exact_aliases_keep_language_book_and_ordinal_identity(self):
        aliases = (('John', 'Joh', 'deu'), ('Isaiah', 'Jes', 'deu'),
                   ('Revelation', 'Offb', 'deu'), ('Colossians', 'Kol', 'deu'),
                   ('1 John', 'הראשונה ליוחנן', 'heb'), ('1 Peter', 'פטרוס הראשונה', 'heb'),
                   ('Galatians', 'גלטים', 'heb'), ('Titus', 'טיטוס', 'heb'))
        for book, alias, language in aliases:
            with self.subTest(alias=alias):
                text = alias + ' 2:14'
                self.check(book + ' 2:14', text, language)
                self.assertEqual(reference_mentions(text, language), [(0, len(text), book + ' 2:14')])
                for invalid in (alias + 'x 2:14', 'x' + alias + ' 2:14',
                                '4 ' + alias + ' 2:14', 'IV ' + alias + ' 2:14',
                                'השנייה ' + alias + ' 2:14', alias + ' 3:14', alias + ' 2:15'):
                    self.check(book + ' 2:14', invalid, language, equal=False)
                self.check('Matthew 2:14', text, language, equal=False)
                self.check(book + ' 2:14', text, 'deu' if language == 'heb' else 'heb', equal=False)
        self.check('John 5:3', 'הראשונה ליוחנן ה׳:ג׳', equal=False)
        self.check('2 Peter 2:2', 'פטרוס הראשונה ב׳:ב׳', equal=False)
        for separator in ('', ' ', '   ', '\t'):
            self.check('1 John 5:3', 'להאיגרת הראשונה' + separator + 'ליוחנן ה׳:ג׳', equal=False)

    def test_german_prose_comma_requires_known_book_whitespace_and_clear_words(self):
        for prose in ('den höheren Gewalten untertan zu sein',
                      'den „höheren Gewalten untertan“ zu sein',
                      '„höheren Gewalten untertan“ zu sein'):
            self.check('In Romans 13 to be subject to higher powers.',
                       'In Römer 13, ' + prose, 'deu')
        for tail in ('', 'den', '14', '14 Worte', '14abc Worte', '14/15', 'IV Worte',
                     'IV „höheren Gewalten“', 'den „14 Worte“', 'den „IV Worte“',
                     'den „höheren Gewalten', '„höheren Gewalten',
                     ', den höheren Gewalten', ': den höheren Gewalten'):
            with self.subTest(tail=tail):
                self.check('In Romans 13 to be subject.', 'In Römer 13, ' + tail, 'deu', equal=False)
        self.check('Romans 13:14', 'Römer 13,14', 'deu')
        self.check('Romans 13:14', 'Römer 13, 14', 'deu')
        self.check('Romans 13:14', 'Römer 13, den höheren Gewalten', 'deu', equal=False)
        self.check('Romans 13:14', 'Römer 13,14, den höheren Gewalten', 'deu')
        source = 'In Romans 13 to be subject.'
        for candidate in ('In Römer 14, den höheren Gewalten',
                          'In Matthäus 13, den höheren Gewalten',
                          'In Römer 13, den höheren Gewalten. Römer 13.',
                          'In Römer, den höheren Gewalten',
                          'Den höheren Gewalten untertan sein.',
                          'In Römer 13.14, den höheren Gewalten',
                          'In Römer 13/14, den höheren Gewalten'):
            with self.subTest(candidate=candidate):
                self.check(source, candidate, 'deu', equal=False)
        self.check(source, 'In Römer 13 den höheren Gewalten untertan sein.', 'deu')
        # No source-backed chapter introduction means no new prose exemption.
        self.check('', 'Römer 13, den höheren Gewalten', 'deu', equal=False)
        self.check('Romans 13 mentions authority.', 'Römer 13, den höheren Gewalten', 'deu', equal=False)
        self.assertEqual(protected_reference_numbers('Romans 13.', 'Römer 14.', language='deu'),
                         (Counter(), Counter()))

    def test_unmarked_hebrew_requires_complete_verified_delimiters(self):
        text = 'Prose — משלי טז:יח. More prose.'
        mentions = reference_mentions(text, 'heb')
        self.assertEqual([key for _, _, key in mentions], ['Proverbs 16:18'])
        self.assertEqual(text[mentions[0][0]:mentions[0][1]], 'משלי טז:יח')
        self.check('Proverbs 16:18', text)
        for candidate in ('משלי טז:יח.', '—משליטז:יח.', '— משלי טז:יח',
                          '— משלי טז :יח.', '— משלי טז: יח.', '— משלי טז יח.',
                          '— משלי טז:18.', '— משלי 16:יח.', '— משלי טז:יח;',
                          '(משלי טז:יח)', '— משלי טז:יח.2', '— משלי טז:יח.עוד',
                          '— משלי טז:יח..', '— משלי טז:יחא.', '— משלי טז:יחa.',
                          '— משלי טז:יח/יט.', '— משלי טז:יח+יט.', '— משלי טז:יח:יט.',
                          '— משלי טז:יח–יט.', '— משלי טז:יח,יט.', '— משלי טז:יח, יח.',
                          '— משלי יו:יח.', '— משלי טז:חי.', '— משלי תתת:יח.',
                          '— משלי יז:יח.', '— משלי טז:יט.', '— תהילים טז:יח.',
                          '— 2 משלי טז:יח.', '— הרביעית משלי טז:יח.'):
            with self.subTest(candidate=candidate):
                self.check('Proverbs 16:18', candidate, equal=False)
        # Neither a missing source citation nor a duplicate gets a free pass.
        self.check('', '— משלי טז:יח.', equal=False)
        self.check('Proverbs 16:18', '— משלי טז:יח. — משלי טז:יח.', equal=False)
        self.check('Proverbs 16:18', '— משלי טז:יח.', 'deu', equal=False)
        for tail in ('/יט', '+יט', ':יט', '–יט', ',יט', 'a', 'א'):
            # A malformed tail cannot disappear next to a valid occurrence.
            self.check('Proverbs 16:18', '— משלי טז:יח. — משלי טז:יח' + tail + '.', equal=False)

    def test_unmarked_prose_and_other_syntax_are_not_generalized(self):
        for text in ('יוחנן אמר: כן.', 'מתי יבוא: מחר.', '— יוחנן אמר:כן.',
                     'משלי טז:יח', 'יוחנן יא:ד', 'בראשית ח:כב', 'טז:יח.',
                     '— יוחנן יא:ד.', '— בראשית ח:כב.'):
            with self.subTest(text=text):
                self.assertEqual(reference_mentions(text, 'heb'), [])
        # Moving a correctly decoded citation to another HTML block still fails.
        pair = documents('<p>Proverbs 16:18.</p><p>More prose.</p>',
                         '<p>עוד מילים.</p><p>— משלי טז:יח.</p>')
        with self.assertRaises(ContractError):
            validate_translation(*pair, language='heb')

    def test_added_unsupported_ordinals_cannot_hide_new_reference_forms(self):
        ordinals = ('4', '4.', 'IV', 'IV.', 'fourth', 'vierte', 'vierter',
                    'הרביעית', 'הרביעי', 'ד׳', '4 IV', 'הרביעית IV', 'IV הרביעית')
        for ordinal in ordinals:
            with self.subTest(ordinal=ordinal):
                source = 'In Romans 13 to be subject.'
                target = 'In Römer 13, den höheren Gewalten. ' + ordinal + ' Römer 14, den höheren Gewalten.'
                self.check(source, target, 'deu', equal=False)
                counters = protected_reference_numbers(source, target, language='deu')
                self.assertTrue(any(key.startswith('!invalid[deu]') for key in counters[1]))
                target = '— משלי טז:יח. — ' + ordinal + ' משלי טז:יח.'
                self.check('Proverbs 16:18', target, equal=False)
                self.assertTrue(any(key.startswith('!invalid[heb]')
                                    for _, _, key in reference_mentions(target, 'heb')))
        # Exact numbered aliases consume their own ordinal and remain valid.
        self.check('In 1 Peter 2 to be subject.', 'In 1. Petrus 2, den höheren Gewalten.', 'deu')
        self.check('1 Samuel 16:18', '— שמואל א׳ טז:יח.')
        self.check('1 Corinthians 16:18', '— הראשונה לקורינתים טז:יח.')
        # No new sentinels leak into unrelated, historically unparsed syntax.
        self.assertEqual(protected_reference_numbers('', '4 Römer 14.', language='deu'),
                         (Counter(), Counter()))
        self.assertEqual(reference_mentions('4 משלי טז:יח.', 'heb'), [])
        self.assertEqual(reference_mentions('— הרביעית משלי אמר:כן.', 'heb'), [])


if __name__ == '__main__':
    unittest.main()
