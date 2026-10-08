"""Offline regressions for strict HTML, safe responses, and byline context."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from berean_translation.common import ContractError, canonical, loads
from berean_translation.config import Config
from berean_translation.html import validate_translation
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
        self.assertEqual(self.campaign['prompt_version'], '2.0.0')
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

    def test_current_requests_use_context_without_mutating_historical_campaign(self):
        self.campaign['prompt_version'] = '1.0.1'
        self.campaign['prompts'] = {'translation': 'Frozen translation prompt', 'review': 'Frozen review prompt'}
        before = copy.deepcopy(self.campaign)
        for stage in ('translate', 'review1', 'correct', 'review2'):
            line, _, _ = build_request(self.config, self.state, {**self.task, 'stage': stage})
            payload = loads(line['body']['messages'][1]['content'])
            self.assertNotIn('byline', payload['source'])
            self.assertEqual(payload['source_context'], {'byline': 'Original Author'})
            prompt = self.config.prompt('review' if stage.startswith('review') else 'translation')
            self.assertTrue(line['body']['messages'][0]['content'].startswith(prompt))
            self.assertEqual(payload['quality_threshold'], 95)
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
