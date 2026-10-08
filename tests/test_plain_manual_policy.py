"""Offline translation checks and resumption of frozen manual authority."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation import manual_admission, plain_policy
from berean_translation.common import ContractError, json_hash, read_json
from berean_translation.config import Config
from berean_translation.engine import Engine
from berean_translation.html import validate_translation
from berean_translation.requests import build_request
from berean_translation.review_contract import review_threshold
from berean_translation.state import State
from support import A, REPO_ROOT, MemoryGit, drive, queue, setup


class PlainHTMLTests(unittest.TestCase):
    def setUp(self):
        self.source = {'html':f'<article data-article-id="{A}"><p>Read John 3:16–18.</p>'
                       f'<figure><img src="/images/articles/{A}-1.jpg" alt="A tree">'
                       '<figcaption>A tree.</figcaption></figure>'
                       '<p><a href="https://example.test/source">The source.</a></p></article>',
                       'article':{'id':A, 'title':'Faith', 'subtitle':None, 'section':'Teaching'}}
        self.candidate = {'html':self.source['html'], 'title':'Geloof',
                          'subtitle':None, 'section':'Onderwijs'}

    def test_localized_citation_grammar_is_left_to_independent_review(self):
        for language, text in [('deu', 'Lies Johannes 3,16–18.'),
                               ('heb', 'קרא יוחנן ג׳:ט״ז–י״ח.'),
                               ('fra', 'Lire Jean 3, versets 16 à 18.')]:
            candidate = {**self.candidate, 'html':self.candidate['html'].replace('Read John 3:16–18.', text)}
            with self.subTest(language=language):
                validate_translation(self.source, candidate, language=language)

    def test_identity_html_urls_and_completeness_remain_required(self):
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
                validate_translation(self.source, {**self.candidate, 'html':html})
        for changes in ({'title':''}, {'subtitle':'Invented'}, {'section':None}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_translation(self.source, {**self.candidate, **changes})

    def test_complete_saved_multilingual_results_pass_without_offsets(self):
        # These are durable paid results, not synthetic translation fixtures.
        identities = [('heb', '25208c78b2f979028095f9fb719c88dd'),
                      ('kor', 'c7527dd4a0a44a563e33017e57b5df42'),
                      ('nob', '09c40d7b604a067de6878ec559753976')]
        for language, identity in identities:
            task = read_json(REPO_ROOT/f'state/tasks/{identity}/task.json')
            source = read_json(REPO_ROOT/task['source_snapshot'])
            result_path = REPO_ROOT/f'state/tasks/{identity}/results/correct.json'
            result = read_json(result_path)
            frozen_result_hash = json_hash(result)
            candidate = {key:result['result'][key] for key in ('html', 'title', 'subtitle', 'section')}
            with self.subTest(language=language):
                validate_translation(source, candidate, language=language)
            self.assertEqual(json_hash(read_json(result_path)), frozen_result_hash)


class PlainManualAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.engine.discover()

    def hold(self, reason='ambiguous_quote_reference', *, operation='translate'):
        create_calls = self.provider.create_calls
        request = queue(self.state, 'held-manual', operation=operation, languages='afr', budget_usd=5)
        # Construct an old funded admission without running the removed runtime.
        with patch.object(self.engine.state, 'save_task'), patch.object(self.engine.state, 'save_record'):
            campaign = self.engine.accept_request(request)
        campaign.update(tasks=[], prompt_version='legacy-manual-v1',
                        prompts={'translation':'Frozen historical translation', 'review':'Frozen historical review'})
        self.state.save_campaign(campaign)
        ledger = manual_admission.initialize(self.engine, campaign, request)
        for entry in ledger['entries'].values():
            entry['events'][0]['reason'] = entry['reason'] = 'awaiting_prefetch'
            manual_admission.transition(entry, 'attention', reason)
            detail = {'text':f'Scripture attention: {reason}: Frozen admission diagnostic',
                      'provenance_sha256':entry['provenance_sha256'],
                      'event_index':len(entry['events'])-1, 'event_sha256':json_hash(entry['events'][-1])}
            entry['attention_detail'] = {**detail, 'sha256':json_hash(detail)}
        manual_admission.save(self.state, ledger)
        self.assertEqual(self.provider.create_calls, create_calls)
        return request, campaign, ledger

    def test_resumption_preserves_original_authority_proof_and_diagnostics(self):
        request, campaign, before = self.hold()
        frozen_contract = copy.deepcopy(manual_admission.contract(campaign))
        self.assertTrue(manual_admission.needs_resume(self.state, campaign))
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
            self.assertTrue(task['plain_policy_resumed'])
            self.assertEqual(task['translation_attempts'], 0)
            view = plain_policy.effective_campaign(self.state, task, after, self.config)
            self.assertEqual(view['quality_threshold'], 95)
            self.assertEqual(view['upgrade_quality_threshold'], 98)
            self.assertEqual(view['prompts']['translation'], self.config.prompt('translation'))
            for key in ('budget_usd', 'models', 'model', 'review_model', 'max_output_tokens', 'review_output_tokens'):
                self.assertEqual(view[key], after[key])
        self.assertEqual(self.state.read('state/queue/held-manual.json'), request)
        replay = copy.deepcopy(ledger)
        manual_admission.resume(self.engine, after, request)
        self.assertEqual(self.state.read(manual_admission.path(campaign['id'])), replay)

    def test_source_human_and_unrelated_holds_are_not_reopened(self):
        request, campaign, before = self.hold()
        with patch.object(self.engine, 'human_protected', return_value=True):
            manual_admission.resume(self.engine, campaign, request)
        after = self.state.read(manual_admission.path(campaign['id']))
        for identity, entry in after['entries'].items():
            self.assertEqual(entry['reason'], 'human_reviewed_or_edited_protected')
            self.assertEqual(entry['events'][:-1], before['entries'][identity]['events'])
        self.assertEqual(self.state.tasks(), [])
        manual_admission.save(self.state, before)
        source = self.state.read('state/source.json')
        source['articles'][A]['translation_key'] = 'f' * 64
        self.state.write('state/source.json', source)
        manual_admission.resume(self.engine, campaign, request)
        entry = next(e for e in self.state.read(manual_admission.path(campaign['id']))['entries'].values()
                     if e['item']['article_id'] == A)
        original = next(e for e in before['entries'].values() if e['item']['article_id'] == A)
        self.assertEqual(entry['reason'], 'source_changed_since_manual_selection')
        self.assertEqual(entry['events'][:-1], original['events'])
        self.assertFalse(manual_admission._can_resume({'status':'attention', 'provenance':{},
                                                     'reason':'original_review_candidate_changed'}))

    def test_existing_attention_reports_current_lineage_guard_without_admitting(self):
        request, campaign, before = self.hold()
        original_contract = copy.deepcopy(manual_admission.contract(campaign))
        for entry in before['entries'].values():
            record = self.state.record(entry['item']['language'], entry['item']['article_id'])
            record['history'].append({'event':'fixture_lineage_change'})
            self.state.save_record(record)
        manual_admission.resume(self.engine, campaign, request)
        after = self.state.read(manual_admission.path(campaign['id']))
        manual_admission.validate(self.state, campaign, after)
        for identity, entry in after['entries'].items():
            original = before['entries'][identity]
            self.assertEqual(entry['status'], 'attention')
            self.assertEqual(entry['reason'], 'publication_or_latest_task_changed_since_manual_selection')
            self.assertEqual(entry['events'][:-1], original['events'])
            self.assertEqual(entry['provenance'], original['provenance'])
            self.assertEqual(entry['attention_detail'], original['attention_detail'])
        self.assertEqual(self.state.tasks(), [])
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(manual_admission.contract(campaign), original_contract)
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(campaign['reported_usage_usd'], 0)
        manual_admission.resume(self.engine, campaign, request)
        self.assertEqual(self.state.read(manual_admission.path(campaign['id'])), after)

    def test_existing_or_reserved_task_bytes_are_preserved(self):
        request, campaign, ledger = self.hold()
        first, second = list(ledger['entries'].values())
        task = {**first['provenance']['task'], 'translation_attempts':1}
        self.state.save_task(task)
        self.state.save_batch({'id':'reserved', 'campaign':campaign['id'],
                               'custom_ids':[second['task_id']+':translate']})
        before_task = copy.deepcopy(task)
        manual_admission.resume(self.engine, campaign, request)
        self.assertEqual(self.state.read(manual_admission.path(campaign['id'])), ledger)
        self.assertEqual(self.state.read(f'state/tasks/{task["id"]}/task.json'), before_task)

    def test_resumption_keeps_ready_history_and_rejects_tampering(self):
        request, campaign, ledger = self.hold()
        identity, entry = next(iter(ledger['entries'].items()))
        entry['ready_fields'] = {'historical_contract': 'a'*64}
        entry['ready_sha256'] = json_hash(entry['ready_fields'])
        manual_admission.save(self.state, ledger)
        old_fields = copy.deepcopy(entry['ready_fields'])
        manual_admission.resume(self.engine, campaign, request)
        ledger = self.state.read(manual_admission.path(campaign['id']))
        self.assertEqual(ledger['entries'][identity]['ready_fields'], old_fields)
        for change in ('event', 'marker', 'historical-fields'):
            corrupt = copy.deepcopy(ledger)
            if change == 'event':
                corrupt['entries'][identity]['events'][0]['reason'] = 'changed'
            elif change == 'marker':
                corrupt['entries'][identity]['resume_fields']['plain_policy_resumed'] = False
            else:
                corrupt['entries'][identity]['ready_fields']['historical_contract'] = 'b'*64
            with self.subTest(change=change), self.assertRaises(ContractError):
                manual_admission.validate(self.state, campaign, corrupt)

    def test_crash_after_materialization_replays_exact_task_once(self):
        request, campaign, _ = self.hold()
        original_save = self.engine.state.save_task
        def crash(task):
            original_save(task)
            raise RuntimeError('Simulated interruption after durable task write')
        with patch.object(self.engine.state, 'save_task', side_effect=crash), self.assertRaises(RuntimeError):
            manual_admission.resume(self.engine, campaign, request)
        manual_admission.resume(self.engine, campaign, request)
        ledger = self.state.read(manual_admission.path(campaign['id']))
        self.assertEqual({e['status'] for e in ledger['entries'].values()}, {'admitted'})
        for record in self.state.records():
            requested = [e for e in record['history'] if e['event'] == 'requested']
            self.assertEqual(len(requested), 1)
        self.assertEqual(self.provider.create_calls, 0)

    def test_resumed_manual_task_runs_generation_and_independent_review(self):
        request, campaign, original = self.hold()
        manual_admission.resume(self.engine, campaign, request)
        drive(self.engine, self.provider)
        self.assertEqual({task['status'] for task in self.state.tasks()}, {'complete'})
        for task in self.state.tasks():
            self.assertEqual(task['translation_attempts'], 1)
            self.assertEqual(task['review_attempts'], 1)
            self.assertTrue(task['plain_policy_resumed'])
        after = self.state.read('state/campaigns/held-manual.json')
        self.assertEqual(manual_admission.contract(after), manual_admission.contract(original['initial_campaign']))
        self.assertLessEqual(after['reserved_usd'], after['budget_usd'])

    def test_resumed_review_freezes_accepted_baseline_and_requires_98(self):
        queue(self.state, 'accepted-original', languages='afr')
        drive(self.engine, self.provider)
        originals = {record['article_id']:copy.deepcopy(record['published']) for record in self.state.records()}
        request, campaign, _ = self.hold(operation='review')
        original_contract = copy.deepcopy(manual_admission.contract(campaign))
        manual_admission.resume(self.engine, campaign, request)
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            view = plain_policy.effective_campaign(self.state, task, campaign, self.config)
            self.assertEqual(review_threshold(view, task), 98)
            self.assertEqual(task['accepted_baseline'], self.state.publication_candidate(originals[task['article_id']])[0])
            self.assertEqual(task['baseline_quality_score'], 97)
            line, _, _ = build_request(self.config, self.state, task)
            self.assertIn('accepted_baseline', line['body']['messages'][1]['content'])
        def below_upgrade_threshold(line):
            result = self.provider.default_result(line)
            if ':review' in line['custom_id']:
                result['score'] = 97
            return result
        drive(self.engine, self.provider, below_upgrade_threshold)
        for record in self.state.records():
            self.assertEqual(record['published'], originals[record['article_id']])
        final = self.state.read('state/campaigns/held-manual.json')
        self.assertEqual(manual_admission.contract(final), original_contract)
        self.assertEqual({self.state.read(f'state/tasks/{identity}/task.json')['status']
                          for identity in final['tasks']}, {'not_ready'})


class SavedManualLedgerReplayTests(unittest.TestCase):
    def test_all_157_actual_never_paid_entries_resume_without_state_writes(self):
        original = State(REPO_ROOT)
        base_tasks, base_batches = original.tasks(), original.batches()
        class Overlay(State):
            def __init__(self):
                super().__init__(REPO_ROOT)
                self.overlay = {}
            def read(self, relative, default=None):
                return copy.deepcopy(self.overlay[relative]) if relative in self.overlay else original.read(relative, default)
            def write(self, relative, value):
                self.overlay[relative] = copy.deepcopy(value)
            def tasks(self):
                return base_tasks + [value for key, value in self.overlay.items()
                                     if key.startswith('state/tasks/') and key.endswith('/task.json')]
            def batches(self):
                return base_batches
        engine = Engine(Config(REPO_ROOT), None, None, MemoryGit())
        admitted = 0
        for identity in ('gh-37439892859', 'gh-37754206621'):
            # Overlapping original selections share one active pair in production.
            # Separate overlays verify each campaign's original157admissions.
            state = Overlay()
            engine.state = state
            campaign = state.read(f'state/campaigns/{identity}.json')
            before = state.read(manual_admission.path(identity))
            contract = copy.deepcopy(manual_admission.contract(campaign))
            request = state.read(f'state/queue/{identity}.json')
            manual_admission.resume(engine, campaign, request)
            after = state.read(manual_admission.path(identity))
            manual_admission.validate(state, campaign, after)
            self.assertEqual(manual_admission.contract(campaign), contract)
            self.assertEqual({entry['status'] for entry in after['entries'].values()}, {'admitted'},
                             {key:entry['reason'] for key,entry in after['entries'].items() if entry['status'] != 'admitted'})
            for task_id, entry in after['entries'].items():
                old = before['entries'][task_id]
                self.assertEqual(entry['provenance'], old['provenance'])
                self.assertEqual(entry['events'][:len(old['events'])], old['events'])
                self.assertEqual(state.read(f'state/tasks/{task_id}/task.json')['translation_attempts'], 0)
            admitted += len(after['entries'])
        self.assertEqual(admitted, 157)
        # The overlay cannot persist writes or reach a provider.
        self.assertTrue(state.overlay)


if __name__ == '__main__':
    unittest.main()
