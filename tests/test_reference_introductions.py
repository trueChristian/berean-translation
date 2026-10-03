"""Actual prose-introduction holds, adversarial references and bounded lifecycle."""
import copy
import html
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError
from berean_translation.html import validate_translation
from berean_translation.reference_notation import reference_mentions
from berean_translation.validation import validate_repository
from support import REPO_ROOT, setup, queue, drive
from test_localized_reference_lifecycle import documents


class ReferenceIntroductionTests(unittest.TestCase):
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

    def test_exact_public_failure_blocks(self):
        fixture = json.loads((REPO_ROOT / 'tests/fixtures/reference_introduction_cases.json').read_text())
        for case in fixture['cases']:
            with self.subTest(case=case['id']):
                self.check(case['source'], case['candidate'], case['language'])

    def test_introductions_do_not_replace_or_hide_real_references(self):
        source = 'In Psalm 119: Thou hast rebuked the proud –Psalm 119:21.'
        for language, book, prose in [('deu', 'Psalm', 'Du hast gesprochen'),
                                       ('heb', 'תהילים', 'נאמר כאן משהו')]:
            introduction = f'{book} 119: {prose}. '
            target = introduction + f'{book} 119:21.'
            self.check(source, target, language)
            keys = [key for _, _, key in reference_mentions(target, language)]
            self.assertEqual(keys, ['Psalm 119:21'])
            for changed in ('', f'{book} 119:22.', f'{book} 118:21.',
                            f'{book} 119:21-22.', f'{book} 119:21. {book} 119:21.'):
                with self.subTest(language=language, changed=changed):
                    self.check(source, introduction + changed, language, False)
            self.check('Psalm 119:21', introduction, language, False)
            self.check('In Psalm 119: Thou hast spoken.', target, language, False)

    def test_dangling_or_numeric_verse_tails_remain_invalid(self):
        for language, book in [('deu', 'Psalm'), ('heb', 'תהילים')]:
            for tail in ('', ' ', ')', '21words here', 'IV words here', '"21" words',
                         '0 words here', '9999 words here', 'יד נאמר כאן',
                         'תתת נאמר כאן', 'ת' * 2000 + ' נאמר כאן', 'א״י נאמר כאן'):
                candidate = f'{book} 119: {tail}'
                with self.subTest(language=language, tail=tail):
                    self.check('Psalm 119:21', candidate, language, False)
                    self.assertTrue(any(key.startswith('!invalid') for _, _, key in
                                        reference_mentions(candidate, language)))
            self.check('Psalm 119:21', f'{book} 0: Thou hast spoken.', language, False)

    def test_attached_hebrew_preposition_preserves_book_and_ordinal_boundaries(self):
        for candidate in ('בבראשית 4:19', 'ובבראשית 4:19', 'בראשית 4:19'):
            self.check('Genesis 4:19', candidate, 'heb')
        for candidate in ('אבבראשית 4:19', 'בבבראשית 4:19', '4 בבראשית 4:19',
                          'בבראשית 4:18', 'בבראשית 5:19', 'בשמות 4:19',
                          'בבראשית 4:19 בראשית 4:19'):
            with self.subTest(candidate=candidate):
                self.check('Genesis 4:19', candidate, 'heb', False)
        self.check('John 3:16', '2ביוחנן 3:16', 'heb', False)
        self.check('John 3:16', 'ב יוחנן 3:16', 'heb', False)

    def test_hebrew_verse_annotation_preserves_complete_range_and_later_citations(self):
        source = 'Matthew 19:3-12: V.4 In the beginning… Matthew 19:4.'
        target = 'מתי 19:3–12: פס׳ 4: בראשית… כששאלו הפרושים. מתי 19:4.'
        self.check(source, target, 'heb')
        self.assertEqual([key for _, _, key in reference_mentions(target, 'heb')],
                         ['Matthew 19:3-12', 'Matthew 19:3-12 verse-label 4', 'Matthew 19:4'])
        for original, replacement in [('19:3–12', '19:3–11'), ('19:4.', '19:5.'),
                                       ('19:4.', ''), ('מתי 19:4.', 'יוחנן 19:4.')]:
            self.check(source, target.replace(original, replacement), 'heb', False)
        self.check(source, target.replace('פס׳ 4:', 'פס׳ 5:'), 'heb', False)
        self.check(source, target.replace('פס׳ 4: ', ''), 'heb', False)
        for label in ('פז׳ 4:', 'ט״ז 4:', 'פס׳ 0:', 'פס׳ 9999:',
                      'פס׳ 4.5:', 'פס׳ 4a:', 'פס׳ 4:16', 'פס׳ 4:יד', 'פס׳ 4:'):
            with self.subTest(label=label):
                malformed = 'מתי 19:3–12: ' + label
                if label != 'פס׳ 4:':
                    malformed += ' בראשית… כששאלו הפרושים.'
                self.check('Matthew 19:3-12', malformed, 'heb', False)

    def test_unqualified_prose_and_nested_annotations_still_fail_closed(self):
        for candidate in ('3: נאמר כאן משהו', 'UnknownBook3: נאמר כאן משהו'):
            self.assertTrue(any(key.startswith('!invalid') for _, _, key in
                                reference_mentions(candidate, 'heb')))
        for count in (2, 1100):
            candidate = 'מתי 19:3–12: ' + 'פס׳ 4: ' * count + 'שלום עולם'
            self.check('Matthew 19:3-12', candidate, 'heb', False)
        for label in ('V.4a', 'V.4.5', 'V.0', 'V.9999'):
            self.check('Matthew 19:3-12: ' + label + ' In the beginning.',
                       'מתי 19:3–12: פס׳ 4: בראשית… כששאלו הפרושים.', 'heb', False)

    def test_complete_hebrew_epistle_alias_keeps_ordinal_identity(self):
        self.check('1 Corinthians 7:5', 'הראשונה אל הקורינתים 7:5', 'heb')
        for source in ('2 Corinthians 7:5', '1 Corinthians 7:6', '1 Corinthians 8:5'):
            self.check(source, 'הראשונה אל הקורינתים 7:5', 'heb', False)
        for candidate in ('השנייה אל הקורינתים 7:5', '2 הראשונה אל הקורינתים 7:5'):
            self.check('1 Corinthians 7:5', candidate, 'heb', False)


class ReferenceIntroductionLifecycleTests(unittest.TestCase):
    def test_terminal_holds_require_explicit_review_and_original_history_stays_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, provider, _, engine = setup(Path(directory))
            for path in list(upstream.contents):
                if path.endswith('.html'):
                    upstream.contents[path] = upstream.contents[path].replace(
                        'John 3:16–18.', 'In Psalm 119: Thou hast spoken –Psalm 119:21. Genesis 4:19.')
            upstream.rebuild()

            def responder(line):
                result = provider.default_result(line)
                if 'html' in result:
                    result['html'] = result['html'].replace(
                        'In Psalm 119: Thou hast spoken –Psalm 119:21. Genesis 4:19.',
                        'ובתהילים 119 נאמר כך –תהילים 119:21. בבראשית 4:19.')
                return result

            queue(state, languages='heb')
            # Simulate the previous parser's verified false hold. The real
            # corrected gate is exercised during the later explicit review.
            with patch('berean_translation.engine.validate_translation',
                       side_effect=ContractError('Previous introductory-colon false hold')):
                drive(engine, provider, responder)
            held = state.tasks()
            self.assertEqual({task['status'] for task in held}, {'not_ready'})
            frozen = {str(path): path.read_bytes() for directory in
                      ('state/tasks', 'state/sources', 'state/campaigns')
                      for path in state.path(directory).rglob('*.json')}
            calls = provider.create_calls
            engine.tick()
            self.assertEqual(provider.create_calls, calls)
            self.assertFalse(state.projection(config)['articles'])
            request = {'id': 'explicit-introduction-review', 'operation': 'review',
                       'model': 'gpt-4.1-mini', 'review_model': 'gpt-4.1-mini',
                       'budget_usd': 1, 'dry_run': False, 'retry_failed': False,
                       'recovery_of_campaign': 'request-1',
                       'previous_task_ids': [task['id'] for task in held]}
            state.write('state/queue/explicit-introduction-review.json', request)
            engine.tick()
            self.assertFalse(state.projection(config)['articles'])
            drive(engine, provider)
            self.assertEqual(validate_repository(config)['ready'], 2)
            campaign = state.read('state/campaigns/explicit-introduction-review.json')
            for identity in campaign['tasks']:
                task = state.read(f'state/tasks/{identity}/task.json')
                self.assertEqual((task['translation_attempts'], task['review_attempts']), (0, 1))
                self.assertEqual(task['status'], 'complete')
            for path, value in frozen.items():
                self.assertEqual(Path(path).read_bytes(), value)


if __name__ == '__main__':
    unittest.main()
