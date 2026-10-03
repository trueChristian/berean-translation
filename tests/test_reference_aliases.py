"""Authentic final-correction alias regressions and conservative boundaries."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError
from berean_translation.html import Fragment, protected_reference_numbers, validate_translation
from berean_translation.reference_notation import reference_mentions
from berean_translation.validation import validate_repository
from support import A, B, REPO_ROOT, drive, queue, setup
from test_localized_reference_lifecycle import documents


FIXTURE = REPO_ROOT / 'tests/fixtures/reference_alias_cases.json'


class ReferenceAliasTests(unittest.TestCase):
    def check(self, source, candidate, language, equal=True):
        pair = documents('<p>' + html.escape(source) + '</p>',
                         '<p>' + html.escape(candidate) + '</p>')
        before = copy.deepcopy(pair)
        if equal:
            validate_translation(*pair, language=language)
        else:
            with self.assertRaises(ContractError):
                validate_translation(*pair, language=language)
        self.assertEqual(pair, before)

    def archive(self, item):
        result = {}
        for kind, provenance in item['provenance'].items():
            path = Path(provenance['path'])
            self.assertFalse(path.is_absolute())
            self.assertNotIn('..', path.parts)
            raw = (REPO_ROOT / path).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance['sha256'])
            self.assertEqual(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
                             provenance['git_blob_sha'])
            result[kind] = json.loads(raw)
        self.assertEqual(result['source']['article']['id'], item['article_id'])
        self.assertEqual(result['task']['article_id'], item['article_id'])
        self.assertEqual(result['task']['language'], item['language'])
        self.assertEqual(result['task']['source_snapshot'], item['provenance']['source']['path'])
        self.assertEqual(result['task']['status'], 'not_ready')
        self.assertEqual(result['task']['review_attempts'], 0)
        return result

    def test_four_archived_final_corrections_pass_without_mutating_history(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(len(fixture['archives']), 4)
        self.assertEqual(sum(item['audited_reference_count'] for item in fixture['archives']), 71)
        for item in fixture['archives']:
            with self.subTest(task=item['task_id'], language=item['language']):
                archive = self.archive(item)
                before = copy.deepcopy(archive)
                # candidate.json can contain the earlier attempt; the immutable
                # correction result is the precise final candidate under test.
                validate_translation(archive['source'], archive['correct']['result'],
                                     language=item['language'])
                original = Fragment(archive['source']['html'], item['article_id'])
                translated = Fragment(archive['correct']['result']['html'], item['article_id'])
                counts = [0, 0]
                for path, block in original.text_by_block.items():
                    # Use the source-backed clock exclusions for this count:
                    # the archives include an ordinary 2 pm / 14:00 conversion.
                    pair = protected_reference_numbers(''.join(block),
                            ''.join(translated.text_by_block[path]), language=item['language'])
                    for side, references in enumerate(pair):
                        counts[side] += sum(count for key, count in references.items()
                                            if 'verse-label' not in key)
                self.assertEqual(counts, [item['audited_reference_count']] * 2)
                self.assertEqual(archive, before)
                self.assertEqual(self.archive(item), before)

    def test_raw_affected_blocks_are_exact_provenance_backed_excerpts(self):
        fixture = json.loads(FIXTURE.read_text())
        archives = {item['task_id']: (item, self.archive(item)) for item in fixture['archives']}
        for case in fixture['cases']:
            with self.subTest(case=case['id']):
                item, archive = archives[case['task_id']]
                original = Fragment(archive['source']['html'], item['article_id'])
                translated = Fragment(archive['correct']['result']['html'], item['article_id'])
                self.assertEqual(case['source'], ''.join(original.text_by_block[case['block']]))
                self.assertEqual(case['candidate'], ''.join(translated.text_by_block[case['block']]))
                self.check(case['source'], case['candidate'], item['language'])

    def test_observed_aliases_keep_explicit_book_identity(self):
        pairs = (
            ('Ps 40:3', 'תהילים 40:3', 'heb', 'Psalm 40:3'),
            ('Ps. 40:3', 'מתהילים 40:3', 'heb', 'Psalm 40:3'),
            ('Rev 18:2', 'ההתגלות 18:2', 'heb', 'Revelation 18:2'),
            ('Rev. 18:2', 'ההתגלות 18:2', 'heb', 'Revelation 18:2'),
            ('Gen 26:15', 'בראשית 26:15', 'heb', 'Genesis 26:15'),
            ('Gen. 26:15', 'בראשית 26:15', 'heb', 'Genesis 26:15'),
            ('Jer. 29:5-7', 'ירמיהו 29:5–7', 'heb', 'Jeremiah 29:5-7'),
            ('Jer 29:5-7', 'ירמיהו 29:5–7', 'heb', 'Jeremiah 29:5-7'),
            ('(Is 65:14)', '(ישעיהו 65:14)', 'heb', 'Isaiah 65:14'),
            ('2 Timothy 4:3', '2 טימותיאוס 4:3', 'heb', '2 Timothy 4:3'),
            ('1 Corinthians 7:39', 'הראשונה לקורינתים 7:39', 'heb', '1 Corinthians 7:39'),
            ('2 Corinthians 6:14-15', 'השנייה לקורינתים 6:14-15', 'heb', '2 Corinthians 6:14-15'),
            ('1 Peter 3:7', 'מהראשונה לפטרוס 3:7', 'heb', '1 Peter 3:7'),
            ('Ephesians 5:22,24', 'אל האפסים 5:22,24', 'heb', 'Ephesians 5:22,24'),
            ('Ephesians 5:22,24', 'Epheser5:22,24', 'deu', 'Ephesians 5:22,24'),
        )
        for source, candidate, language, key in pairs:
            with self.subTest(source=source, candidate=candidate):
                self.check(source, candidate, language)
                self.assertEqual([value for _, _, value in
                                  reference_mentions(source, 'eng', target_language=language)], [key])
                self.assertEqual([value for _, _, value in reference_mentions(candidate, language)], [key])

    def test_book_numbered_book_and_chapter_swaps_remain_held(self):
        for source, candidate, language in (
            ('Ps 40:3', 'ירמיהו 40:3', 'heb'),
            ('Ps 40:3', 'תהילים 41:3', 'heb'),
            ('Gen 26:15', 'שמות 26:15', 'heb'),
            ('Rev 18:2', 'יוחנן 18:2', 'heb'),
            ('Jer. 29:5-7', 'תהילים 29:5-7', 'heb'),
            ('2 Timothy 4:3', '1 טימותיאוס 4:3', 'heb'),
            ('1 Corinthians 7:39', 'השנייה לקורינתים 7:39', 'heb'),
            ('2 Corinthians 6:14-15', 'הראשונה לקורינתים 6:14-15', 'heb'),
            ('1 Peter 3:7', 'השנייה לפטרוס 3:7', 'heb'),
            ('1 Peter 3:7', '2 הראשונה לפטרוס 3:7', 'heb'),
            ('Ephesians 5:22,24', 'Philipper 5:22,24', 'deu'),
            ('Ephesians 5:22,24', 'Epheser 6:22,24', 'deu'),
        ):
            with self.subTest(source=source, candidate=candidate):
                self.check(source, candidate, language, False)

    def test_recognized_hebrew_prefixes_do_not_become_arbitrary_word_suffixes(self):
        for book, source in (('תהילים', 'Psalm 144:12'), ('הראשונה לפטרוס', '1 Peter 3:7')):
            number = source.rsplit(' ', 1)[-1]
            for prefix in ('', 'מ', 'ב', 'וב', 'ו'):
                with self.subTest(book=book, prefix=prefix):
                    self.check(source, prefix + book + ' ' + number, 'heb')
            for prefix in ('א', 'שמ', 'ממ', 'אב', 'כב', 'בב', 'כל', '2 מ'):
                with self.subTest(book=book, prefix=prefix):
                    self.check(source, prefix + book + ' ' + number, 'heb', False)

    def test_quoted_prose_after_lists_preserves_every_verse_and_span(self):
        for language, book, prose in (
            ('deu', 'Epheser', '„Ihr Frauen, ordnet euch unter.“'),
            ('heb', 'אל האפסים', '״נשים, היכנעו לבעליכן.״'),
        ):
            for numbers, key in (('5:22,24', 'Ephesians 5:22,24'),
                                 ('5:25,28,29', 'Ephesians 5:25,28-29')):
                text = book + numbers + ': ' + prose
                with self.subTest(language=language, numbers=numbers):
                    self.check('Ephesians ' + numbers, text, language)
                    mentions = reference_mentions(text, language)
                    self.assertEqual([value for _, _, value in mentions], [key])
                    self.assertIn(numbers, text[mentions[0][0]:mentions[0][1]])
            for numbers in ('5:22', '5:22,23,24', '5:22,24,24', '5:22,25',
                            '5:22,6:24', '5:22,24:1', '5:22,24-6:1',
                            '5:22,24,', '5:22,,24'):
                with self.subTest(language=language, malformed=numbers):
                    self.check('Ephesians 5:22,24', book + ' ' + numbers + ': ' + prose,
                               language, False)
            self.check('Ephesians 5:22,24',
                       book + ' 5:22,24: ' + prose + ' ' + book + ' 5:22,24.', language, False)
            for numbers in ('5:25,28', '5:25,28,30', '5:25,28,29,29', '5:25,28,6:29'):
                with self.subTest(language=language, three_verse_change=numbers):
                    self.check('Ephesians 5:25,28,29', book + ' ' + numbers + ': ' + prose,
                               language, False)

    def test_list_prose_cannot_hide_unsupported_or_ambiguous_numeral_tails(self):
        for language, book, prose in (('deu', 'Epheser', 'Ihr Frauen'),
                                     ('heb', 'אל האפסים', 'נשים היכנעו')):
            for connector in ('+', '/', '*', '×', '÷', '∕', '／', '=', '|', '^', '&', '.'):
                with self.subTest(language=language, connector=connector):
                    self.check('Ephesians 5:22,24',
                               f'{book} 5:22,24{connector}25: {prose}', language, False)
            for tail in ('', ' ', '21 words here', '"21" words here', '“21” words here',
                         '״21״ נשים היכנעו', 'IV words here', '״IV״ נשים היכנעו',
                         'ט״ז נשים היכנעו', '״ט״ז״ נשים היכנעו', 'יד נאמר כאן',
                         '9999 words here', '0 words here', 'ת' * 2000 + ' נאמר כאן'):
                with self.subTest(language=language, tail=tail[:60]):
                    self.check('Ephesians 5:22,24', f'{book} 5:22,24: {tail}', language, False)

    def test_standalone_labels_have_separate_numbered_identity(self):
        for language, label, prose in (
            ('heb', 'פסוק', '״היכנעו זה לזה.״'),
            ('deu', 'Vers', '„Ordnet euch einander unter.“'),
        ):
            for number in (21, 33):
                source = f'Verse {number}, “Submit yourselves one to another.”'
                candidate = f'{label} {number}: {prose}'
                with self.subTest(language=language, number=number):
                    self.check(source, candidate, language)
                    left = [key for _, _, key in reference_mentions(source, 'eng', target_language=language)]
                    right = [key for _, _, key in reference_mentions(candidate, language)]
                    self.assertEqual(len(left), 1)
                    self.assertEqual(left, right)
                    self.assertIn(str(number), left[0])
                    for changed in (f'{label} {number + 1}: {prose}', prose,
                                    candidate + ' ' + candidate,
                                    f'{label} {number}: {label} {number}: {prose}'):
                        with self.subTest(changed=changed):
                            self.check(source, changed, language, False)
                    compact_source = source.replace(f' {number}', str(number))
                    compact_candidate = candidate.replace(f' {number}', str(number))
                    self.check(compact_source, compact_candidate, language)
                    self.assertEqual([key for _, _, key in reference_mentions(
                        compact_source, 'eng', target_language=language)], left)
                    self.assertEqual([key for _, _, key in reference_mentions(
                        compact_candidate, language)], right)
            self.check('First look at verse 21, “Submit yourselves.”',
                       ('ראשית הביטו בפסוק 21: ' if language == 'heb' else 'Zuerst Vers 21: ') + prose,
                       language)
            book = 'אל האפסים' if language == 'heb' else 'Epheser'
            self.check('Verse 21, “Submit yourselves.” Ephesians 5:21.',
                       f'{label} 21: {prose}', language, False)
            self.check('Ephesians 5:21.', f'{label} 21: {prose}', language, False)
            self.check('Verse 21, “Submit yourselves.”', f'{book} 5:21.', language, False)

    def test_malformed_and_nested_standalone_labels_cannot_be_ignored(self):
        for language, label, prose in (('heb', 'פסוק', '״היכנעו זה לזה.״'),
                                       ('deu', 'Vers', '„Ordnet euch einander unter.“')):
            for number in ('22', '0', '9999', '21.5', '21a', '21:22', '21:ט״ז', '"21"', '״21״'):
                with self.subTest(language=language, number=number):
                    self.check('Verse 21, “Submit yourselves.”', f'{label} {number}: {prose}',
                               language, False)
            for tail in ('', '21 words here', '"21" words here', 'IV words here',
                         'ט״ז נאמר כאן', f'{label} 22: {prose}',
                         (f'{label} 21: ' * 1100) + prose):
                with self.subTest(language=language, tail=tail[:60]):
                    self.check('Verse 21, “Submit yourselves.”', f'{label} 21: {tail}', language, False)
            for source in ('Verse 0, “Submit yourselves.”', 'Verse 21.5, “Submit yourselves.”',
                           'Verse "21", “Submit yourselves.”', 'Verse 21a, “Submit yourselves.”'):
                self.check(source, f'{label} 21: {prose}', language, False)

    def test_amounts_clocks_and_prose_cannot_replace_genuine_references(self):
        for language, book, clock in (('deu', 'Psalm', 'Um 14:30 Uhr'),
                                       ('heb', 'תהילים', 'בשעה 14:30')):
            source = 'At 2:30 pm, read Ps 14:30.'
            self.check(source, f'{clock}, {book} 14:30.', language)
            self.check(source, f'{clock}.', language, False)
            self.check('Ps 14:30.', f'{clock}.', language, False)
        for replacement in ('Preis 5,22 Euro', '(5,22) Euro', '5.22', 'Um 5:22 Uhr'):
            self.check('Ephesians 5:22.', replacement, 'deu', False)
        for replacement in ('Epheser said: a plain sentence.', 'qEpheser 5:22', 'EpheserX 5:22'):
            self.check('Ephesians 5:22.', replacement, 'deu', False)

    def test_label_and_alias_cannot_move_between_structural_blocks(self):
        source, candidate = documents('<p>Verse 21, “Submit yourselves.”</p><p>Ps 40:3.</p>',
                                      '<p>תהילים 40:3.</p><p>פסוק 21: ״היכנעו זה לזה.״</p>')
        with self.assertRaisesRegex(ContractError, r'/p\[1\]'):
            validate_translation(source, candidate, language='heb')


class ReferenceAliasLifecycleTests(unittest.TestCase):
    def test_alias_pass_waits_for_independent_review_and_verse_change_stays_held(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, provider, _, engine = setup(Path(directory))
            for path in list(upstream.contents):
                if path.endswith('.html'):
                    upstream.contents[path] = upstream.contents[path].replace(
                        'John 3:16–18.', 'Ephesians 5:22,24.')
            upstream.rebuild()

            def responder(line):
                result = provider.default_result(line)
                if 'html' in result:
                    numbers = '5:22,25' if B in result['html'] else '5:22,24'
                    result['html'] = result['html'].replace(
                        'Ephesians 5:22,24.', f'Epheser{numbers}: „Ihr Frauen, ordnet euch unter.“')
                return result

            queue(state, languages='deu')
            engine.tick()
            provider.complete_all(responder)
            engine.tick()
            tasks = {task['article_id']: task for task in state.tasks()}
            good = tasks[A]
            self.assertEqual(good['stage'], 'review1')
            self.assertEqual(good['status'], 'in_batch')
            self.assertEqual(good['review_attempts'], 1)
            self.assertFalse(state.projection(config)['articles'])
            self.assertEqual(validate_repository(config)['ready'], 0)
            self.assertEqual([event['stage'] for event in good['events']], ['translate'])
            self.assertFalse(state.path(f'state/tasks/{good["id"]}/results/review1.json').exists())

            drive(engine, provider, responder)
            tasks = {task['article_id']: task for task in state.tasks()}
            self.assertEqual(tasks[A]['status'], 'complete')
            self.assertEqual([event['stage'] for event in tasks[A]['events']], ['translate', 'review1'])
            self.assertEqual((tasks[A]['translation_attempts'], tasks[A]['review_attempts']), (1, 1))
            self.assertEqual(tasks[B]['status'], 'not_ready')
            self.assertEqual((tasks[B]['translation_attempts'], tasks[B]['review_attempts']), (2, 0))
            self.assertEqual([event['stage'] for event in tasks[B]['events']], ['translate', 'correct'])
            self.assertIsNotNone(state.candidate(tasks[B]))
            self.assertEqual(validate_repository(config)['ready'], 1)
            self.assertEqual([item['id'] for item in state.projection(config)['articles']], [A])
            frozen = state.path(f'state/tasks/{tasks[B]["id"]}/task.json').read_bytes()
            calls = provider.create_calls
            engine.tick()
            self.assertEqual(provider.create_calls, calls)
            self.assertEqual(state.path(f'state/tasks/{tasks[B]["id"]}/task.json').read_bytes(), frozen)


if __name__ == '__main__':
    unittest.main()
