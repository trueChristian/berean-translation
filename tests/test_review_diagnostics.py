"""Offline diagnostics over copies of the preserved Bengali review evidence."""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import canonical, digest, json_hash
from berean_translation.config import Config
from berean_translation.downstream import frontier
from berean_translation.review_contract import validate_review
from berean_translation.review_diagnostics import preserved_review_diagnostics
from berean_translation.state import State

REPO_ROOT = Path(__file__).resolve().parents[1]
PRESERVED = {
    'eac67cbcb51ae1e038a51c66d4802ec6': {
        'count': 49, 'score': 32, 'severity': {'critical': 13, 'major': 34, 'minor': 2},
        'review_sha256': 'b0e32b9041a8ce67d2e98d7a1dc34007752f4dcdcf3e478a591fa8d3ad3b2051'},
    '43cff43c105045d25904e1e43a91a84b': {
        'count': 32, 'score': 27, 'severity': {'critical': 10, 'major': 21, 'minor': 1},
        'review_sha256': '9f8fc2c319fd2366e5601fbef91eb6a2c4ab988f811e2de4ea257657b2ad74c3'},
}


class ReviewDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(REPO_ROOT / 'config', self.root / 'config')
        shutil.copytree(REPO_ROOT / 'prompts', self.root / 'prompts')
        self.state, original = State(self.root), State(REPO_ROOT)
        self.config = Config(self.root)

        def copy_file(relative):
            destination = self.state.path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original.path(relative), destination)

        article_ids = set()
        for identity in PRESERVED:
            relative = f'state/tasks/{identity}'
            shutil.copytree(original.path(relative), self.state.path(relative))
            task = self.state.read(f'{relative}/task.json')
            article_ids.add(task['article_id'])
            record_path = f'state/records/{task["language"]}/{task["article_id"]}.json'
            copy_file(record_path)
            record = self.state.read(record_path)
            copy_file(f'state/campaigns/{task["campaign"]}.json')
            copy_file(task['source_snapshot'])
            for key in ('html_path', 'metadata_path', 'source_snapshot'):
                copy_file(record['published'][key])
        source = original.read('state/source.json')
        source['articles'] = {key: value for key, value in source['articles'].items() if key in article_ids}
        issue_ids = {value['issue_id'] for value in source['articles'].values()}
        source['issues'] = [issue for issue in source['issues'] if issue['id'] in issue_ids]
        self.state.write('state/source.json', source)
        self.identity = next(iter(PRESERVED))
        self.result_path = f'state/tasks/{self.identity}/results/review1.json'
        self.raw_path = f'state/tasks/{self.identity}/attempts/review1.json'
        self.review = self.state.read(self.result_path)['result']

    def diagnostics(self):
        return preserved_review_diagnostics(self.config, self.state)

    def replace_review(self, review):
        """Mutate only a disposable fixture, keeping its raw evidence coherent."""
        archived = self.state.read(self.result_path)
        archived['result'] = review
        self.state.write(self.result_path, archived)
        attempt = self.state.read(self.raw_path)
        raw = canonical(review)
        attempt['response'].update(content=raw.decode(), content_bytes=len(raw), content_sha256=digest(raw))
        self.state.write(self.raw_path, attempt)

    def immutable_bytes(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for name in ('state', 'content')
                for path in self.state.path(name).rglob('*') if path.is_file()}

    def assert_first_not_classified(self):
        self.assertNotIn(self.identity, {item['task_id'] for item in self.diagnostics()})

    def test_actual_49_and_32_findings_preserve_hashes_publication_decisions_and_task_bytes(self):
        before = self.immutable_bytes()
        published = self.state.projection(self.config)
        recovery = frontier(self.config, self.state)
        diagnostics = self.diagnostics()
        self.assertEqual(len(diagnostics), 2)
        for item in diagnostics:
            expected = PRESERVED[item['task_id']]
            self.assertEqual(item['score'], expected['score'])
            self.assertEqual(item['findings_count'], expected['count'])
            self.assertEqual(item['severity_counts'], expected['severity'])
            self.assertEqual(item['status'], 'not_ready')
            self.assertEqual(item['failure_kind'], 'invalid_result')
            self.assertEqual(item['failure'], 'Invalid review verdict or findings')
            review = self.state.read(item['result_path'])['result']
            self.assertEqual(json_hash(review), expected['review_sha256'])
            attempt = self.state.read(item['raw_path'])['response']
            self.assertEqual(digest(attempt['content']), expected['review_sha256'])
            base = f'state/tasks/{item["task_id"]}/decisions'
            self.assertEqual(self.state.read(f'{base}/review1.json')['findings'], [])
            self.assertEqual(len(self.state.read(f'{base}/terminal.json')['findings']), expected['count'])

        self.state.derive(self.config)
        text = self.state.path('STATUS.md').read_text()
        self.assertIn('## Preserved overlong negative reviews', text)
        self.assertIn('Score 32/100; 49 findings; passed=false', text)
        self.assertIn('Score 27/100; 32 findings; passed=false', text)
        self.assertIn('not_ready / invalid_result: Invalid review verdict or findings', text)
        for item in diagnostics:
            for key in ('result_path', 'raw_path', 'task_path'):
                self.assertIn(f']({item[key]})', text)
        self.assertEqual(self.immutable_bytes(), before)
        self.assertEqual(self.state.read('index.json'), published)
        self.assertEqual(self.state.read('RECOVERY.json'), recovery)
        generated = {name: self.state.path(name).read_bytes() for name in ('STATUS.md', 'index.json', 'RECOVERY.json')}
        self.state.derive(self.config)
        self.assertEqual(generated, {name: self.state.path(name).read_bytes() for name in generated})
        self.assertEqual(self.immutable_bytes(), before)

    def test_every_finding_including_finding_31_must_be_valid(self):
        malformed = ({'severity': 'major'}, {**self.review['findings'][30], 'severity': 'unknown'},
                     {**self.review['findings'][30], 'location': 31})
        for finding in malformed:
            with self.subTest(finding=finding):
                review = copy.deepcopy(self.review)
                review['findings'][30] = finding
                self.replace_review(review)
                self.assert_first_not_classified()

    def test_positive_overflow_is_not_a_usable_negative_review(self):
        review = copy.deepcopy(self.review)
        review.update(passed=True, score=99)
        self.replace_review(review)
        self.assert_first_not_classified()

    def test_entire_canonical_review_must_fit_bound_before_validation(self):
        size = len(canonical(self.review))
        self.config.runtime['max_result_bytes'] = size - 1
        with patch('berean_translation.review_diagnostics.validate_review', wraps=validate_review) as validate:
            self.assert_first_not_classified()
            self.assertNotIn(self.review, [call.args[0] for call in validate.call_args_list])
        self.config.runtime['max_result_bytes'] = size
        self.assertIn(self.identity, {item['task_id'] for item in self.diagnostics()})

    def test_invalid_score_shape_or_non_overflow_is_not_classified(self):
        for changes in ({'score': True}, {'score': 101}, {'extra': 'field'}, {'findings_complete': True},
                        {'passed': 0}, {'findings': self.review['findings'][:30]}):
            with self.subTest(changes=changes):
                self.replace_review({**self.review, **changes})
                self.assert_first_not_classified()

    def test_missing_damaged_truncated_or_mismatched_raw_evidence_is_not_classified(self):
        original = self.state.read(self.raw_path)
        for changes in ({'content_evidence_truncated': True}, {'finish_reason': 'length'},
                        {'outcome': 'provider_refusal'}, {'refusal': 'Refused'},
                        {'content_sha256': '0' * 64}, {'content_bytes': 0}, {'content': '{}'}):
            with self.subTest(changes=changes):
                attempt = copy.deepcopy(original)
                attempt['response'].update(changes)
                self.state.write(self.raw_path, attempt)
                self.assert_first_not_classified()
        self.state.path(self.raw_path).unlink()
        self.assert_first_not_classified()

    def test_only_latest_terminal_legacy_invalid_review_tasks_are_classified(self):
        task_path = f'state/tasks/{self.identity}/task.json'
        original = self.state.read(task_path)
        for changes in ({'status': 'queued'}, {'status': 'complete'}, {'stage': 'translate'},
                        {'failure_kind': 'quality_rejection'}):
            with self.subTest(changes=changes):
                self.state.save_task({**original, **changes})
                self.assert_first_not_classified()
        self.state.save_task(original)
        campaign_path = f'state/campaigns/{original["campaign"]}.json'
        campaign = self.state.read(campaign_path)
        self.state.write(campaign_path, {**campaign, 'review_contract_version': 2})
        self.assertEqual(self.diagnostics(), [])
        self.state.write(campaign_path, campaign)
        record = self.state.record(original['language'], original['article_id'])
        record['latest_task'] = 'newer-task'
        self.state.save_record(record)
        self.assert_first_not_classified()


if __name__ == '__main__':
    unittest.main()
