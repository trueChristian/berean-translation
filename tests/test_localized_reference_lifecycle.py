"""Offline localized citation gates and immutable held-candidate recovery."""
from __future__ import annotations

import copy
import html
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError
from berean_translation.html import validate_translation
from berean_translation.validation import validate_repository
from support import REPO_ROOT, setup, queue, drive


ARTICLE_ID = '11111111-1111-4111-8111-111111111111'


def documents(source_body, candidate_body):
    metadata = {'title': 'Reference fixture', 'subtitle': None, 'section': ''}
    source = {'article': {'id': ARTICLE_ID, **metadata},
              'html': f'<article data-article-id="{ARTICLE_ID}">{source_body}</article>'}
    candidate = {**metadata, 'html': f'<article data-article-id="{ARTICLE_ID}">{candidate_body}</article>'}
    return source, candidate


class LocalizedReferenceGateTests(unittest.TestCase):
    def test_public_failure_excerpts_and_synthetic_negatives(self):
        fixture = json.loads((REPO_ROOT / 'tests/fixtures/localized_reference_cases.json').read_text())
        for case in fixture['cases']:
            with self.subTest(case=case['id']):
                source, candidate = documents('<p>' + html.escape(case['source']) + '</p>',
                                              '<p>' + html.escape(case['candidate']) + '</p>')
                before = copy.deepcopy((source, candidate))
                if case['expected_equal']:
                    validate_translation(source, candidate, language=case['language'])
                else:
                    with self.assertRaises(ContractError):
                        validate_translation(source, candidate, language=case['language'])
                self.assertEqual((source, candidate), before)

    def test_language_is_required_and_unrelated_languages_keep_strict_legacy_gate(self):
        for original, candidate, language in (
            ('John 3:16', 'Johannes 3,16', 'deu'),
            ('Genesis 8:22', 'בראשית ח׳:כ״ב', 'heb'),
        ):
            source, translated = documents(f'<p>{original}</p>', f'<p>{candidate}</p>')
            validate_translation(source, translated, language=language)
            for other in (None, 'afr', 'rus', 'eng'):
                with self.subTest(language=language, other=other), self.assertRaises(ContractError):
                    validate_translation(source, translated, language=other)

    def test_localized_references_cannot_move_between_blocks(self):
        for text, language in (('Johannes 3,16', 'deu'), ('יוחנן ג׳:ט״ז', 'heb')):
            source, candidate = documents('<p>John 3:16.</p><p>Conclusion.</p>',
                                          f'<p>Conclusion.</p><p>{text}.</p>')
            with self.subTest(language=language), self.assertRaisesRegex(ContractError, r'/p\[1\]'):
                validate_translation(source, candidate, language=language)

    def test_known_book_identity_is_preserved_even_with_identical_numbers(self):
        for text, language in (('Matthäus 3,16', 'deu'), ('מתי ג׳:ט״ז', 'heb')):
            with self.subTest(language=language), self.assertRaises(ContractError):
                validate_translation(*documents('<p>John 3:16.</p>', f'<p>{text}.</p>'), language=language)

    def test_clock_exemptions_do_not_consume_localized_scripture(self):
        for clock, reference, language in (
            ('Um 14:30 Uhr', 'Johannes 14,30', 'deu'),
            ('בשעה 14:30', 'יוחנן י״ד:ל׳', 'heb'),
        ):
            source, candidate = documents('<p>At 2:30 pm, read John 14:30.</p>',
                                          f'<p>{clock}, {reference}.</p>')
            validate_translation(source, candidate, language=language)
            missing = documents('<p>At 2:30 pm, read John 14:30.</p>', f'<p>{clock}.</p>')
            with self.subTest(language=language), self.assertRaises(ContractError):
                validate_translation(*missing, language=language)

    def test_numbered_john_cannot_become_gospel_or_another_epistle(self):
        for language, book in (('deu', 'Johannes'), ('heb', 'יוחנן')):
            for ordinal in (1, 2, 3):
                for source, candidate in (
                    ('John 3:16', f'{ordinal}. {book} 3:16'),
                    (f'{ordinal} John 3:16', f'{book} 3:16'),
                    (f'{ordinal} John 3:16', f'{ordinal % 3 + 1}. {book} 3:16'),
                ):
                    with self.subTest(language=language, source=source, candidate=candidate), \
                            self.assertRaises(ContractError):
                        validate_translation(*documents(f'<p>{source}</p>', f'<p>{candidate}</p>'),
                                             language=language)

    def test_parenthesized_amounts_cannot_replace_a_bare_citation(self):
        for amount in ('(2,14) Euro', '(2,14)%', '€ (2,14)', '(2,14) USD'):
            with self.subTest(amount=amount), self.assertRaises(ContractError):
                validate_translation(*documents('<p>(2:14)</p>', f'<p>{amount}</p>'), language='deu')

    def test_identical_unmarked_clocks_remain_protected_but_changed_clocks_fail(self):
        for language, phrase in (('deu', 'Treffen um'), ('heb', 'ניפגש בשעה')):
            source, same = documents('<p>Meet at 9:00.</p>', f'<p>{phrase} 9:00.</p>')
            validate_translation(source, same, language=language)
            for value in ('9:01', '8:00', ''):
                with self.subTest(language=language, value=value), self.assertRaises(ContractError):
                    validate_translation(*documents('<p>Meet at 9:00.</p>', f'<p>{phrase} {value}.</p>'),
                                         language=language)

    def test_unsupported_numeric_tails_cannot_be_accepted_as_a_prefix(self):
        for tail in ('/19', '+19', '.19'):
            with self.subTest(tail=tail), self.assertRaises(ContractError):
                validate_translation(*documents('<p>Matthew 1:18.</p>',
                                                 f'<p>Matthäus 1,18{tail}.</p>'), language='deu')


class LocalizedReferenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state, _, self.provider, _, self.engine = setup(self.root)

    def responder(self, line):
        result = self.provider.default_result(line)
        if 'html' in result:
            result['html'] = result['html'].replace('John 3:16–18', 'Johannes 3,16–18')
        return result

    def test_new_candidate_still_requires_independent_review_before_publication(self):
        queue(self.state, languages='deu')
        drive(self.engine, self.provider, self.responder)
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        for task in self.state.tasks():
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
            self.assertEqual(task['status'], 'complete')
            self.assertEqual([event['stage'] for event in task['events']], ['translate', 'review1'])

    def test_existing_terminal_holds_are_unchanged_until_explicit_bounded_review(self):
        queue(self.state, languages='deu')
        # Simulate the deployed pre-fix gate, which had no language argument.
        with patch('berean_translation.engine.validate_translation',
                   side_effect=lambda source, candidate, **kwargs: validate_translation(source, candidate)):
            drive(self.engine, self.provider, self.responder)
        held = self.state.tasks()
        self.assertEqual({task['status'] for task in held}, {'not_ready'})
        frozen = {str(path): path.read_bytes() for directory in ('state/tasks', 'state/sources', 'state/campaigns')
                  for path in self.state.path(directory).rglob('*.json')}
        calls = self.provider.create_calls
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, calls)
        self.assertFalse(self.state.projection(self.config)['articles'])
        for path, value in frozen.items():
            self.assertEqual(Path(path).read_bytes(), value)

        request = {'id': 'explicit-localized-review', 'operation': 'review',
                   'model': 'gpt-4.1-mini', 'review_model': 'gpt-4.1-mini',
                   'budget_usd': 1, 'dry_run': False, 'retry_failed': False,
                   'recovery_of_campaign': 'request-1', 'previous_task_ids': [t['id'] for t in held]}
        self.state.write('state/queue/explicit-localized-review.json', request)
        drive(self.engine, self.provider)
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        campaign = self.state.read('state/campaigns/explicit-localized-review.json')
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (0, 1))
            self.assertEqual(task['status'], 'complete')
        for path, value in frozen.items():
            self.assertEqual(Path(path).read_bytes(), value)


if __name__ == '__main__':
    unittest.main()
