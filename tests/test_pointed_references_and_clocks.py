"""Real failed results, exact Hebrew identities, and protected ambiguous times."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from collections import Counter

from berean_translation.common import ContractError
from berean_translation.html import protected_reference_numbers, validate_translation
from berean_translation.reference_notation import reference_mentions
from support import REPO_ROOT
from test_localized_reference_lifecycle import documents


class PointedReferencesAndClocksTests(unittest.TestCase):
    def test_complete_archived_candidates_preserve_history_and_clock_rejection(self):
        fixture = json.loads((REPO_ROOT / 'tests/fixtures/pointed_references_and_clocks.json').read_text())
        for item in fixture['cases']:
            archive, frozen = {}, {}
            for name, proof in item['provenance'].items():
                path = REPO_ROOT / proof['path']
                raw = frozen[path] = path.read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['sha256'])
                self.assertEqual(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
                                 proof['git_blob_sha'])
                self.assertEqual(proof['url'], f'https://github.com/{fixture["repository"]}/blob/'
                                 f'{fixture["revision"]}/{proof["path"]}')
                archive[name] = json.loads(raw)
            self.assertEqual(archive['task']['status'], 'not_ready')
            candidate = {key: value for key, value in archive['correct']['result'].items()
                         if key != 'scripture_selections'}
            if item['kind'] == 'pointed_book':
                self.assertIn('קֹהֶלֶת', candidate['html'])
                validate_translation(archive['source'], candidate, language='heb')
            else:
                with self.assertRaisesRegex(ContractError, 'Clock notation changed.*do not infer AM/PM'):
                    validate_translation(archive['source'], candidate, language='heb')
                # Diagnosis does not silently accept the model's inferred PM.
                fixed = copy.deepcopy(candidate)
                self.assertEqual(fixed['html'].count('17:30'), 1)
                fixed['html'] = fixed['html'].replace('17:30', '5:30')
                validate_translation(archive['source'], fixed, language='heb')
            self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)

    def test_pointed_aliases_retain_exact_spans_and_identity(self):
        pairs = [('Ecclesiastes 12:1-7', 'קֹהֶלֶת 12:1-7'),
                 ('Genesis 4:19', 'בְּבְרֵאשִׁית 4:19'),
                 ('Psalm 144:12', 'מִתְּהִילִים 144:12'),
                 ('1 John 4:7', 'יוֹחָנָן הָרִאשׁוֹנָה 4:7')]
        for original, target in pairs:
            with self.subTest(target=target):
                source = Counter(key for _, _, key in reference_mentions(original, 'eng', target_language='heb'))
                actual = reference_mentions('Before: ' + target + '.', 'heb')
                self.assertEqual(Counter(key for _, _, key in actual), source)
                self.assertEqual(len(actual), 1)
                start, end, _ = actual[0]
                self.assertEqual(('Before: ' + target + '.')[start:end], target)

    def test_pointing_cannot_hide_wrong_books_ordinals_or_word_boundaries(self):
        for source, target in (
            ('Ecclesiastes 12:1-7', 'קֹהֶלֶת 12:1-8'),
            ('Ecclesiastes 12:1-7', 'מִשְׁלֵי 12:1-7'),
            ('Ecclesiastes 12:1-7', 'קֹהֶלֶת 11:1-7'),
            ('Ecclesiastes 12:1-7', 'שְׁקֹהֶלֶת 12:1-7'),
            ('Ecclesiastes 12:1-7', 'קֹהֶלֶתְנ 12:1-7'),
            ('Ecclesiastes 12:1-7', 'ק\u0301הלת 12:1-7'),
            ('Ecclesiastes 12:1-7', 'ק׀הלת 12:1-7'),
            ('Ecclesiastes 12:1-7', 'ק׀ה׀ל׀ת׀ 12:1-7'),
            ('Ecclesiastes 12:1-7', 'ש\u0301קֹהֶלֶת 12:1-7'),
            ('Ecclesiastes 12:1-7', 'ש\u034fקֹהֶלֶת 12:1-7'),
            ('Ecclesiastes 12:1-7', 'ש\u200dקֹהֶלֶת 12:1-7'),
            ('John 3:16', '4 יוֹחָנָן 3:16'),
            ('John 3:16', '4\u0301 יוֹחָנָן 3:16'),
            ('John 3:16', 'הָרְבִיעִית\u0301 יוֹחָנָן 3:16'),
            ('John 3:16', 'הָרְבִיעִית יוֹחָנָן 3:16'),
            ('John 3:16', '1 יוֹחָנָן 3:16'),
            ('1 John 3:16', 'יוֹחָנָן 3:16'),
            ('1 John 3:16', '2 יוֹחָנָן 3:16'),
            ('1 John 3:16', 'שאיג\u0301רת הָרִאשׁוֹנָה לְיוֹחָנָן 3:16'),
            ('1 John 3:16', 'שאיג\u200dרת הָרִאשׁוֹנָה לְיוֹחָנָן 3:16'),
            ('Ecclesiastes 12:1-7', 'קֹהֶלֶת 12:1-7; קֹהֶלֶת 12:1-7'),
            ('Ecclesiastes 12:1-7', ''),
        ):
            with self.subTest(target=target):
                left, right = protected_reference_numbers(source, target, language='heb')
                self.assertNotEqual(left, right)

    def test_clock_diagnostic_never_changes_numeric_acceptance(self):
        for time in ('17:30', '5:31', ''):
            with self.subTest(time=time), self.assertRaisesRegex(ContractError, 'Clock notation changed'):
                validate_translation(*documents('<p>We eat dinner at 5:30.</p>',
                                                f'<p>אנו אוכלים בשעה {time}.</p>'), language='heb')
        validate_translation(*documents('<p>Dinner at 5:30.</p>', '<p>ארוחה בשעה 5:30.</p>'), language='heb')
        validate_translation(*documents('<p>Dinner at 5:30 pm.</p>', '<p>ארוחה בשעה 17:30.</p>'), language='heb')
        for source, target in (
            ('John 5:30.', 'בשעה 17:30.'),
            ('At 5:30, read John 3:16.', 'בשעה 17:30, יוחנן 3:17.'),
            ('At 5:30, read John 3:16.', 'בשעה 17:30.'),
        ):
            with self.subTest(source=source), self.assertRaisesRegex(ContractError, 'Scripture chapter/verse'):
                validate_translation(*documents(f'<p>{source}</p>', f'<p>{target}</p>'), language='heb')


if __name__ == '__main__':
    unittest.main()
