"""Offline ordinary translation and original-envelope manual admission recovery."""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation import manual_admission, plain_policy
from berean_translation.common import ContractError, json_hash, read_json
from berean_translation.html import validate_translation
from berean_translation.requests import build_request
from berean_translation.review_contract import review_threshold
from berean_translation.scripture_evidence import ScriptureAttention
from support import A, REPO_ROOT, drive, queue, setup


class PlainHTMLTests(unittest.TestCase):
    def setUp(self):
        self.source = {'html':f'<article data-article-id="{A}"><p>Read John 3:16–18.</p>'
                       f'<figure><img src="/images/articles/{A}-1.jpg" alt="A tree">'
                       '<figcaption>A tree.</figcaption></figure>'
                       '<p><a href="https://example.test/source">The source.</a></p></article>',
                       'article':{'id':A, 'title':'Faith', 'subtitle':None, 'section':'Teaching'}}
        self.candidate = {'html':self.source['html'], 'title':'Geloof',
                          'subtitle':None, 'section':'Onderwijs'}

    def test_localized_citation_grammar_is_left_to_semantic_review(self):
        for language, text in [('deu', 'Lies Johannes 3,16–18.'),
                               ('heb', 'קרא יוחנן ג׳:ט״ז–י״ח.'),
                               ('fra', 'Lire Jean 3, versets 16 à 18.')]:
            candidate = {**self.candidate, 'html':self.candidate['html'].replace('Read John 3:16–18.', text)}
            with self.subTest(language=language):
                validate_translation(self.source, candidate, language=language, scripture_validation=False)
        changed = {**self.candidate, 'html':self.candidate['html'].replace('3:16–18', '3:19')}
        with self.assertRaisesRegex(ContractError, 'Scripture'):
            validate_translation(self.source, changed)
        # The plain runtime sends reference fidelity to its independent reviewer.
        validate_translation(self.source, changed, scripture_validation=False)

    def test_plain_validation_retains_identity_html_urls_and_completeness(self):
        original = self.candidate['html']
        mutations = [original.replace(A, '22222222-2222-4222-8222-222222222222', 1),
                     original.replace('<p>Read', '<p onclick="bad()">Read'),
                     original.replace('https://example.test/source', 'javascript:bad()'),
                     original.replace('https://example.test/source', 'https://example.test/other'),
                     original.replace('Read John 3:16–18.', ''),
                     original.replace('alt="A tree"', 'alt=""'),
                     original.replace('<p>Read', '<script>bad()</script><p>Read'),
                     original.replace('</article>', '<p>Added paragraph.</p></article>')]
        for html in mutations:
            with self.subTest(html=html), self.assertRaises(ContractError):
                validate_translation(self.source, {**self.candidate, 'html':html}, scripture_validation=False)
        for changes in ({'title':''}, {'subtitle':'Invented'}, {'section':None}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_translation(self.source, {**self.candidate, **changes}, scripture_validation=False)

    def test_saved_complete_multilingual_candidates_need_no_selection_offsets(self):
        manifest = read_json(REPO_ROOT/'tests/fixtures/scripture-selection-repair.json')
        for case in manifest['cases']:
            if not case['recoverable_metadata']:
                continue
            source = read_json(REPO_ROOT/case['source_path'])
            result = read_json(REPO_ROOT/case['result_path'])
            self.assertEqual(json_hash(source), case['source_sha256'])
            self.assertEqual(json_hash(result), case['result_sha256'])
            candidate = {key:result['result'][key] for key in ('html', 'title', 'subtitle', 'section')}
            with self.subTest(language=case['language']):
                validate_translation(source, candidate, language=case['language'], scripture_validation=False)
            # Evidence, raw provider responses and terminal task history stay unchanged.
            self.assertEqual(json_hash(read_json(REPO_ROOT/case['source_path'])), case['source_sha256'])
            self.assertEqual(json_hash(read_json(REPO_ROOT/case['result_path'])), case['result_sha256'])


class PlainManualAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.config.runtime.pop(plain_policy.FIELD, None)
        self.config.runtime['scripture_quotes_enabled'] = True
        shutil.copytree(REPO_ROOT/'data', self.root/'data')
        shutil.copytree(REPO_ROOT/'docs/third-party', self.root/'docs/third-party')
        self.engine.discover()

    def hold(self, reason='ambiguous_quote_reference'):
        request = queue(self.state, 'held-manual', languages='afr', budget_usd=5)
        with patch('berean_translation.manual_admission.freeze_scripture_evidence',
                   side_effect=ScriptureAttention(reason, 'Authentic structural fixture hold')):
            self.engine.accept_request(request)
        campaign = self.state.read('state/campaigns/held-manual.json')
        ledger = self.state.read(manual_admission.path('held-manual'))
        self.assertEqual({e['status'] for e in ledger['entries'].values()}, {'attention'})
        self.assertEqual(self.provider.create_calls, 0)
        return request, campaign, ledger

    def enable_plain(self):
        self.config.runtime[plain_policy.FIELD] = 1

    def test_marker_requires_exact_supported_integer(self):
        self.assertFalse(plain_policy.is_plain({}))
        self.assertTrue(plain_policy.is_plain({plain_policy.FIELD:1}))
        self.assertEqual(plain_policy.frozen_fields(self.config), {})
        for value in (True, False, None, '1', 0, 2):
            with self.subTest(value=value), self.assertRaises(ContractError):
                plain_policy.is_plain({plain_policy.FIELD:value})

    def test_scripture_attention_migrates_without_fetching_or_changing_authority(self):
        request, campaign, before = self.hold()
        frozen_contract = copy.deepcopy(manual_admission.contract(campaign))
        self.enable_plain()
        self.assertTrue(manual_admission.needs_resume(self.state, campaign, config=self.config))
        with patch('berean_translation.manual_admission.freeze_scripture_evidence',
                   side_effect=AssertionError('Plain translation must never fetch Scripture')):
            after = manual_admission.resume(self.engine, campaign, request)
        self.assertEqual(manual_admission.contract(after), frozen_contract)
        self.assertEqual(after['budget_usd'], 5)
        self.assertEqual(after['reserved_usd'], 0)
        self.assertEqual(after['reported_usage_usd'], 0)
        ledger = self.state.read(manual_admission.path(campaign['id']))
        manual_admission.validate(self.state, after, ledger)
        self.assertEqual({e['status'] for e in ledger['entries'].values()}, {'admitted'})
        for identity, original in before['entries'].items():
            entry = ledger['entries'][identity]
            self.assertEqual(entry['provenance'], original['provenance'])
            self.assertEqual(entry['attention_detail'], original['attention_detail'])
            self.assertEqual(entry['events'][:len(original['events'])], original['events'])
            task = self.state.read(f'state/tasks/{identity}/task.json')
            self.assertTrue(plain_policy.is_plain(task))
            self.assertEqual(task['translation_attempts'], 0)
            view = plain_policy.effective_campaign(self.state, task, after)
            self.assertTrue(plain_policy.is_plain(view))
            self.assertNotIn('scripture_quotes', view)
            for key in ('budget_usd', 'models', 'model', 'review_model', 'max_output_tokens', 'review_output_tokens'):
                self.assertEqual(view[key], after[key])
        self.assertEqual(self.state.read('state/queue/held-manual.json'), request)
        self.assertEqual(self.provider.create_calls, 0)
        replay = copy.deepcopy(ledger)
        manual_admission.resume(self.engine, after, request)
        self.assertEqual(self.state.read(manual_admission.path(campaign['id'])), replay)

    def test_non_scripture_source_and_human_guards_remain_held(self):
        request, campaign, before = self.hold()
        self.enable_plain()
        with patch.object(self.engine, 'human_protected', return_value=True):
            manual_admission.resume(self.engine, campaign, request)
        self.assertEqual(self.state.read(manual_admission.path(campaign['id'])), before)
        self.assertEqual(self.state.tasks(), [])
        source = self.state.read('state/source.json')
        source['articles'][A]['translation_key'] = 'f' * 64
        self.state.write('state/source.json', source)
        manual_admission.resume(self.engine, campaign, request)
        entry = next(e for e in self.state.read(manual_admission.path(campaign['id']))['entries'].values()
                     if e['item']['article_id'] == A)
        self.assertEqual(entry, next(e for e in before['entries'].values() if e['item']['article_id'] == A))

    def test_migration_cannot_replace_an_existing_or_reserved_task(self):
        _, campaign, ledger = self.hold()
        self.enable_plain()
        first, second = list(ledger['entries'].values())
        task = {**first['provenance']['task'], 'translation_attempts':1}
        self.state.save_task(task)
        batch = {'id':'reserved', 'campaign':campaign['id'], 'custom_ids':[second['task_id']+':translate']}
        self.state.save_batch(batch)
        before_task = copy.deepcopy(self.state.read(f'state/tasks/{task["id"]}/task.json'))
        manual_admission.migrate_manual_admissions(self.engine, campaign, ledger)
        self.assertEqual(self.state.read(manual_admission.path(campaign['id'])), ledger)
        self.assertFalse(any('plain_migration' in e for e in ledger['entries'].values()))
        self.assertEqual(self.state.read(f'state/tasks/{task["id"]}/task.json'), before_task)

    def test_modified_frozen_migration_prompts_or_history_are_rejected(self):
        request, campaign, _ = self.hold()
        self.enable_plain()
        manual_admission.resume(self.engine, campaign, request)
        ledger = self.state.read(manual_admission.path(campaign['id']))
        identity = next(iter(ledger['entries']))
        for change in ('prompt', 'event', 'task-marker'):
            corrupt = copy.deepcopy(ledger)
            if change == 'prompt':
                corrupt['entries'][identity]['plain_migration']['policy']['prompts']['translation'] += ' changed'
            elif change == 'event':
                corrupt['entries'][identity]['events'][0]['reason'] = 'changed'
            else:
                corrupt['entries'][identity]['ready_fields'][plain_policy.FIELD] = 2
            with self.subTest(change=change), self.assertRaises(ContractError):
                manual_admission.validate(self.state, campaign, corrupt)

    def test_migrated_manual_task_runs_generation_and_independent_review(self):
        request, campaign, original = self.hold()
        self.enable_plain()
        self.config.runtime['scripture_quotes_enabled'] = False
        manual_admission.resume(self.engine, campaign, request)
        with patch('berean_translation.manual_admission.freeze_scripture_evidence',
                   side_effect=AssertionError('Migrated plain stages must not query Scripture')):
            drive(self.engine, self.provider)
        tasks = self.state.tasks()
        self.assertEqual({task['status'] for task in tasks}, {'complete'})
        for task in tasks:
            self.assertEqual(task['translation_attempts'], 1)
            self.assertEqual(task['review_attempts'], 1)
            self.assertTrue(plain_policy.is_plain(task))
            publication = self.state.record(task['language'], task['article_id'])['published']
            self.assertTrue(plain_policy.is_plain(publication))
        after = self.state.read('state/campaigns/held-manual.json')
        self.assertEqual(manual_admission.contract(after), manual_admission.contract(original['initial_campaign']))
        self.assertLessEqual(after['reserved_usd'], after['budget_usd'])

    def test_migrated_review_preserves_frozen_publication_and_requires_98(self):
        self.enable_plain()
        self.config.runtime['scripture_quotes_enabled'] = False
        queue(self.state, 'accepted-original', languages='afr')
        drive(self.engine, self.provider)
        originals = {record['article_id']:copy.deepcopy(record['published']) for record in self.state.records()}
        self.config.runtime.pop(plain_policy.FIELD)
        self.config.runtime['scripture_quotes_enabled'] = True
        request = queue(self.state, 'legacy-review', operation='review', languages='afr')
        with patch('berean_translation.manual_admission.freeze_scripture_evidence',
                   side_effect=ScriptureAttention('ambiguous_quote_reference', 'Fixture saved review hold')):
            campaign = self.engine.accept_request(request)
        original_contract = copy.deepcopy(manual_admission.contract(campaign))
        self.enable_plain()
        self.config.runtime['scripture_quotes_enabled'] = False
        manual_admission.resume(self.engine, campaign, request)
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            view = plain_policy.effective_campaign(self.state, task, campaign)
            self.assertEqual(review_threshold(view, task), 98)
            self.assertEqual(task['accepted_baseline'], self.state.publication_candidate(originals[task['article_id']])[0])
            self.assertEqual(task['baseline_quality_score'], 97)
            line, _, _ = build_request(self.config, self.state, task)
            self.assertIn('accepted_baseline', line['body']['messages'][1]['content'])
        # A 97-point review is insufficient to replace the existing 97-point publication.
        drive(self.engine, self.provider)
        for record in self.state.records():
            self.assertEqual(record['published'], originals[record['article_id']])
        final = self.state.read('state/campaigns/legacy-review.json')
        self.assertEqual(manual_admission.contract(final), original_contract)
        self.assertEqual({self.state.read(f'state/tasks/{identity}/task.json')['status']
                          for identity in final['tasks']}, {'not_ready'})


if __name__ == '__main__':
    unittest.main()
