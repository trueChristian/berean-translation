"""Offline regressions for substantive plain review and frozen request policy."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

from berean_translation.common import ContractError, loads, read_json
from berean_translation.requests import accepted_review, build_request
from berean_translation.review_contract import actionable_finding, review_threshold

REPO_ROOT = Path(__file__).resolve().parents[1]


def finding(severity='major', source='Do I need to tell?', target='Devo dirlo?', fix='Devo dirlo?'):
    return {'severity': severity, 'location': 'title', 'source_quote': source,
            'translation_quote': target, 'suggested_fix': fix}


def review(*, score=95, passed=False, findings=None, complete=True):
    return {'score': score, 'passed': passed, 'findings': findings or [],
            'findings_complete': complete}


class PlainReviewPolicyTests(unittest.TestCase):
    source = {'html': '<article><p>Faith.</p></article>', 'article': {'title': 'Do I need to tell?'}}
    candidate = {'html': '<article><p>Fede.</p></article>', 'title': 'Devo dirlo?',
                 'subtitle': None, 'section': None}

    def accept(self, result, threshold=95, **changes):
        arguments = {'contract_version': 2,
                     'source': self.source, 'candidate': self.candidate}
        arguments.update(changes)
        return accepted_review(result, threshold, **arguments)

    def test_recorded_no_op_title_finding_does_not_block_plain_review(self):
        path = ('state/tasks/51be3a29a517c3329933c93fe5dad3f5/'
                'results/review2.json')
        recorded = read_json(REPO_ROOT / path)['result']
        result = {**recorded, 'findings_complete': True}
        self.assertTrue(self.accept(result))
        self.assertFalse(accepted_review(recorded))

    def test_false_verdict_with_passing_score_and_only_minor_findings(self):
        result = review(findings=[finding(severity='minor', fix='A stylistic alternative')])
        self.assertTrue(self.accept(result))
        self.assertTrue(accepted_review(result, contract_version=2))

    def test_false_verdict_without_findings_does_not_override_score(self):
        self.assertTrue(self.accept(review()))
        self.assertFalse(self.accept(review(score=94)))

    def test_genuine_meaning_error_blocks_even_score_100_and_passed_true(self):
        result = review(score=100, passed=True, findings=[finding(fix='Devo confessarlo?')])
        self.assertFalse(self.accept(result))

    def test_no_op_exemption_requires_source_and_candidate_anchors(self):
        result = review(findings=[finding()])
        for changes in ({'source': None}, {'candidate': None},
                        {'source': {'html': '', 'title': 'Unrelated'}},
                        {'candidate': {'html': '', 'title': 'Unrelated'}}):
            with self.subTest(changes=changes):
                self.assertFalse(self.accept(result, **changes))

    def test_empty_evidence_and_real_alternative_remain_blocking(self):
        for item in (finding(source=''), finding(target=''), finding(fix=''),
                     finding(fix='The current wording is correct; change it to Devo confessarlo?')):
            with self.subTest(item=item):
                self.assertTrue(actionable_finding(item, source=self.source, candidate=self.candidate))

    def test_meaning_changing_punctuation_is_not_an_existing_text_fix(self):
        source = {'html': "<p>Let's eat, Grandma.</p>"}
        candidate = {'html': '<p>Comamos abuela.</p>'}
        defect = finding(source="Let's eat, Grandma.", target='Comamos abuela.',
                         fix='Comamos, abuela.')
        self.assertTrue(actionable_finding(defect, source=source, candidate=candidate))
        self.assertFalse(self.accept(review(score=100, findings=[defect]),
                                     source=source, candidate=candidate))

    def test_incomplete_or_malformed_reviews_still_fail_closed(self):
        self.assertFalse(self.accept(review(complete=False, score=100, passed=True)))
        malformed = review()
        malformed['passed'] = 'false'
        with self.assertRaises(ContractError):
            self.accept(malformed)

    def test_accepted_upgrade_requires_98(self):
        campaign = {'quality_threshold': 95, 'upgrade_quality_threshold': 98,
                    'operation': 'review'}
        self.assertEqual(review_threshold(campaign), 95)
        task = {'accepted_baseline': self.candidate}
        self.assertEqual(review_threshold(campaign, task), 98)
        self.assertFalse(self.accept(review(score=97), review_threshold(campaign, task)))
        self.assertTrue(self.accept(review(score=98), review_threshold(campaign, task)))


class RequestState:
    def __init__(self, campaign):
        self.campaign = campaign
        self.source_value = {'html': '<article><p>Faith. John 3:16.</p></article>',
                             'article': {'id': '11111111-1111-4111-8111-111111111111',
                                         'title': 'Faith', 'subtitle': None, 'section': 'Teaching',
                                         'byline': 'Author'}}
        self.candidate_value = {key: self.source_value['article'].get(key)
                                for key in ('title', 'subtitle', 'section')}
        self.candidate_value['html'] = '<article><p>Glaube. Johannes 3:16.</p></article>'

    def read(self, path, default=None):
        return self.campaign if path == 'state/campaigns/plain.json' else default

    def source(self, task):
        return self.source_value

    def candidate(self, task):
        return self.candidate_value


class PlainRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.campaign = {'id': 'plain', 'quality_threshold': 95, 'upgrade_quality_threshold': 98,
                         'review_contract_version': 2,
                         'prompt_version': '2.0.0', 'operation': 'translate',
                         'language_settings': {'deu': {'name': 'German', 'tag': 'de', 'guidance': ''}},
                         'glossaries': {}, 'max_output_tokens': 1000, 'review_output_tokens': 1000,
                         'prompts': {'translation': 'Translate.', 'review': 'Review.', 'repair': 'Repair.'}}
        self.config = SimpleNamespace(runtime={'prompt_version': '2.0.0', 'review_contract_version': 2,
                                              'quality_threshold': 95, 'upgrade_quality_threshold': 98},
                                      prompt=lambda name: self.campaign['prompts'][name])
        self.state = RequestState(self.campaign)
        self.task = {'id': 'task', 'campaign': 'plain', 'stage': 'translate', 'language': 'deu',
                     'model': 'fixture', 'review_model': 'fixture', 'models': {'fixture': {
                         'api_model': 'fixture', 'max_output_tokens': 1000, 'context_tokens': 100000,
                         'input_batch_usd_per_million': 1, 'output_batch_usd_per_million': 1}}}

    def request(self):
        line, _, _ = build_request(self.config, self.state, self.task)
        return line['body'], loads(line['body']['messages'][1]['content'])

    def test_plain_translation_never_loads_scripture_even_when_old_policy_is_present(self):
        self.campaign['scripture_quotes'] = {'malformed_historical_policy': True}
        body, payload = self.request()
        self.assertEqual(body['response_format']['json_schema']['schema']['required'],
                         ['html', 'title', 'subtitle', 'section'])
        self.assertNotIn('scripture_evidence', payload)
        self.assertNotIn('scripture_selection_audit', payload)
        self.assertNotIn('scripture_selections', payload)
        self.assertEqual(payload['quality_threshold'], 95)

    def test_upgrade_baseline_is_sent_to_review_and_correction_at_98(self):
        self.task['accepted_baseline'] = copy.deepcopy(self.state.candidate_value)
        self.task['baseline_quality_score'] = 96
        for stage in ('review1', 'correct', 'review2'):
            with self.subTest(stage=stage):
                self.task['stage'] = stage
                body, payload = self.request()
                self.assertEqual(payload['quality_threshold'], 98)
                self.assertEqual(payload['accepted_baseline'], self.task['accepted_baseline'])
                self.assertEqual(payload['baseline_quality_score'], 96)
                if stage.startswith('review'):
                    self.assertIn('98/100', body['messages'][0]['content'])
                else:
                    self.assertNotIn('controls the verdict', body['messages'][0]['content'])
                self.assertIn('regression', body['messages'][0]['content'])

    def test_review_rubric_belongs_only_to_review_requests(self):
        for stage in ('translate', 'review1', 'review2'):
            with self.subTest(stage=stage):
                self.task['stage'] = stage
                body, payload = self.request()
                system = body['messages'][0]['content']
                self.assertEqual(payload['quality_threshold'], 95)
                if stage.startswith('review'):
                    self.assertIn('95/100', system)
                    self.assertIn('controls the verdict', system)
                else:
                    self.assertEqual(system, self.campaign['prompts']['translation'])

    def test_normal_and_recovery_corrections_require_technical_repairs(self):
        self.task['stage'] = 'correct'
        for recovery in (None, 'downstream_recovery', 'autonomous_recovery'):
            with self.subTest(recovery=recovery):
                for key in ('downstream_recovery', 'autonomous_recovery'):
                    self.task.pop(key, None)
                if recovery:
                    self.task[recovery] = True
                body, payload = self.request()
                system = body['messages'][0]['content']
                self.assertIn('HTML/metadata contract defects even if the meaning is already correct', system)
                self.assertIn('source tags, nesting, order, attributes, IDs, links, image URLs and comments', system)
                self.assertIn('only translatable text and existing alt/title text', system)
                self.assertIn('complete corrected four-field JSON', system)
                self.assertNotIn('controls the verdict', system)
                self.assertEqual(payload['translation'], self.state.candidate_value)

    def test_malformed_upgrade_baseline_fails_before_submission(self):
        self.task['accepted_baseline'] = {'title': 'Incomplete'}
        with self.assertRaises(ContractError):
            self.request()


if __name__ == '__main__':
    unittest.main()
