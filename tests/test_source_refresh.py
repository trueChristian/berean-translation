"""Offline authorization, deduplication, and cost boundaries for source refreshes."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from berean_translation.common import ContractError, read_json, write_json
from berean_translation.config import Config
from berean_translation.html import split_article
from berean_translation.refresh import enqueue_source_refreshes
from support import A, B, ISSUE, ISSUE2, FakeProvider, setup, queue, drive


class SourceRefreshSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.engine.discover()
        self.config.runtime['automatic_source_refresh']['enabled'] = True
        # Selection-only fixtures do not need real publication files. Integration
        # tests below exercise the actual checkpoint/projection and collector.
        self.engine.checkpoint = MagicMock()

    def publish(self, article_id=A, language='afr', key='old-source', human=False):
        record = self.state.record(language, article_id)
        record['published'] = {'translation_key': key, 'human_reviewed': human}
        self.state.save_record(record)

    def task(self, identity, key, status='not_ready', article_id=A, language='afr'):
        task = {'id': identity, 'translation_key': key, 'status': status,
                'article_id': article_id, 'language': language}
        self.state.save_task(task)
        return task

    def current_key(self, article_id=A):
        return self.state.read('state/source.json')['articles'][article_id]['translation_key']

    def requests(self):
        return [read_json(path) for path in sorted((self.root/'state/queue').glob('refresh-*.json'))]

    def test_exactly_one_changed_published_pair_creates_bounded_immutable_request(self):
        self.publish()
        ids = enqueue_source_refreshes(self.engine)
        self.assertEqual(len(ids), 1)
        request = self.requests()[0]
        self.assertEqual(request['id'], ids[0])
        self.assertEqual(request['article_ids'], [A])
        self.assertEqual(request['source_translation_keys'], {A: self.current_key()})
        self.assertEqual(request['issues'], ISSUE)
        self.assertEqual(request['languages'], 'afr')
        self.assertEqual(request['model'], 'gpt-5-mini')
        self.assertEqual(request['review_model'], 'gpt-5-mini')
        self.assertEqual(request['budget_usd'], 10)
        self.assertIs(request['source_refresh'], True)
        self.assertIs(request['retry_failed'], False)
        self.assertIs(request['dry_run'], False)
        self.assertEqual(self.provider.create_calls, 0)
        self.engine.checkpoint.assert_called_once()
        recorded = (self.root/f'state/queue/{ids[0]}.json').read_bytes()
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.assertEqual((self.root/f'state/queue/{ids[0]}.json').read_bytes(), recorded)
        self.engine.checkpoint.assert_called_once()

    def test_unchanged_untranslated_new_language_human_and_removed_sources_do_not_queue(self):
        self.publish(key=self.current_key())
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.publish(human=True)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.publish(article_id='removed-article')
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.assertEqual(self.requests(), [])  # B and every new language remain untouched.

    def test_disabled_policy_never_queues_changed_publications(self):
        self.publish()
        self.config.runtime['automatic_source_refresh']['enabled'] = False
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.engine.checkpoint.assert_not_called()

    def test_any_active_task_blocks_new_source_until_terminal(self):
        self.publish()
        task = self.task('active', 'older-source', status='submitted')
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.upstream.change_source('A newer source. John 3:16–18.')
        self.engine.discover()
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        task['status'] = 'not_ready'; self.state.save_task(task)
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 1)
        self.assertEqual(self.requests()[0]['source_translation_keys'], {A: self.current_key()})

    def test_all_historical_attempts_block_failed_fingerprint_even_when_not_latest(self):
        for status in ('not_ready', 'complete', 'budget_blocked', 'cancelled', 'source_error', 'proposal'):
            with self.subTest(status=status):
                self.publish()
                self.task('historical', self.current_key(), status=status)
                self.task('newer', 'different-source', status='not_ready')
                record = self.state.record('afr', A); record['latest_task'] = 'newer'; self.state.save_record(record)
                self.assertEqual(enqueue_source_refreshes(self.engine), [])

    def test_failed_acceptance_and_git_revision_change_cannot_repeat_same_refresh(self):
        self.publish()
        identity = enqueue_source_refreshes(self.engine)[0]
        self.state.write(f'state/queue-errors/{identity}.json', {'error': 'stale input'})
        source = self.state.read('state/source.json'); source['revision'] = 'f'*40
        self.state.write('state/source.json', source)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.assertEqual(len(self.requests()), 1)

    def test_new_fingerprint_can_queue_but_rollback_to_attempted_key_cannot(self):
        self.publish()
        original = self.state.read('state/source.json')
        self.task('failed-original', self.current_key())
        self.upstream.change_source('A changed source. John 3:16–18.')
        self.engine.discover()
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 1)
        self.state.write('state/source.json', original)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])

    def test_pending_manual_request_suppresses_overlapping_auto_campaign(self):
        self.publish()
        queue(self.state, issues=ISSUE, languages='afr')
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.state.write('state/queue-errors/request-1.json', {'error': 'invalid manual request'})
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 1)

    def test_nonoverlapping_manual_request_does_not_block_refresh(self):
        self.publish()
        queue(self.state, languages='deu')
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 1)

    def test_pending_manual_issue_selectors_support_commas_spaces_and_newlines(self):
        self.publish()
        source = self.state.read('state/source.json')
        source['issues'].append({'id': ISSUE2, 'slug': 'other-issue', 'source_id': 'other-issue'})
        self.state.write('state/source.json', source)
        for delimiter in (',', ' ', '\n', '\t'):
            with self.subTest(delimiter=delimiter):
                queue(self.state, issues=ISSUE2 + delimiter + ISSUE, languages='afr')
                self.assertEqual(enqueue_source_refreshes(self.engine), [])

    def test_groups_are_one_issue_and_language_with_five_campaign_limit(self):
        source = self.state.read('state/source.json')
        source['issues'].append({'id': ISSUE2, 'slug': 'other-issue', 'source_id': 'other-issue'})
        source['articles'][B]['issue_id'] = ISSUE2
        self.state.write('state/source.json', source)
        for language in list(self.config.languages)[:3]:
            self.publish(A, language); self.publish(B, language)
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 5)
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 1)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        pairs = set()
        for request in self.requests():
            pairs.add((request['issues'], request['languages']))
            self.assertEqual(len(request['article_ids']), 1)
            article = source['articles'][request['article_ids'][0]]
            self.assertEqual(article['issue_id'], request['issues'])
        self.assertEqual(len(pairs), 6)

    def test_same_issue_groups_only_stale_published_articles(self):
        self.publish(A); self.publish(B)
        self.assertEqual(len(enqueue_source_refreshes(self.engine)), 1)
        self.assertEqual(self.requests()[0]['article_ids'], sorted([A, B]))

    def test_oversized_group_is_not_split_into_multiple_funded_campaigns(self):
        self.publish(A); self.publish(B)
        self.config.runtime['max_tasks_per_request'] = 1
        identity = enqueue_source_refreshes(self.engine)[0]
        self.assertEqual(self.requests()[0]['article_ids'], sorted([A, B]))
        self.engine.accept_queue()
        error = self.state.read(f'state/queue-errors/{identity}.json')
        self.assertTrue(error['error'])
        self.assertIsNone(self.state.read(f'state/campaigns/{identity}.json'))
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        self.assertEqual(self.provider.create_calls, 0)

    def test_acceptance_rejects_malformed_or_expanded_automatic_authority(self):
        self.publish()
        request = self.state.read(f'state/queue/{enqueue_source_refreshes(self.engine)[0]}.json')
        source = self.state.read('state/source.json')
        source['issues'].append({'id': ISSUE2, 'slug': 'other-issue', 'source_id': 'other-issue'})
        self.state.write('state/source.json', source)
        cases = [('article_ids', []), ('article_ids', [A, A]), ('article_ids', [[A]]),
                 ('source_translation_keys', {}), ('source_translation_keys', {A: 'invalid'}),
                 ('source_translation_keys', []), ('languages', 'afr,deu'),
                 ('issues', ISSUE + ',' + ISSUE2), ('issues', ISSUE2), ('budget_usd', 11),
                 ('model', 'gpt-4.1-mini'), ('review_model', 'gpt-4.1-mini'),
                 ('retry_failed', True), ('dry_run', True), ('source_refresh', 'true'),
                 ('source_refresh', False)]
        for index, (field, value) in enumerate(cases):
            candidate = {**request, 'id': f'invalid-refresh-{index}', field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                self.engine.accept_request(candidate)
            self.assertIsNone(self.state.read(f'state/campaigns/{candidate["id"]}.json'))
        self.assertEqual(self.provider.create_calls, 0)

    def test_acceptance_rechecks_publication_and_current_fingerprint(self):
        self.publish()
        request = self.state.read(f'state/queue/{enqueue_source_refreshes(self.engine)[0]}.json')
        for index, publication in enumerate((None, {'translation_key': self.current_key(), 'human_reviewed': False},
                                             {'translation_key': 'old-source', 'human_reviewed': True})):
            record = self.state.record('afr', A); record['published'] = publication; self.state.save_record(record)
            campaign = self.engine.accept_request({**request, 'id': f'recheck-refresh-{index}'})
            self.assertEqual(campaign['tasks'], [])
        self.assertEqual(self.provider.create_calls, 0)


class SourceRefreshIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        queue(self.state); drive(self.engine, self.provider)
        self.old_publication = copy.deepcopy(self.state.record('afr', A)['published'])
        self.old_html = self.state.path(self.old_publication['html_path']).read_bytes()
        self.config.runtime['automatic_source_refresh']['enabled'] = True
        self.upstream.change_source()
        self.engine.discover()

    def test_successful_refresh_uses_exact_pair_and_preserves_other_article(self):
        identities = enqueue_source_refreshes(self.engine)
        self.assertEqual(len(identities), 1)
        drive(self.engine, self.provider)
        campaign = self.state.read(f'state/campaigns/{identities[0]}.json')
        self.assertEqual(len(campaign['tasks']), 1)
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.assertEqual((task['article_id'], task['language'], task['status']), (A, 'afr', 'complete'))
        self.assertEqual(task['model'], 'gpt-5-mini')
        self.assertNotEqual(self.state.record('afr', A)['published']['translation_key'], self.old_publication['translation_key'])
        self.assertEqual(len(self.state.tasks()), 3)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])

    def test_new_article_added_before_acceptance_cannot_expand_refresh_selection(self):
        identity = enqueue_source_refreshes(self.engine)[0]
        new_id = '55555555-5555-4555-8555-555555555555'
        article = copy.deepcopy(self.upstream.articles[1])
        article['id'], article['sequence'] = new_id, 3
        old_path = article['html']['repository_path']
        article['html']['repository_path'] = old_path.replace(B, new_id)
        article['images'][0]['public_path'] = article['images'][0]['public_path'].replace(B, new_id)
        self.upstream.contents[article['html']['repository_path']] = self.upstream.contents[old_path].replace(B, new_id)
        self.upstream.articles.append(article); self.upstream.rebuild()
        drive(self.engine, self.provider)
        campaign = self.state.read(f'state/campaigns/{identity}.json')
        self.assertEqual([row['article_id'] for row in campaign['selection']], [A])
        self.assertIsNone(self.state.record('afr', new_id)['published'])
        self.assertEqual(len(self.state.tasks()), 3)

    def test_human_review_after_queueing_prevents_automatic_replacement(self):
        identity = enqueue_source_refreshes(self.engine)[0]
        path = self.state.path(self.old_publication['html_path'])
        path.write_text(split_article(path.read_text())[0] + '\n')
        calls = self.provider.create_calls
        drive(self.engine, self.provider)
        self.assertTrue(self.state.record('afr', A)['published']['human_reviewed'])
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.state.read(f'state/campaigns/{identity}.json')['tasks'], [])

    def test_source_change_before_acceptance_defers_to_a_new_exact_request(self):
        identity = enqueue_source_refreshes(self.engine)[0]
        original_keys = self.state.read(f'state/queue/{identity}.json')['source_translation_keys']
        path = f'content/articles/{A}.html'
        self.upstream.contents[path] = self.upstream.contents[path].replace('Grace changed.', 'Changed again.')
        self.upstream.revision = 'c'*40
        self.upstream.rebuild()
        calls = self.provider.create_calls
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.state.read(f'state/campaigns/{identity}.json')['tasks'], [])
        self.engine.tick()
        refreshes = [campaign for campaign in self.state.campaigns() if campaign.get('source_refresh')]
        self.assertEqual(len(refreshes), 2)
        current = next(campaign for campaign in refreshes if campaign['id'] != identity)
        self.assertNotEqual(current['source_translation_keys'], original_keys)
        self.assertEqual(len(current['tasks']), 1)

    def test_disabled_policy_pauses_existing_queue_and_reenable_accepts_once(self):
        identity = enqueue_source_refreshes(self.engine)[0]
        self.config.runtime['automatic_source_refresh']['enabled'] = False
        calls = self.provider.create_calls
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, calls)
        self.assertIsNone(self.state.read(f'state/campaigns/{identity}.json'))
        self.assertIsNone(self.state.read(f'state/queue-errors/{identity}.json'))
        self.config.runtime['automatic_source_refresh']['enabled'] = True
        drive(self.engine, self.provider)
        self.assertEqual(len(self.state.read(f'state/campaigns/{identity}.json')['tasks']), 1)
        self.assertEqual(len(self.state.tasks()), 3)

    def test_immutable_queue_checkpoint_precedes_every_paid_creation(self):
        self.git.checkpoints.clear()
        identity = enqueue_source_refreshes(self.engine)[0]
        original_create = self.provider.create
        observed = []
        def verify_before_create(*args):
            self.assertIsNotNone(self.state.read(f'state/queue/{identity}.json'))
            messages = self.git.checkpoints
            queued = messages.index('runtime: queue bounded automatic source refresh requests')
            reserved = messages.index('runtime: reserve task identities and budget before OpenAI submission')
            intent = messages.index('runtime: record batch submission intent before the billable request')
            self.assertLess(queued, reserved)
            self.assertLess(reserved, intent)
            observed.append(True)
            return original_create(*args)
        self.provider.create = verify_before_create
        drive(self.engine, self.provider)
        self.assertEqual(len(observed), 2)

    def test_failed_refresh_preserves_old_good_and_never_retries_same_source(self):
        identity = enqueue_source_refreshes(self.engine)[0]
        def fail_reviews(line):
            if ':review' in line['custom_id']:
                return {'score': 90, 'passed': False, 'findings': [{
                    'severity': 'major', 'location': 'paragraph', 'source_quote': 'Source',
                    'translation_quote': 'Candidate', 'suggested_fix': 'Preserve meaning'}]}
            return FakeProvider.default_result(line)
        drive(self.engine, self.provider, fail_reviews, ticks=10)
        campaign = self.state.read(f'state/campaigns/{identity}.json')
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.assertEqual(task['status'], 'not_ready')
        self.assertEqual(task['translation_attempts'], 2)
        self.assertEqual(task['review_attempts'], 2)
        self.assertLessEqual(campaign['reserved_usd'], 10)
        self.assertEqual(self.state.record('afr', A)['published'], self.old_publication)
        self.assertEqual(self.state.path(self.old_publication['html_path']).read_bytes(), self.old_html)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        calls = self.provider.create_calls
        self.engine.tick(); self.engine.tick()
        self.assertEqual(self.provider.create_calls, calls)

    def test_budget_block_before_submission_keeps_old_publication(self):
        self.config.runtime['automatic_source_refresh']['budget_usd'] = 0.000001
        calls = self.provider.create_calls
        identity = enqueue_source_refreshes(self.engine)[0]
        drive(self.engine, self.provider)
        campaign = self.state.read(f'state/campaigns/{identity}.json')
        task = self.state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
        self.assertEqual(task['status'], 'budget_blocked')
        self.assertEqual(task['translation_attempts'], 0)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.state.record('afr', A)['published'], self.old_publication)
        self.assertEqual(enqueue_source_refreshes(self.engine), [])


class SourceRefreshConfigTests(unittest.TestCase):
    def test_production_policy_is_enabled_and_bounded_without_new_translation(self):
        config = Config(Path(__file__).resolve().parents[1])
        self.assertIs(config.runtime['automatic_new_translation'], True)
        self.assertEqual(config.runtime['autonomous_translation']['total_budget_usd'], 30)
        self.assertEqual(config.runtime['automatic_source_refresh'], {
            'enabled': True, 'model': 'gpt-5-mini', 'review_model': 'gpt-5-mini',
            'budget_usd': 10, 'max_campaigns_per_tick': 5})

    def test_invalid_or_expanded_refresh_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, *_ = setup(root)
            original = copy.deepcopy(config.runtime)
            cases = [('enabled', 'true'), ('model', 'gpt-4.1-mini'), ('review_model', 'gpt-4.1-mini'),
                     ('budget_usd', 11), ('budget_usd', 0), ('budget_usd', True),
                     ('max_campaigns_per_tick', 6), ('max_campaigns_per_tick', 0),
                     ('max_campaigns_per_tick', True), ('unexpected', 'value')]
            for key, value in cases:
                runtime = copy.deepcopy(original); runtime['automatic_source_refresh'][key] = value
                write_json(root/'config/runtime.json', runtime)
                with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                    Config(root)


if __name__ == '__main__':
    unittest.main()
