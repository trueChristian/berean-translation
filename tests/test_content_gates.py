"""Offline regressions for source-backed clocks, strict HTML, and byline context."""
from __future__ import annotations
import copy
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from berean_translation.common import ContractError, canonical, loads
from berean_translation.config import Config
from berean_translation.html import reference_numbers, validate_translation
from berean_translation.requests import build_request, parse_response
from support import setup, queue, drive


ARTICLE_ID = '11111111-1111-4111-8111-111111111111'
ROOT = Path(__file__).resolve().parents[1]


class ContentGateTests(unittest.TestCase):
    def validate(self, source_body, translated_body):
        source = {'article': {'id': ARTICLE_ID, 'title': 'A story', 'subtitle': None, 'section': ''},
                  'html': f'<article data-article-id="{ARTICLE_ID}">{source_body}</article>'}
        candidate = {key: source['article'][key] for key in ('title', 'subtitle', 'section')}
        candidate['html'] = f'<article data-article-id="{ARTICLE_ID}">{translated_body}</article>'
        return validate_translation(source, candidate)

    def test_source_backed_clock_notation_is_equivalent(self):
        for source, translated in (
            ('Around 2 pm, a call came.', 'Omstreeks 14:00 het ’n oproep gekom.'),
            ('At 9 a.m., we met.', 'Om 09:00 het ons ontmoet.'),
            ('At 12 am, we met.', 'Omstreeks 00:00 het ons ontmoet.'),
            ('At 12 p.m., we met.', 'Omstreeks 12:00 het ons ontmoet.'),
            ('At 2:30 pm, we met.', 'Ons het 14:30 uur ontmoet.'),
            ('At 2:30 PM, we met.', 'Omstreeks ١٤:٣٠ het ons ontmoet.'),
            ('At 2 pm and 4 pm, we met.', 'Om 14:00 en om 16:00 het ons ontmoet.'),
            ('Around 2 pm, read John 3:16–18.', 'Omstreeks 14:00, lees Johannes ٣:١٦—١٨.'),
            ('At 2:30 pm, read John 14:30.', 'Om 14:30, lees Johannes 14:30.'),
        ):
            with self.subTest(source=source, translated=translated):
                self.validate(f'<p>{source}</p>', f'<p>{translated}</p>')

    def test_clock_may_span_preserved_inline_markup(self):
        self.validate('<p>Around <em>2 pm</em>, a call came.</p>',
                      '<p>Omstreeks <em>14:00</em> het ’n oproep gekom.</p>')

    def test_unexplained_or_ambiguous_clock_shaped_numbers_remain_protected(self):
        for source, translated in (
            ('A call came.', 'Omstreeks 14:00 het ’n oproep gekom.'),
            ('Around 2 pm, a call came.', '14:00 het ’n oproep gekom.'),
            ('Around 2 pm, a call came.', 'Johannes 14:00.'),
            ('Around 2 pm, a call came.', 'Omstreeks 15:00 het ’n oproep gekom.'),
            ('Around 2 pm, a call came.', 'Omstreeks 24:00 het ’n oproep gekom.'),
            ('Around 2 pm, a call came.', 'Omstreeks 14:99 het ’n oproep gekom.'),
            ('Around 2 pm, a call came.', 'Omstreeks 14:00–03 het ’n oproep gekom.'),
            ('Around 2 pm, a call came.', 'Om 14:00 en om 14:00.'),
            ('Around 2 pm, a call came.', 'Around 2 pm, and at 14:00.'),
            ('Read 3:16.', 'Read 3:17.'),
            ('Read 14:00.', 'Omstreeks 2 pm.'),
            ('Read 14:00.', 'Read 15:00.'),
        ):
            with self.subTest(source=source, translated=translated), self.assertRaisesRegex(ContractError, 'Scripture'):
                self.validate(f'<p>{source}</p>', f'<p>{translated}</p>')

    def test_clock_exception_never_hides_missing_added_changed_or_duplicate_references(self):
        for source, translated in (
            ('Around 2 pm. John 3:16.', 'Om 14:00. Johannes 3:17.'),
            ('Around 2 pm. John 3:16.', 'Om 14:00.'),
            ('Around 2 pm. John 3:16.', 'Om 14:00. Johannes 3:16 en 3:17.'),
            ('Around 2 pm. John 3:16 and 3:16.', 'Om 14:00. Johannes 3:16.'),
            ('Around 2 pm. John 3:16–18.', 'Om 14:00. Johannes ٣:١٦—١٩.'),
            ('Around 2:30 pm. John 14:30.', 'Om 14:30.'),
            ('Around 2:30 pm. John 3:16.', 'Om 14:30. Johannes 14:30.'),
        ):
            with self.subTest(source=source, translated=translated), self.assertRaisesRegex(ContractError, 'Scripture'):
                self.validate(f'<p>{source}</p>', f'<p>{translated}</p>')

    def test_clock_cannot_explain_number_in_another_block(self):
        with self.assertRaisesRegex(ContractError, r'/p\[2\]'):
            self.validate('<p>Around 2 pm, a call came.</p><p>Conclusion.</p>',
                          '<p>’n Oproep het gekom.</p><p>Omstreeks 14:00.</p>')

    def test_scripture_placement_is_preserved_between_blocks(self):
        with self.assertRaisesRegex(ContractError, r'/p\[1\]'):
            self.validate('<p>John 3:16.</p><p>John 4:7.</p>',
                          '<p>Johannes 4:7.</p><p>Johannes 3:16.</p>')

    def test_clock_cannot_move_between_non_paragraph_text_containers(self):
        for tag in ('div','section','td','th','caption','pre','figure'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ContractError, 'Scripture'):
                self.validate(f'<{tag}>Around 2 pm, a call came.</{tag}><{tag}>Conclusion.</{tag}>',
                              f'<{tag}>A call came.</{tag}><{tag}>Omstreeks 14:00.</{tag}>')
            with self.subTest(tag=tag):
                self.validate(f'<{tag}>Around <em>2 pm</em>, a call came.</{tag}>',
                              f'<{tag}>Omstreeks <em>14:00</em> het ’n oproep gekom.</{tag}>')

    def test_standalone_number_extractor_is_still_conservative(self):
        self.assertEqual(reference_numbers('Around 2 pm; Omstreeks 14:00; John 3:16–18; ٣:١٦—١٨.'),
                         Counter({'14:00': 1, '3:16-18': 2}))

    def test_reference_error_identifies_values_and_counts(self):
        with self.assertRaises(ContractError) as caught:
            self.validate('<p>John 3:16 and 3:16; John 4:7.</p>', '<p>Johannes 4:8.</p>')
        message = str(caught.exception)
        self.assertIn('/article[1]/p[1]', message)
        self.assertIn('missing: 3:16 (x2), 4:7', message)
        self.assertIn('extra: 4:8', message)

    def test_structure_error_identifies_first_path_and_signature(self):
        with self.assertRaises(ContractError) as caught:
            self.validate('<p>First.</p><p>Second.</p>', '<p>First.</p><p class="byline">Second.</p>')
        message = str(caught.exception)
        self.assertIn('first difference at signature[3]', message)
        self.assertIn('source /article[1]/p[2]', message)
        self.assertIn('translation /article[1]/p[2]', message)
        self.assertIn("('class', 'byline')", message)

    def test_invented_byline_is_still_a_structural_failure(self):
        with self.assertRaisesRegex(ContractError, 'HTML structure'):
            self.validate('<p>Story.</p>', '<p class="byline">Author</p><p>Story.</p>')

    def test_added_removed_nested_markup_comments_and_links_stay_strict(self):
        source = '<div><p><em>Read</em> <a href="/original/">this</a>.</p><!-- audit --></div>'
        for translated in (
            source.replace('<em>Read</em>', 'Read'),
            source.replace('/original/', '/changed/'),
            source.replace('<!-- audit -->', ''),
            source.replace('<!-- audit -->', '<!-- changed -->'),
            source.replace('</div>', '<p>Extra.</p></div>'),
        ):
            with self.subTest(translated=translated), self.assertRaisesRegex(ContractError, 'first difference'):
                self.validate(source, translated)


class BylineRequestTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(ROOT)
        self.source = {'article': {'id': ARTICLE_ID, 'title': 'Story', 'subtitle': None,
                                   'section': '', 'byline': 'Original Author'},
                       'html': f'<article data-article-id="{ARTICLE_ID}"><p>Story.</p></article>'}
        self.candidate = {key: self.source['article'][key] for key in ('title', 'subtitle', 'section')}
        self.candidate['html'] = self.source['html']
        self.campaign = {'prompt_version': self.config.runtime['prompt_version'],
                         'language_settings': self.config.languages, 'glossaries': {},
                         'prompts': {name: self.config.prompt(name) for name in ('translation', 'review')},
                         'max_output_tokens': self.config.runtime['max_output_tokens'],
                         'review_output_tokens': self.config.runtime['review_output_tokens']}
        self.state = SimpleNamespace(source=lambda task: self.source,
                                     read=lambda path: self.campaign,
                                     candidate=lambda task: self.candidate)
        self.task = {'id': 'task', 'campaign': 'campaign', 'language': 'afr',
                     'stage': 'translate', 'model': 'gpt-4.1-mini', 'review_model': 'gpt-4.1-mini',
                     'models': self.config.models}

    def test_new_campaigns_separate_context_from_exact_translation_fields_at_every_stage(self):
        self.assertEqual(self.campaign['prompt_version'], '1.0.2')
        for stage in ('translate', 'review1', 'correct', 'review2'):
            with self.subTest(stage=stage):
                line, _, _ = build_request(self.config, self.state, {**self.task, 'stage': stage})
                body = line['body']
                payload = loads(body['messages'][1]['content'])
                self.assertEqual(set(payload['source']), {'html', 'title', 'subtitle', 'section'})
                self.assertEqual(payload['source_context'], {'byline': 'Original Author'})
                self.assertNotIn('Original Author', payload['source']['html'])
                self.assertIn('source_context.byline', body['messages'][0]['content'])
                if not stage.startswith('review'):
                    self.assertEqual(body['response_format']['json_schema']['schema']['required'],
                                     ['html', 'title', 'subtitle', 'section'])
                if stage != 'translate':
                    self.assertEqual(payload['translation'], self.candidate)

    def test_absent_byline_remains_absent_context(self):
        del self.source['article']['byline']
        line, _, _ = build_request(self.config, self.state, self.task)
        self.assertEqual(loads(line['body']['messages'][1]['content'])['source_context'], {'byline': None})

    def test_historical_campaign_prompt_and_payload_contract_are_not_rewritten(self):
        self.campaign['prompt_version'] = '1.0.1'
        self.campaign['prompts'] = {'translation': 'Frozen translation prompt', 'review': 'Frozen review prompt'}
        before = copy.deepcopy(self.campaign)
        for stage in ('translate', 'review1', 'correct', 'review2'):
            line, _, _ = build_request(self.config, self.state, {**self.task, 'stage': stage})
            payload = loads(line['body']['messages'][1]['content'])
            self.assertEqual(payload['source']['byline'], 'Original Author')
            self.assertNotIn('source_context', payload)
            prompt = before['prompts']['review' if stage.startswith('review') else 'translation']
            self.assertEqual(line['body']['messages'][0]['content'], prompt)
        self.assertEqual(self.campaign, before)

    def test_prompts_do_not_request_context_byline_as_candidate_prose(self):
        for name in ('translation', 'review'):
            prompt = self.config.prompt(name)
            self.assertIn('source_context.byline is attribution context only', prompt)
            self.assertIn('p.byline', prompt)
            self.assertIn('inside source.html', prompt)
        self.assertIn('Its absence from the translation is not an omission', self.config.prompt('review'))


class ResponseShapeTests(unittest.TestCase):
    def setUp(self):
        self.row = {'error': None, 'response': {'status_code': 200, 'body': {
            'model': 'test-model', 'usage': {'prompt_tokens': 12, 'completion_tokens': 6},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}}}

    def test_malformed_rows_and_response_shapes_raise_contract_error(self):
        for row in (None, [], 'row', {'error': 'error'}, {'error': []},
                    {'response': []}, {'response': None}, {'error': {}}, {'response': {}}):
            with self.subTest(row=row), self.assertRaises(ContractError):
                parse_response(row, 10000)
        for body in (None, [], 'body', 1):
            row = copy.deepcopy(self.row)
            row['response']['body'] = body
            with self.subTest(body=body), self.assertRaisesRegex(ContractError, 'body must be an object'):
                parse_response(row, 10000)

    def test_malformed_choice_message_usage_and_unicode_raise_contract_error(self):
        cases = [('choices', None), ('choices', {}), ('choices', [None]), ('choices', ['choice']),
                 ('choices', []), ('choices', [{}, {}]),
                 ('message', None), ('message', []), ('message', 'text'),
                 ('usage', []), ('usage', 'tokens'), ('content', '\ud800')]
        for field, value in cases:
            row = copy.deepcopy(self.row)
            body = row['response']['body']
            if field == 'message':
                body['choices'][0]['message'] = value
            elif field == 'content':
                body['choices'][0]['message']['content'] = value
            else:
                body[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                parse_response(row, 10000)

    def test_missing_or_null_usage_still_accepts_valid_structured_response(self):
        for present in (True, False):
            row = copy.deepcopy(self.row)
            if present:
                row['response']['body']['usage'] = None
            else:
                del row['response']['body']['usage']
            result, provenance = parse_response(row, 10000)
            self.assertEqual(result, {})
            self.assertEqual(provenance['usage'], {})

    def test_one_malformed_response_does_not_stop_other_batch_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, provider, git, engine = setup(Path(directory))
            queue(state)
            engine.tick()
            provider.complete_all()
            batch = next(iter(provider.batches.values()))
            output_id = batch['output_file_id']
            rows = [loads(line) for line in provider.files[output_id].splitlines()]
            rows[0]['response']['body'] = []
            provider.files[output_id] = b'\n'.join(canonical(row) for row in rows)
            drive(engine, provider)
            self.assertEqual(sorted(task['status'] for task in state.tasks()), ['complete', 'not_ready'])
            failed = next(task for task in state.tasks() if task['status'] == 'not_ready')
            self.assertIn('body must be an object', failed['failure'])


if __name__ == '__main__':
    unittest.main()
