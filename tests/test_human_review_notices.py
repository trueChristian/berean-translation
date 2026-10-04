"""Offline regressions for the three authentic retained Afrikaans review notices.

Only their public notice tails/provenance are fixtures. Article bodies, provider
responses, source inventories and reviewers used by the runtime are synthetic.
"""
from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

from berean_translation.common import ContractError, digest, read_json
from berean_translation.gitstore import GitStore
from berean_translation.html import human_notice, split_article
from berean_translation.refresh import enqueue_source_refreshes
from berean_translation.validation import export, validate_repository
from support import A, drive, queue, setup


FIXTURES = read_json(Path(__file__).parent / 'fixtures/human_review_notices.json')['cases']
UNREVIEWED = 'en is nog nie deur ’n mens nagegaan nie.'
REVIEWED = {1: 'en is een keer deur ’n mens nagegaan.',
            2: 'en is twee keer deur ’n mens nagegaan.'}


class HumanReviewNoticeTests(unittest.TestCase):
    def setUp(self):
        self.prepare(FIXTURES[0])

    def prepare(self, case):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.case = case
        self.article_id = case['article_id']
        # Keep support's artificial prose/images, but exercise each incident's
        # exact article identity and model so its retained notice is verbatim.
        article = copy.deepcopy(self.upstream.articles[0])
        original_path = article['html']['repository_path']
        article['id'] = self.article_id
        article['html']['repository_path'] = original_path.replace(A, self.article_id)
        article['images'][0]['public_path'] = article['images'][0]['public_path'].replace(A, self.article_id)
        self.upstream.contents[article['html']['repository_path']] = self.upstream.contents[original_path].replace(A, self.article_id)
        self.upstream.articles = [article]
        self.upstream.rebuild()
        model = 'gpt-5-mini' if case['model'].startswith('gpt-5-mini-') else case['model']
        queue(self.state, model=model, review_model=model)
        drive(self.engine, self.provider)
        self.original_publication = copy.deepcopy(self.publication())
        self.original_html = self.path().read_text(encoding='utf-8')
        self.body = split_article(self.original_html)[0]
        self.assertEqual(self.publication()['model'], case['model'])
        self.assertEqual(self.original_publication['notice_html'].replace(
            UNREVIEWED, REVIEWED[case['review_count']]), case['notice_html'])

    def publication(self):
        return self.state.record('afr', self.article_id)['published']

    def path(self):
        return self.state.path(self.publication()['html_path'])

    def replace_notice(self, tail):
        self.path().write_text(self.body + ('\n\n' + tail if tail else '') + '\n', encoding='utf-8')

    def acknowledge(self, count=None):
        tail = self.case['notice_html'] if count is None else self.original_publication['notice_html'].replace(
            UNREVIEWED, REVIEWED[count])
        self.replace_notice(tail)
        self.engine.tick()
        return split_article(self.path().read_text())[1]

    def human_tail(self):
        return human_notice(self.config.languages['afr'], self.article_id, self.config.runtime['english_route'])

    def assert_human_footer(self, text):
        tail = split_article(text)[1]
        self.assertEqual(tail, self.human_tail())
        self.assertNotIn('gpt-', tail)
        self.assertNotIn(self.case['model'], tail)
        self.assertIn(f'href="/en/articles/{self.article_id}/"', tail)
        self.assertIn('data-translation-notice="human-reviewed"', tail)
        return tail

    def git_command(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, author='Fixture reviewer', email='reviewer@example.test', paths=None):
        self.git_command('add', '--', *(paths or ['.']))
        self.git_command('-c', 'user.name=' + author, '-c', 'user.email=' + email,
                         'commit', '-m', 'Offline fixture content edit')
        return self.git_command('rev-parse', 'HEAD')

    def use_real_git(self):
        self.git_command('init', '-b', 'main')
        self.commit('github-actions[bot]', '41898282+github-actions[bot]@users.noreply.github.com')
        self.engine.gitstore = GitStore(self.root)

    def assert_publication_consumers_reject(self):
        with self.assertRaises(ContractError):
            self.state.projection(self.config)
        with self.assertRaises(ContractError):
            validate_repository(self.config, check_index=False)
        with self.assertRaises(ContractError):
            export(self.config, self.root / '.build/rejected', self.upstream.client.discover(),
                   self.upstream.revision, translation_revision='d' * 40)

    def test_all_three_exact_human_notices_sync_validate_and_export(self):
        for case in FIXTURES:
            with self.subTest(article_id=case['article_id'], model=case['model']):
                self.prepare(case)
                before_calls = self.provider.create_calls
                self.replace_notice(case['notice_html'])
                _, tail, _ = self.state.publication_candidate(self.publication(), working=True)
                self.assertEqual(tail, case['notice_html'])
                # Recognizing a possible notice is not itself human evidence.
                self.assert_publication_consumers_reject()
                self.engine.tick()
                publication = self.publication()
                self.assertIs(publication['human_reviewed'], True)
                self.assertEqual(publication['human_review']['author'], 'Fixture reviewer')
                self.assertEqual(publication['human_review']['commit'], 'c' * 40)
                self.assertEqual(publication['human_review_notice_html'], self.human_tail())
                for key in ('notice_html', 'model', 'review_model', 'source_snapshot',
                            'source_revision', 'translation_key', 'task'):
                    self.assertEqual(publication[key], self.original_publication[key], key)
                self.assertIn(UNREVIEWED, publication['notice_html'])
                self.assertEqual(validate_repository(self.config)['ready'], 1)
                item = self.state.projection(self.config)['articles'][0]
                self.assertIs(item['human_reviewed'], True)
                self.assertIs(item['ai_notice_required'], False)
                self.assertEqual(item['translation_model'], case['model'])
                history = copy.deepcopy(self.state.record('afr', self.article_id)['history'])
                self.engine.tick()
                self.assertEqual(self.state.record('afr', self.article_id)['history'], history)
                self.assertEqual(self.provider.create_calls, before_calls)

                destination = self.root / '.build/export'
                result = export(self.config, destination, self.upstream.client.discover(),
                                self.upstream.revision, translation_revision='d' * 40)
                self.assertEqual(result['article_count'], 1)
                exported = read_json(destination / 'index.json')['articles'][0]
                output = (destination / exported['html']).read_text(encoding='utf-8')
                self.assert_human_footer(output)
                self.assertEqual(split_article(output)[0], self.body)
                self.assertEqual(exported['html_sha256'], digest(output))
                self.assertIs(exported['human_reviewed'], True)
                self.assertIs(exported['ai_notice_required'], False)
                self.assertEqual(exported['translation_model'], case['model'])
                self.assertIn(f'href="/en/articles/{self.article_id}/"', output)

    def assert_quarantined(self, accepted, expected_text=None):
        publication = self.publication()
        self.assertTrue(publication.get('edit_issue'))
        for key in ('html_sha256', 'metadata_sha256', 'human_reviewed', 'human_review',
                    'model', 'review_model', 'source_snapshot', 'translation_key', 'notice_html'):
            self.assertEqual(publication[key], accepted[key], key)
        candidate, _, text = self.state.publication_candidate(publication)
        self.assertEqual(digest(text), accepted['html_sha256'])
        if expected_text is not None:
            self.assertEqual(text, expected_text)
        self.assertEqual(validate_repository(self.config)['ready'], 1)
        return candidate, text

    def test_human_body_edit_is_reviewed_even_with_original_unreviewed_notice(self):
        self.use_real_git()
        edited = self.original_html.replace('Faith and', 'Belief and')
        self.path().write_text(edited, encoding='utf-8')
        commit = self.commit(paths=[self.publication()['html_path']])
        self.engine.tick()
        publication = self.publication()
        self.assertIs(publication['human_reviewed'], True)
        self.assertEqual(publication['human_review']['commit'], commit)
        self.assertEqual(split_article(self.path().read_text())[0], split_article(edited)[0])
        self.assert_human_footer(self.path().read_text())
        self.assertEqual(publication['human_review_notice_html'], self.human_tail())
        self.assertIs(self.state.projection(self.config)['articles'][0]['ai_notice_required'], False)
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_sidecar_only_human_edit_is_reviewed(self):
        self.use_real_git()
        metadata_path = self.publication()['metadata_path']
        metadata = self.state.read(metadata_path)
        metadata['title'] = 'A human supplied title'
        self.state.write(metadata_path, metadata)
        commit = self.commit(paths=[metadata_path])
        self.engine.tick()
        self.assertIs(self.publication()['human_reviewed'], True)
        self.assertEqual(self.publication()['human_review']['commit'], commit)
        self.assertEqual(self.state.projection(self.config)['articles'][0]['title'], metadata['title'])
        self.assertEqual(split_article(self.path().read_text())[0], self.body)
        self.assert_human_footer(self.path().read_text())
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_human_body_and_reference_changes_do_not_need_ai_source_parity(self):
        self.use_real_git()
        edited = self.original_html.replace('Faith and <em>grace</em>. John 3:16–18.',
            'A human changed the wording. John 3:19–21.').replace(
            '</article>', '<p>A paragraph added by the human editor.</p></article>')
        self.path().write_text(edited, encoding='utf-8')
        commit = self.commit(paths=[self.publication()['html_path']])
        before_calls = self.provider.create_calls
        self.engine.tick()
        self.assertIs(self.publication()['human_reviewed'], True)
        self.assertEqual(self.publication()['human_review']['commit'], commit)
        self.assertEqual(split_article(self.path().read_text())[0], split_article(edited)[0])
        self.assert_human_footer(self.path().read_text())
        self.assertFalse(self.publication().get('edit_issue'))
        self.assertEqual(validate_repository(self.config)['ready'], 1)
        self.assertEqual(self.provider.create_calls, before_calls)
        destination = self.root / '.build/human-body-export'
        self.commit('github-actions[bot]', 'bot@example.test')
        export(self.config, destination, self.upstream.client.discover(), self.upstream.revision)
        exported = read_json(destination / 'index.json')['articles'][0]
        self.assertEqual(split_article((destination / exported['html']).read_text())[0], split_article(edited)[0])
        self.assert_human_footer((destination / exported['html']).read_text())

    def test_bot_edits_are_isolated_without_granting_human_review(self):
        for case in FIXTURES:
            with self.subTest(article_id=case['article_id']):
                self.prepare(case)
                self.use_real_git()
                self.replace_notice(case['notice_html'])
                edited = self.path().read_bytes()
                self.commit('github-actions[bot]', '41898282+github-actions[bot]@users.noreply.github.com',
                            paths=[self.publication()['html_path']])
                self.engine.tick()
                self.assert_quarantined(self.original_publication, self.original_html)
                self.assertEqual(self.path().read_bytes(), edited)
                self.assertIs(self.publication()['human_reviewed'], False)
                history = copy.deepcopy(self.state.record('afr', self.article_id)['history'])
                self.engine.tick()
                self.assertEqual(self.state.record('afr', self.article_id)['history'], history)

    def test_arbitrary_human_notice_wording_becomes_canonical_without_gating(self):
        good = self.case['notice_html']
        variants = {
            'original unreviewed wording': self.original_publication['notice_html'],
            'different provider wording': good.replace('OpenAI', 'AI-assisted draft'),
            'different model wording': good.replace(self.case['model'], 'the previous model'),
            'arbitrary review count': good.replace(REVIEWED[1], 'en is drie keer deur ’n mens nagegaan.'),
            'arbitrary acknowledgement': good.replace(REVIEWED[1], 'Roline het die vertaling nagegaan.'),
            'removed acknowledgement': good.replace(REVIEWED[1], ''),
            'removed marker': good.replace(' data-translation-notice="ai"', ''),
            'changed marker': good.replace('data-translation-notice="ai"', 'data-translation-notice="human"'),
            'changed markup': good.replace('<p>', '<div>').replace('</p>', '</div>'),
            'extra human note': good + '\n<p>Reviewed and corrected by a human editor.</p>',
            'incomplete notice': good.replace('</aside>', ''),
            'unsafe discarded notice': good.replace('</aside>', '<script>alert(1)</script></aside>'),
        }
        for label, tail in variants.items():
            with self.subTest(variant=label):
                self.prepare(FIXTURES[0])
                self.use_real_git()
                # Ensure the original-notice case is still an actual human edit.
                self.body = self.body.replace('Faith and', 'Belief and')
                self.replace_notice(tail)
                edited = self.path().read_text()
                commit = self.commit(paths=[self.publication()['html_path']])
                self.engine.tick()
                self.assertIs(self.publication()['human_reviewed'], True)
                self.assertEqual(self.publication()['human_review']['commit'], commit)
                self.assertFalse(self.publication().get('edit_issue'))
                self.assertEqual(split_article(self.path().read_text())[0], split_article(edited)[0])
                self.assert_human_footer(self.path().read_text())
                self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_unsafe_html_and_broken_identity_are_isolated_and_last_good_exports(self):
        mutations = {
            'script in body': self.original_html.replace('</article>', '<script>alert(1)</script></article>'),
            'event attribute': self.original_html.replace('<p>', '<p onclick="alert(1)">', 1),
            'unsafe URL': self.original_html.replace('</article>', '<a href="javascript:alert(1)">Unsafe</a></article>'),
            'wrong identity': self.original_html.replace(f'data-article-id="{self.article_id}"', f'data-article-id="{A}"'),
            'incomplete body': self.original_html.replace('</article>', ''),
        }
        for index, (label, edited) in enumerate(mutations.items()):
            with self.subTest(mutation=label):
                self.prepare(FIXTURES[0])
                self.path().write_text(edited, encoding='utf-8')
                self.engine.tick()
                self.assert_quarantined(self.original_publication, self.original_html)
                self.assertEqual(self.path().read_text(), edited)
                destination = self.root / f'.build/quarantine-export-{index}'
                result = export(self.config, destination, self.upstream.client.discover(),
                                self.upstream.revision, translation_revision='d' * 40)
                self.assertEqual(result['article_count'], 1)
                exported = read_json(destination / 'index.json')['articles'][0]
                self.assertEqual((destination / exported['html']).read_text(), self.original_html)
                self.assertIs(exported['human_reviewed'], False)
                self.assertIs(exported['ai_notice_required'], True)

    def test_invalid_sidecar_is_isolated_and_last_good_metadata_exports(self):
        for index, malformed in enumerate(('{', '{"title":"first","title":"second"}',
                                            '[]', '{"title":42,"subtitle":null,"section":"Teaching"}')):
            with self.subTest(sidecar=malformed):
                self.prepare(FIXTURES[0])
                path = self.state.path(self.publication()['metadata_path'])
                original = self.state.read(self.publication()['metadata_path'])
                path.write_text(malformed)
                self.engine.tick()
                candidate, _ = self.assert_quarantined(self.original_publication, self.original_html)
                self.assertEqual({key:candidate[key] for key in original}, original)
                self.assertEqual(path.read_text(), malformed)
                destination = self.root / f'.build/metadata-export-{index}'
                export(self.config, destination, self.upstream.client.discover(),
                       self.upstream.revision, translation_revision='d' * 40)
                exported = read_json(destination / 'index.json')['articles'][0]
                self.assertEqual(read_json(destination / exported['metadata']), original)

    def test_reviewer_count_changes_require_new_human_commit(self):
        self.use_real_git()
        before_calls = self.provider.create_calls
        commits = []
        for count in (1, 2, 1):
            with self.subTest(count=count, revision=len(commits)):
                tail = self.original_publication['notice_html'].replace(UNREVIEWED, REVIEWED[count])
                self.replace_notice(tail)
                commit = self.commit(paths=[self.publication()['html_path']])
                commits.append(commit)
                self.engine.tick()
                publication = self.publication()
                self.assertIs(publication['human_reviewed'], True)
                self.assertEqual(publication['human_review']['commit'], commit)
                self.assertEqual(publication['human_review_notice_html'], self.human_tail())
                self.assertEqual(publication['notice_html'], self.original_publication['notice_html'])
                self.assertEqual(validate_repository(self.config)['ready'], 1)
        self.assertEqual(len(set(commits)), 3)
        self.assertEqual(self.provider.create_calls, before_calls)

    def test_staged_or_unstaged_change_is_isolated_until_human_commit(self):
        self.use_real_git()
        self.replace_notice(self.case['notice_html'])
        self.commit(paths=[self.publication()['html_path']])
        self.engine.tick()
        accepted = copy.deepcopy(self.publication())
        accepted_text = self.path().read_text()
        twice = self.original_publication['notice_html'].replace(UNREVIEWED, REVIEWED[2])
        self.replace_notice(twice)
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.git_command('add', '--', accepted['html_path'])
                self.engine.tick()
                self.assert_quarantined(accepted, accepted_text)
                self.assertEqual(split_article(self.path().read_text())[1], twice)
        commit = self.commit(paths=[accepted['html_path']])
        self.engine.tick()
        self.assertFalse(self.publication().get('edit_issue'))
        self.assertEqual(self.publication()['human_review']['commit'], commit)
        self.assertEqual(self.publication()['human_review_notice_html'], self.human_tail())
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_bot_change_cannot_replace_an_existing_human_publication(self):
        self.use_real_git()
        self.replace_notice(self.case['notice_html'])
        self.commit(paths=[self.publication()['html_path']])
        self.engine.tick()
        accepted = copy.deepcopy(self.publication())
        accepted_text = self.path().read_text()
        self.replace_notice(self.original_publication['notice_html'].replace(UNREVIEWED, REVIEWED[2]))
        self.commit('github-actions[bot]', 'bot@example.test', paths=[accepted['html_path']])
        self.engine.tick()
        self.assert_quarantined(accepted, accepted_text)

    def test_human_html_commit_cannot_cover_uncommitted_or_bot_sidecar_edit(self):
        for sidecar_commit in (None, 'bot', 'human'):
            with self.subTest(sidecar_commit=sidecar_commit):
                self.prepare(FIXTURES[0])
                self.use_real_git()
                self.replace_notice(self.case['notice_html'])
                html_commit = self.commit(paths=[self.publication()['html_path']])
                metadata_path = self.publication()['metadata_path']
                metadata = self.state.read(metadata_path)
                metadata['title'] = 'Reviewed fixture title'
                self.state.write(metadata_path, metadata)
                if sidecar_commit == 'bot':
                    self.commit('github-actions[bot]', 'bot@example.test', paths=[metadata_path])
                elif sidecar_commit == 'human':
                    self.commit(paths=[metadata_path])
                self.engine.tick()
                if sidecar_commit == 'human':
                    self.assertIs(self.publication()['human_reviewed'], True)
                    self.assertEqual(self.publication()['human_review']['commit'], html_commit)
                    self.assertFalse(self.publication().get('edit_issue'))
                    self.assertEqual(validate_repository(self.config)['ready'], 1)
                else:
                    self.assert_quarantined(self.original_publication, self.original_html)

    def test_notice_reversion_does_not_erase_genuine_human_attribution(self):
        self.use_real_git()
        self.replace_notice(self.case['notice_html'])
        self.commit(paths=[self.publication()['html_path']])
        self.engine.tick()
        self.replace_notice(self.original_publication['notice_html'])
        commit = self.commit(paths=[self.publication()['html_path']])
        self.engine.tick()
        publication = self.publication()
        self.assertIs(publication['human_reviewed'], True)
        self.assertEqual(publication['human_review']['commit'], commit)
        self.assertEqual(publication['human_review_notice_html'], self.human_tail())
        self.assertIs(self.state.projection(self.config)['articles'][0]['ai_notice_required'], False)
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_restoring_accepted_bytes_clears_quarantine_without_new_review(self):
        self.path().write_text(self.original_html.replace('</article>', '<script>1</script></article>'))
        self.engine.tick()
        self.assert_quarantined(self.original_publication, self.original_html)
        self.path().write_text(self.original_html)
        self.engine.tick()
        self.assertFalse(self.publication().get('edit_issue'))
        self.assertIs(self.publication()['human_reviewed'], False)
        self.assertIsNone(self.publication()['human_review'])
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_legacy_complete_notice_removal_stays_supported(self):
        for was_acknowledged in (False, True):
            with self.subTest(was_acknowledged=was_acknowledged):
                self.prepare(FIXTURES[0])
                if was_acknowledged:
                    self.acknowledge()
                self.replace_notice('')
                self.engine.tick()
                publication = self.publication()
                self.assertIs(publication['human_reviewed'], True)
                self.assertIsNotNone(publication['human_review'])
                self.assertEqual(publication['human_review_notice_html'], self.human_tail())
                self.assert_human_footer(self.path().read_text())
                self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_human_review_is_excluded_from_new_manual_ai_review(self):
        self.acknowledge()
        before = self.path().read_bytes()
        publication = copy.deepcopy(self.publication())
        tasks = copy.deepcopy(self.state.tasks())
        calls = self.provider.create_calls
        queue(self.state, 'rereview', operation='review', issues='all', retry_failed=True)
        drive(self.engine, self.provider)
        self.assertEqual(self.state.read('state/campaigns/rereview.json')['tasks'], [])
        self.assertEqual(self.state.tasks(), tasks)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.publication(), publication)
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_changed_human_references_still_excluded_from_forced_ai_review(self):
        edited = self.original_html.replace('John 3:16–18.', 'John 3:19–21.').replace(
            '</article>', '<p>A new human paragraph.</p></article>')
        self.path().write_text(edited)
        self.engine.tick()
        self.assertIs(self.publication()['human_reviewed'], True)
        publication = copy.deepcopy(self.publication())
        calls = self.provider.create_calls
        tasks = copy.deepcopy(self.state.tasks())
        queue(self.state, 'review-human-reference', operation='review', issues='all', retry_failed=True)
        drive(self.engine, self.provider)
        self.assertEqual(self.state.read('state/campaigns/review-human-reference.json')['tasks'], [])
        self.assertEqual(self.state.tasks(), tasks)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.publication(), publication)
        self.assertEqual(split_article(self.path().read_text())[0], split_article(edited)[0])
        self.assert_human_footer(self.path().read_text())
        self.assertEqual(validate_repository(self.config)['ready'], 1)

    def test_human_acknowledgement_during_ai_review_protects_publication(self):
        queue(self.state, 'inflight-review', operation='review', issues='all')
        self.engine.tick()
        tail = self.acknowledge()
        before = self.path().read_bytes()
        drive(self.engine, self.provider)
        record = self.state.record('afr', self.article_id)
        task = self.state.read(f'state/tasks/{record["latest_task"]}/task.json')
        self.assertEqual(task['status'], 'proposal')
        self.assertIs(record['published']['human_reviewed'], True)
        self.assertEqual(record['published']['human_review_notice_html'], tail)
        self.assertEqual(self.path().read_bytes(), before)

    def test_forced_retranslation_after_source_change_cannot_touch_human_publication(self):
        self.acknowledge()
        before = self.path().read_bytes()
        publication = copy.deepcopy(self.publication())
        tasks = copy.deepcopy(self.state.tasks())
        calls = self.provider.create_calls
        source_path = self.upstream.articles[0]['html']['repository_path']
        self.upstream.contents[source_path] = self.upstream.contents[source_path].replace('Faith and', 'Trust and')
        self.upstream.revision = 'b' * 40
        self.upstream.rebuild()
        queue(self.state, 'forced-translation', operation='translate', issues='all', retry_failed=True)
        drive(self.engine, self.provider)
        self.assertEqual(self.state.read('state/campaigns/forced-translation.json')['tasks'], [])
        self.assertEqual(self.state.tasks(), tasks)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.publication(), publication)
        self.assertEqual(self.path().read_bytes(), before)
        self.assertIs(self.state.projection(self.config)['articles'][0]['human_reviewed'], True)

    def test_source_refresh_skips_acknowledged_human_publication(self):
        self.acknowledge()
        before_calls = self.provider.create_calls
        before = self.path().read_bytes()
        self.config.runtime['automatic_source_refresh']['enabled'] = True
        source_path = self.upstream.articles[0]['html']['repository_path']
        self.upstream.contents[source_path] = self.upstream.contents[source_path].replace('Faith and', 'Trust and')
        self.upstream.revision = 'b' * 40
        self.upstream.rebuild()
        self.engine.discover()
        self.assertEqual(enqueue_source_refreshes(self.engine), [])
        drive(self.engine, self.provider)
        self.assertEqual(self.provider.create_calls, before_calls)
        self.assertEqual(self.path().read_bytes(), before)
        item = self.state.projection(self.config)['articles'][0]
        self.assertEqual(item['status'], 'stale')
        self.assertIs(item['human_reviewed'], True)
        self.assertIs(item['ai_notice_required'], False)
        self.assertFalse(any(campaign.get('source_refresh') for campaign in self.state.campaigns()))


if __name__ == '__main__':
    unittest.main()
