"""Offline prompt packaging and frozen-campaign checks, not model-quality tests."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError, loads, read_json, write_json
from berean_translation.config import Config
from berean_translation.html import validate_translation
from berean_translation.requests import accepted_review, build_request
from support import queue, setup


ROOT = Path(__file__).resolve().parents[1]
STAGES = ('translate', 'review1', 'correct', 'review2')


class QuotationPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.state, self.upstream, self.provider, _, self.engine = setup(self.root)
        self.fixture = read_json(ROOT / 'tests/fixtures/quotation_policy.json')
        self.upstream.articles = self.upstream.articles[:1]
        article = self.upstream.articles[0]
        article.update({key: value for key, value in self.fixture['source'].items() if key != 'html'})
        article['images'] = []
        self.upstream.contents[article['html']['repository_path']] = self.fixture['source']['html']
        self.upstream.rebuild()
        self.engine.discover()

    def accept(self, identity='quotation-fixture'):
        request = queue(self.state, identity)
        campaign = self.engine.accept_request(request)
        self.assertEqual(len(campaign['tasks']), 1)
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.state.save_candidate(task, self.fixture['translation'])
        return request, campaign, task

    def request(self, task, stage):
        line, _, _ = build_request(self.config, self.state, {**task, 'stage': stage})
        return line['body']['messages'][0]['content'], loads(line['body']['messages'][1]['content'])

    def assert_quotation_policy(self, prompt):
        for text in (
            'quoted substantive prose',
            'authoritative English as printed',
            'Fidelity means preserving the printed meaning, not retaining English wording.',
            'substitute another Bible version',
            'copy, retain, or restore English quoted prose as a fidelity workaround',
            'conventional target-language forms',
            'exact chapter/verse numbers, ranges and the number and placement of references',
            'quoted prose or translatable article metadata',
        ):
            self.assertIn(text, prompt)

    def test_new_campaign_packages_quotation_policy_at_every_stage(self):
        _, campaign, task = self.accept()
        self.assertEqual(campaign['prompt_version'], '1.0.3')
        frozen_source = self.state.path(task['source_snapshot']).read_bytes()
        for stage in STAGES:
            with self.subTest(stage=stage):
                prompt, payload = self.request(task, stage)
                name = 'review' if stage.startswith('review') else 'translation'
                self.assertEqual(prompt, campaign['prompts'][name])
                self.assertEqual(prompt, self.config.prompt(name))
                self.assert_quotation_policy(prompt)
                self.assertIn('When a source clock has no explicit AM/PM period, preserve its printed hour and minute exactly', prompt)
                self.assertEqual(payload['source'], {key: self.fixture['source'][key]
                                                    for key in ('html', 'title', 'subtitle', 'section')})
                self.assertEqual(payload['source_context'], {'byline': 'Miriam'})
                self.assertEqual(payload['language_tag'], 'af')
                if stage == 'translate':
                    self.assertNotIn('translation', payload)
                else:
                    self.assertEqual(payload['translation'], self.fixture['translation'])
        self.assertEqual(self.state.path(task['source_snapshot']).read_bytes(), frozen_source)
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(self.provider.upload_calls, 0)

    def test_correction_packages_invalid_demands_and_valid_evidence_without_filtering(self):
        _, _, task = self.accept()
        valid = self.fixture['valid_meaning_finding']
        candidate = copy.deepcopy(self.fixture['translation'])
        candidate['html'] = candidate['html'].replace('barmhartigheid bewys', valid['translation_quote'])
        self.state.save_candidate(task, candidate)
        task['findings'] = [*self.fixture['invalid_english_demands'], valid]
        prompt, payload = self.request(task, 'correct')
        self.assertIn('Reject reviewer requests to retain or restore English quoted prose merely to match the source verbatim', prompt)
        self.assertIn('when its meaning can be faithfully translated into the target language', prompt)
        self.assertIn('Correct any substantiated meaning, theology, omission, attribution, or reference error in the target language', prompt)
        self.assertIn('not permission to ignore valid findings', prompt)
        self.assertEqual(payload['correction_findings'], task['findings'])
        self.assertEqual(payload['translation'], candidate)
        self.assertIn(valid['translation_quote'], payload['translation']['html'])
        self.assertEqual(payload['source']['html'], self.fixture['source']['html'])

    def test_unchanged_names_cited_titles_and_identifiers_are_contextual_exceptions(self):
        _, campaign, task = self.accept()
        source = self.state.source(task)
        candidate = self.fixture['translation']
        # This checks existing structure/reference compatibility only, not language quality.
        validate_translation(source, candidate)
        for text in ('Miriam', 'Morning Notes', 'BV-Q17'):
            self.assertIn(text, source['html'])
            self.assertIn(text, candidate['html'])
        self.assertIn('Proper names, established titles of cited works, and identifiers may remain unchanged where appropriate to the target language',
                      campaign['prompts']['translation'])
        self.assertIn('An unchanged proper name, established title of a cited work, or identifier is not by itself an untranslated substantive passage',
                      campaign['prompts']['review'])
        self.assertIn('Any substantiated meaning, theology, omission, attribution, or reference error still requires a narrowly scoped correction in the target language',
                      campaign['prompts']['review'])
        for replacement in ('5:7–8', '', '5:7–9 and 5:7–9'):
            with self.subTest(replacement=replacement), self.assertRaises(ContractError):
                changed = {**candidate, 'html': candidate['html'].replace('5:7–9', replacement, 1)}
                validate_translation(source, changed)

    def test_invalid_suggested_fix_does_not_discard_underlying_semantic_defect(self):
        _, _, task = self.accept()
        finding = self.fixture['semantic_defect_with_invalid_fix']
        candidate = copy.deepcopy(self.fixture['translation'])
        candidate['html'] = candidate['html'].replace('barmhartigheid bewys', finding['translation_quote'])
        self.state.save_candidate(task, candidate)
        task['findings'] = [finding]
        prompt, payload = self.request(task, 'correct')
        self.assertIn('An invalid suggested fix does not invalidate its underlying finding', prompt)
        self.assertIn('if it identifies a real semantic defect, repair that defect faithfully in the target language', prompt)
        self.assertEqual(payload['correction_findings'], [finding])
        self.assertIn(finding['source_quote'], payload['source']['html'])
        self.assertIn(finding['translation_quote'], payload['translation']['html'])

    def assert_frozen_after_prompt_change(self, request, campaign, task, version, prompts):
        campaign_path = self.state.path(f'state/campaigns/{campaign["id"]}.json')
        before = campaign_path.read_bytes()
        expected_requests = {stage: self.request(task, stage) for stage in STAGES}
        runtime = read_json(self.root / 'config/runtime.json')
        runtime['prompt_version'] = version
        write_json(self.root / 'config/runtime.json', runtime)
        for name, prompt in prompts.items():
            (self.root / f'prompts/{name}.txt').write_text(prompt, encoding='utf-8')
        self.config = Config(self.root)
        self.engine.config = self.config
        self.assertEqual(self.engine.accept_request(request), campaign)
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(self.request(task, stage), expected_requests[stage])
        self.assertEqual(campaign_path.read_bytes(), before)
        self.assertEqual(self.provider.create_calls, 0)

    def test_existing_1_0_2_campaign_is_not_migrated_to_new_prompts(self):
        current_prompts = {name: self.config.prompt(name) for name in ('translation', 'review')}
        self.config.runtime['prompt_version'] = '1.0.2'
        # Artificial stand-ins let this test distinguish old frozen text from new files.
        for name in current_prompts:
            (self.root / f'prompts/{name}.txt').write_text(f'Frozen 1.0.2 {name} prompt.', encoding='utf-8')
        request, campaign, task = self.accept()
        self.assertEqual(campaign['prompt_version'], '1.0.2')
        self.assert_frozen_after_prompt_change(request, campaign, task, '1.0.3', current_prompts)
        for stage in STAGES:
            _, payload = self.request(task, stage)
            self.assertEqual(payload['source_context'], {'byline': 'Miriam'})
            self.assertNotIn('byline', payload['source'])

    def test_new_campaign_also_keeps_frozen_prompts_after_later_file_edits(self):
        request, campaign, task = self.accept()
        later_prompts = {name: f'Unrelated future {name} prompt.' for name in ('translation', 'review')}
        self.assert_frozen_after_prompt_change(request, campaign, task, 'fixture-future', later_prompts)

    def test_english_demands_are_not_automatically_filtered_out_of_quality_holds(self):
        self.assertEqual(self.config.runtime['quality_threshold'], 95)
        self.assertEqual(self.config.runtime['max_translation_attempts'], 2)
        self.assertFalse(accepted_review({'score': 94, 'passed': True, 'findings': []}))
        self.assertTrue(accepted_review({'score': 95, 'passed': True, 'findings': []}))
        for finding in (*self.fixture['invalid_english_demands'], self.fixture['valid_meaning_finding'],
                        self.fixture['semantic_defect_with_invalid_fix']):
            for severity in ('major', 'critical'):
                with self.subTest(finding=finding['location'], severity=severity):
                    review = {'score': 99, 'passed': True, 'findings': [{**finding, 'severity': severity}]}
                    self.assertFalse(accepted_review(review))


if __name__ == '__main__':
    unittest.main()
