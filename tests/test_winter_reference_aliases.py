"""Winter final-correction citations, immutable evidence, and review boundaries."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError
from berean_translation.html import Fragment, protected_reference_numbers, validate_translation
from berean_translation.reference_notation import reference_mentions
from berean_translation.validation import validate_repository
from support import A, B, REPO_ROOT, queue, setup
from test_localized_reference_lifecycle import documents


FIXTURE = REPO_ROOT / 'tests/fixtures/winter_reference_alias_cases.json'


class WinterReferenceAliasTests(unittest.TestCase):
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
        self.assertEqual(task['status'], 'not_ready')
        self.assertEqual(task['stage'], 'correct')
        self.assertEqual((task['translation_attempts'], task['review_attempts']), (2, 0))
        self.assertEqual([event['stage'] for event in task['events']], ['translate', 'correct'])
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

    def test_four_complete_final_corrections_preserve_all_59_references_and_history(self):
        frozen = self.history()
        self.assertEqual(len(self.fixture['archives']), 4)
        self.assertEqual(sum(item['audited_reference_count']
                             for item in self.fixture['archives']), 59)
        for item in self.fixture['archives']:
            with self.subTest(task=item['task_id'], language=item['language']):
                archive = self.archive(item)
                before = copy.deepcopy(archive)
                # candidate.json is an earlier attempt in these held tasks.
                # Always validate the exact archived final correction result.
                validate_translation(archive['source'], archive['correct']['result'],
                                     language=item['language'])
                source = Fragment(archive['source']['html'], item['article_id'])
                candidate = Fragment(archive['correct']['result']['html'], item['article_id'])
                self.assertEqual(source.text_by_block.keys(), candidate.text_by_block.keys())
                counts = [0, 0]
                for path, block in source.text_by_block.items():
                    references = protected_reference_numbers(''.join(block),
                        ''.join(candidate.text_by_block[path]), language=item['language'])
                    self.assertEqual(*references, msg=path)
                    for side, counter in enumerate(references):
                        counts[side] += sum(counter.values())
                self.assertEqual(counts, [item['audited_reference_count']] * 2)
                self.assertEqual(archive, before)
                self.assertEqual(self.archive(item), before)
        # Passing a read-only gate cannot reset attempts, amend decisions, or
        # manufacture an independent semantic review for historical holds.
        self.assertEqual(self.history(), frozen)

    def test_ten_affected_blocks_are_exact_pinned_excerpts(self):
        archives = {item['task_id']: (item, self.archive(item))
                    for item in self.fixture['archives']}
        self.assertEqual(len(self.fixture['cases']), 10)
        for case in self.fixture['cases']:
            with self.subTest(case=case['id']):
                item, archive = archives[case['task_id']]
                source = Fragment(archive['source']['html'], item['article_id'])
                candidate = Fragment(archive['correct']['result']['html'], item['article_id'])
                self.assertEqual(case['source'], ''.join(source.text_by_block[case['block']]))
                self.assertEqual(case['candidate'], ''.join(candidate.text_by_block[case['block']]))
                self.check(case['source'], case['candidate'], item['language'])
                counters = protected_reference_numbers(case['source'], case['candidate'],
                                                       language=item['language'])
                self.assertEqual(dict(counters[0]), case['expected_references'])
                self.assertEqual(dict(counters[1]), case['expected_references'])

    def test_full_artifact_book_ordinal_number_range_list_duplicate_and_omission_errors_fail(self):
        archives = {item['task_id']: (item, self.archive(item))
                    for item in self.fixture['archives']}
        frozen = self.history()
        self.assertEqual(len(self.fixture['mutations']), 15)
        for mutation in self.fixture['mutations']:
            with self.subTest(mutation=mutation['id']):
                item, archive = archives[mutation['task_id']]
                source = archive['source']
                original = archive['correct']['result']
                validate_translation(source, original, language=item['language'])
                changed = copy.deepcopy(original)
                block = ''.join(Fragment(original['html'], item['article_id'])
                                .text_by_block[mutation['block']])
                self.assertIn(mutation['before'], block)
                self.assertIn(mutation['before'], original['html'])
                changed['html'] = original['html'].replace(mutation['before'], mutation['after'], 1)
                self.assertNotEqual(changed['html'], original['html'])
                # The rest of the real article and all its HTML stay intact;
                # the failure must identify the deliberately altered block.
                with self.assertRaisesRegex(ContractError, re.escape(mutation['block'])):
                    validate_translation(source, changed, language=item['language'])
                self.assertEqual(self.archive(item), archive)
        self.assertEqual(self.history(), frozen)

    def test_observed_names_retain_the_complete_explicit_book_identity(self):
        cases = (
            ('Hebrew 4:11', 'Hebräer 4:11', 'deu', 'Hebrews 4:11'),
            ('Hebrew 4:11', 'אל העברים 4:11', 'heb', 'Hebrews 4:11'),
            ('1 John 4:7', 'האיגרת הראשונה ליוחנן 4:7', 'heb', '1 John 4:7'),
            ('2 Timothy 2:22', 'טימותיאוס השנייה 2:22', 'heb', '2 Timothy 2:22'),
            ('1 Timothy 4:12', 'טימותיאוס הראשונה 4:12', 'heb', '1 Timothy 4:12'),
            ('Revelation 1:14', 'התגלות 1:14', 'heb', 'Revelation 1:14'),
            ('1 John 2:16-17', 'יוחנן הראשונה 2:16-17', 'heb', '1 John 2:16-17'),
        )
        for source, candidate, language, expected in cases:
            with self.subTest(source=source, candidate=candidate):
                self.check(source, candidate, language)
                self.assertEqual([key for _, _, key in
                                  reference_mentions(source, 'eng', target_language=language)],
                                 [expected])
                mentions = reference_mentions(candidate, language)
                self.assertEqual([key for _, _, key in mentions], [expected])
                self.assertEqual(candidate[mentions[0][0]:mentions[0][1]], candidate)

    def test_gospel_john_and_first_epistle_do_not_collapse(self):
        self.check('John 4:7', 'יוחנן 4:7')
        for alias in ('האיגרת הראשונה ליוחנן', 'יוחנן הראשונה'):
            with self.subTest(alias=alias):
                self.check('1 John 4:7', alias + ' 4:7')
                self.check('John 4:7', alias + ' 4:7', equal=False)
                self.check('1 John 4:7', 'יוחנן 4:7', equal=False)
                self.check('2 John 4:7', alias + ' 4:7', equal=False)
                self.check('3 John 4:7', alias + ' 4:7', equal=False)

    def test_unobserved_names_and_unsupported_ordinals_cannot_match_supported_books(self):
        for source, candidate in (
            ('1 John 2:14', 'הראשונה ליוחנן 2:14'),
            ('1 John 2:14', 'להאיגרת הראשונה ליוחנן 2:14'),
            ('1 John 2:14', 'האיגרת השנייה ליוחנן 2:14'),
            ('1 John 2:14', 'האיגרת הראשונה ליוחנן השנייה 2:14'),
            ('1 John 2:14', 'יוחנן השנייה 2:14'),
            ('1 Timothy 4:12', 'טימותיאוס השלישית 4:12'),
            ('2 Timothy 2:22', 'טימותיאוס השלישית 2:22'),
        ):
            with self.subTest(candidate=candidate):
                self.check(source, candidate, equal=False)
        for source, alias in (
            ('1 John', 'האיגרת הראשונה ליוחנן'),
            ('1 John', 'יוחנן הראשונה'),
            ('1 Timothy', 'טימותיאוס הראשונה'),
            ('2 Timothy', 'טימותיאוס השנייה'),
            ('Revelation', 'התגלות'),
        ):
            for ordinal in ('2 ', '4 ', 'IV ', 'הרביעית ', 'השנייה '):
                with self.subTest(alias=alias, ordinal=ordinal):
                    self.check(source + ' 2:14', ordinal + alias + ' 2:14', equal=False)

    def test_existing_hebrew_prefix_allowlist_does_not_accept_arbitrary_words(self):
        for source, alias in (
            ('1 John', 'האיגרת הראשונה ליוחנן'),
            ('1 John', 'יוחנן הראשונה'),
            ('1 Timothy', 'טימותיאוס הראשונה'),
            ('2 Timothy', 'טימותיאוס השנייה'),
            ('Revelation', 'התגלות'),
        ):
            for prefix in ('', 'מ', 'ב', 'וב', 'ומ', 'ו'):
                with self.subTest(alias=alias, prefix=prefix):
                    self.check(source + ' 2:14', prefix + alias + ' 2:14')
            for prefix in ('א', 'שמ', 'ממ', 'אב', 'כב', 'בב', 'כל', 'ל', '2 מ'):
                with self.subTest(alias=alias, prefix=prefix):
                    self.check(source + ' 2:14', prefix + alias + ' 2:14', equal=False)
            self.check(source + ' 2:14', alias + 'x 2:14', equal=False)
        for source in ('qHebrew 4:11', 'HebrewX 4:11', '2 Hebrew 4:11', 'IV Hebrew 4:11'):
            with self.subTest(source=source):
                self.check(source, 'Hebräer 4:11', 'deu', equal=False)


class WinterReferenceAliasLifecycleTests(unittest.TestCase):
    def test_final_alias_pass_requires_independent_review_and_genuine_error_stays_held(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, provider, _, engine = setup(Path(directory))
            english = ('Hebrew 4:11. 1 John 4:7. 2 Timothy 2:22. 1 Timothy 4:12. '
                       'Revelation 1:14. 1 John 2:16-17.')
            translated = ('אל העברים 4:11. האיגרת הראשונה ליוחנן 4:7. טימותיאוס השנייה 2:22. '
                          'טימותיאוס הראשונה 4:12. התגלות 1:14. יוחנן הראשונה 2:16-17.')
            for path in list(upstream.contents):
                if path.endswith('.html'):
                    upstream.contents[path] = upstream.contents[path].replace('John 3:16–18.', english)
            upstream.rebuild()

            def responder(line):
                result = provider.default_result(line)
                if 'html' in result:
                    text = translated
                    # Both initial attempts fail; only A's one correction fixes
                    # the actual verse error. The alias spellings are identical.
                    if line['custom_id'].split(':')[1] == 'translate' or B in result['html']:
                        text = text.replace('טימותיאוס השנייה 2:22', 'טימותיאוס השנייה 2:23')
                    result['html'] = result['html'].replace(english, text)
                return result

            queue(state, languages='heb')
            engine.tick()
            provider.complete_all(responder)
            engine.tick()
            self.assertEqual({task['stage'] for task in state.tasks()}, {'correct'})
            self.assertFalse(state.projection(config)['articles'])
            provider.complete_all(responder)
            engine.tick()
            tasks = {task['article_id']: task for task in state.tasks()}
            good, bad = tasks[A], tasks[B]
            self.assertEqual((good['stage'], good['status']), ('review2', 'in_batch'))
            self.assertEqual((good['translation_attempts'], good['review_attempts']), (2, 1))
            self.assertEqual([event['stage'] for event in good['events']], ['translate', 'correct'])
            self.assertFalse(state.path(f'state/tasks/{good["id"]}/results/review2.json').exists())
            self.assertFalse(state.projection(config)['articles'])
            self.assertEqual(validate_repository(config)['ready'], 0)
            self.assertEqual(bad['status'], 'not_ready')
            self.assertEqual((bad['translation_attempts'], bad['review_attempts']), (2, 0))
            frozen = {path: path.read_bytes()
                      for path in state.path(f'state/tasks/{bad["id"]}').rglob('*') if path.is_file()}

            provider.complete_all(responder)
            engine.tick()
            tasks = {task['article_id']: task for task in state.tasks()}
            self.assertEqual(tasks[A]['status'], 'complete')
            self.assertEqual([event['stage'] for event in tasks[A]['events']],
                             ['translate', 'correct', 'review2'])
            self.assertEqual((tasks[A]['translation_attempts'], tasks[A]['review_attempts']), (2, 1))
            self.assertEqual(tasks[B]['status'], 'not_ready')
            self.assertEqual([event['stage'] for event in tasks[B]['events']], ['translate', 'correct'])
            self.assertIsNotNone(state.candidate(tasks[B]))
            self.assertEqual(validate_repository(config)['ready'], 1)
            self.assertEqual([item['id'] for item in state.projection(config)['articles']], [A])
            calls = (provider.create_calls, provider.upload_calls)
            for _ in range(2):
                engine.tick()
            self.assertEqual((provider.create_calls, provider.upload_calls), calls)
            self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)


if __name__ == '__main__':
    unittest.main()
