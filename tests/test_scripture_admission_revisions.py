"""Offline revision journals for actual, never-paid manual Scripture holds.

Every repository and journal is a disposable fixture. No repository runtime
state, real GetBible endpoint, or billable provider is used by these tests.
"""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from berean_translation import manual_admission
from berean_translation.common import ContractError, canonical, json_hash
from berean_translation.requests import reserve_cost
from berean_translation.scripture_admission_revisions import (
    OfflineAdmissionRevisionStore, complete_cycle_ceiling, inspect_never_paid_hold)
from berean_translation.scripture_association import associate
from berean_translation.scripture_component_evidence import component_policy
from berean_translation.scripture_component_processing import (
    OfflineComponentCycle, processing_contract)
from berean_translation.scripture_evidence import build_evidence
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.state import State
from support import A, B, REPO_ROOT, queue, setup
from test_scripture_component_evidence import insertion
from test_scripture_evidence import FakeMCP, fixture


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def held_fixture(parent, *, articles=1, budget=5, model='gpt-4.1-mini',
                 review_model='gpt-4.1-mini'):
    """Produce real attention provenance through accept_request, not a forgery."""
    root = parent / 'repository'
    root.mkdir()
    config, state, upstream, paid, git, engine = setup(root, review_contract_version=2)
    shutil.copytree(REPO_ROOT / 'data', root / 'data')
    shutil.copytree(REPO_ROOT / 'docs/third-party', root / 'docs/third-party')
    config.runtime['scripture_quotes_enabled'] = True
    english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
    printed, operations = insertion(english, '[indeed]')
    upstream.articles = upstream.articles[:articles]
    for item in upstream.articles:
        upstream.contents[item['html']['repository_path']] = (
            f'<article data-article-id="{item["id"]}"><p>“{printed}” (John 4:16).</p>'
            f'<figure><img src="/images/articles/{item["id"]}-1.jpg" alt="A tree">'
            '<figcaption>A tree.</figcaption></figure></article>')
    upstream.rebuild()
    engine.discover()
    fake = FakeMCP()
    engine.scripture_provider = GetBibleMCP(fake)
    campaign = engine.accept_request(queue(state, languages='deu', budget_usd=budget,
                                          model=model, review_model=review_model))
    ledger = state.read(manual_admission.path(campaign['id']))
    manual_admission.validate(state, campaign, ledger)
    by_article = {}
    for entry_id, entry in ledger['entries'].items():
        source = state.source(entry['provenance']['task'])
        scopes = associate(source['html'], source['article']['id'])[2]
        policy = component_policy(root, source, [
            {'scope_sha256': json_hash(scopes[0]), 'operations': operations}])
        evidence = build_evidence(source, 'de', policy, GetBibleMCP(fake))
        by_article[entry['item']['article_id']] = SimpleNamespace(
            entry_id=entry_id, entry=entry, source=source, policy=policy, evidence=evidence)
    journal = parent / 'private-revisions'
    store = OfflineAdmissionRevisionStore(journal, root)
    return SimpleNamespace(root=root, config=config, state=state, upstream=upstream,
        paid=paid, git=git, engine=engine, campaign=campaign, ledger=ledger,
        by_article=by_article, fake=fake, journal=journal, store=store)


class OfflineAdmissionRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.fixture = held_fixture(self.parent)
        self.use_fixture(self.fixture)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            blocker = patch(target, side_effect=AssertionError('Network forbidden in offline test'))
            blocker.start()
            self.addCleanup(blocker.stop)
        remote = patch.object(GetBibleMCP, '_remote',
            side_effect=AssertionError('GetBible remote forbidden in offline test'))
        remote.start()
        self.addCleanup(remote.stop)

    def use_fixture(self, fixture):
        self.fixture = fixture
        for key in ('root', 'config', 'state', 'upstream', 'paid', 'engine', 'campaign',
                    'ledger', 'by_article', 'fake', 'journal', 'store'):
            setattr(self, key, getattr(fixture, key))
        self.item = self.by_article[A]

    def replacement_fixture(self, **kwargs):
        child = self.parent / ('fixture-' + str(len(list(self.parent.iterdir()))))
        child.mkdir()
        self.use_fixture(held_fixture(child, **kwargs))

    def propose(self, item=None, **kwargs):
        item = self.item if item is None else item
        return self.store.propose(self.engine, self.campaign['id'], item.entry_id,
            kwargs.pop('scripture_policy', item.policy), kwargs.pop('evidence', item.evidence),
            self.root, **kwargs)

    def prepare(self, proposal, **kwargs):
        return self.store.prepare(self.engine, proposal['revision_id'], **kwargs)

    def frozen_processing(self, item=None):
        item = self.item if item is None else item
        campaign = copy.deepcopy(self.campaign)
        campaign['scripture_quotes'] = copy.deepcopy(item.policy)
        task = copy.deepcopy(item.entry['provenance']['task'])
        task['models'] = copy.deepcopy(campaign['models'])
        return processing_contract(self.root, campaign, task)

    def assert_original_unchanged(self, before):
        self.assertEqual(files(self.root), before)
        self.assertEqual((self.paid.upload_calls, self.paid.create_calls), (0, 0))

    def test_fixture_is_an_actual_annotation_attention_hold_with_no_task(self):
        self.assertEqual(self.campaign['status'], 'admission_attention')
        self.assertEqual(self.campaign['tasks'], [])
        self.assertEqual(self.campaign['reserved_usd'], 0)
        self.assertEqual(self.item.entry['status'], 'attention')
        self.assertEqual(self.item.entry['reason'], 'source_quote_annotation')
        self.assertFalse((self.root / 'state/tasks' / self.item.entry_id).exists())
        before = files(self.root)
        result = inspect_never_paid_hold(self.engine, self.campaign['id'], self.item.entry_id)
        self.assertIsInstance(result, dict)
        self.assert_original_unchanged(before)

    def test_propose_prepare_and_request_leave_every_original_byte_unchanged(self):
        before = files(self.root)
        provider_reads = len(self.fake.calls)
        with patch.object(State, 'write', side_effect=AssertionError('Live State write forbidden')):
            proposal = self.propose()
            cycle = self.prepare(proposal)
            self.assertIsInstance(cycle, OfflineComponentCycle)
            request = cycle.request()
            self.assertEqual(request['stage'], 'translate')
            snapshot = cycle.snapshot()
            self.assertTrue(snapshot['offline_only'])
            self.assertFalse(snapshot['funding_authorized'])
            self.assertFalse(snapshot['publication_authorized'])
        self.assertEqual(len(self.fake.calls), provider_reads)
        self.assert_original_unchanged(before)
        self.assertEqual(self.campaign['scripture_quotes']['version'], '1')
        self.assertTrue(files(self.journal))

    def test_current_configuration_cannot_replace_original_funding_or_models(self):
        original = copy.deepcopy(self.campaign)
        self.config.models['gpt-4.1-mini']['input_batch_usd_per_million'] = 999999
        self.config.runtime.update(default_model='gpt-6-luna', max_output_tokens=17)
        self.config.languages['deu']['tag'] = 'af'
        (self.root / 'prompts/translation.txt').write_text('Changed current default prompt')
        before = files(self.root)
        proposal = self.propose()
        cycle = self.prepare(proposal)
        request = cycle.request()
        self.assertEqual(request['line']['body']['model'], original['models'][original['model']]['api_model'])
        self.assertEqual(self.state.read(f'state/campaigns/{original["id"]}.json'), original)
        self.assert_original_unchanged(before)

    def test_changed_old_queue_campaign_or_provenance_fail_closed(self):
        for mutation in ('queue', 'campaign', 'provenance', 'missing_provenance', 'status'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                if mutation == 'queue':
                    request = self.state.read('state/queue/request-1.json')
                    request['budget_usd'] = 100
                    self.state.write('state/queue/request-1.json', request)
                elif mutation == 'campaign':
                    campaign = copy.deepcopy(self.campaign)
                    campaign['model'] = 'gpt-6-luna'
                    self.state.save_campaign(campaign)
                else:
                    ledger = copy.deepcopy(self.ledger)
                    entry = ledger['entries'][self.item.entry_id]
                    if mutation == 'provenance': entry['provenance']['task']['translation_attempts'] = 1
                    elif mutation == 'missing_provenance': entry['provenance'] = None
                    else: entry['status'] = 'pending'
                    self.state.write(manual_admission.path(self.campaign['id']), ledger)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)

    def test_changed_source_snapshot_or_current_fingerprint_fail_closed(self):
        for mutation in ('snapshot', 'current', 'removed'):
            for prepared in (False, True):
                with self.subTest(mutation=mutation, prepared=prepared):
                    self.replacement_fixture()
                    proposal = self.propose() if prepared else None
                    if mutation == 'snapshot':
                        source = copy.deepcopy(self.item.source)
                        source['html'] += '<p>Drift.</p>'
                        self.state.write(self.item.entry['provenance']['task']['source_snapshot'], source)
                    else:
                        source = self.state.read('state/source.json')
                        if mutation == 'current': source['articles'][A]['translation_key'] = 'f' * 64
                        else: del source['articles'][A]
                        self.state.write('state/source.json', source)
                    before = files(self.root)
                    with self.assertRaises(ContractError):
                        self.prepare(proposal) if prepared else self.propose()
                    self.assert_original_unchanged(before)

    def test_human_files_or_record_publication_drift_block_prepare(self):
        for mutation in ('human_file', 'human_history', 'publication', 'latest_task'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                proposal = self.propose()
                record = self.state.record('deu', A)
                if mutation == 'human_file':
                    path = self.root / f'content/deu/articles/{A}.html'
                    path.parent.mkdir(parents=True)
                    path.write_text('Human work must never be overwritten.')
                elif mutation == 'human_history':
                    record['history'].append({'event': 'human_review', 'at': '2026-10-01T00:00:00Z'})
                    self.state.save_record(record)
                elif mutation == 'publication':
                    record['published'] = {'human_reviewed': True}
                    self.state.save_record(record)
                else:
                    record['latest_task'] = 'newer-task'
                    self.state.save_record(record)
                before = files(self.root)
                with self.assertRaises(ContractError): self.prepare(proposal)
                self.assert_original_unchanged(before)

    def test_paid_terminal_overlapping_and_orphan_work_is_not_a_fresh_cycle(self):
        for mutation in ('orphan_candidate', 'original_task', 'paid_task', 'terminal_pair', 'batch'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                task = copy.deepcopy(self.item.entry['provenance']['task'])
                if mutation == 'orphan_candidate':
                    self.state.write(f'state/tasks/{task["id"]}/candidate.json', {'html': 'orphan'})
                elif mutation == 'batch':
                    self.state.save_batch({'id': 'orphan-batch', 'campaign': self.campaign['id'],
                        'tasks': [task['id']], 'status': 'completed'})
                else:
                    if mutation == 'paid_task': task.update(translation_attempts=1, batch='paid-batch')
                    elif mutation == 'terminal_pair': task.update(id='older-terminal', status='not_ready')
                    self.state.save_task(task)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)

    def test_old_policy_or_changed_evidence_cannot_be_rebranded_a_component_revision(self):
        before = files(self.root)
        with self.assertRaises(ContractError):
            self.propose(scripture_policy=self.campaign['scripture_quotes'])
        bad = copy.deepcopy(self.item.evidence)
        bad['lookups'].clear()
        with self.assertRaises(ContractError): self.propose(evidence=bad)
        self.assert_original_unchanged(before)

    def test_complete_cycle_cost_bounds_all_four_stages_at_frozen_rates(self):
        self.replacement_fixture(model='gpt-4.1-nano', review_model='gpt-4.1')
        contract = self.frozen_processing()
        stages = [('generation_model', 'max_output_tokens'), ('review_model', 'review_output_tokens')]
        expected = Decimal('0')
        for model_key, limit_key in stages:
            model = contract['models'][contract[model_key]]
            output = min(contract[limit_key], model['max_output_tokens'])
            maximum_input = model['context_tokens'] - output
            expected += 2 * Decimal(str(reserve_cost(model, maximum_input, output)))
        self.assertEqual(Decimal(complete_cycle_ceiling(contract)['total_usd']), expected)
        self.assertGreater(expected, 0)

    def test_complete_bound_considers_lower_price_after_long_context_threshold(self):
        contract = self.frozen_processing()
        for model in contract['models'].values():
            model.update(context_tokens=6000, max_output_tokens=100,
                input_batch_usd_per_million=100, cached_input_batch_usd_per_million=200,
                cache_write_batch_usd_per_million=150, output_batch_usd_per_million=300,
                long_context_threshold_tokens=4500, long_context_input_multiplier=.01,
                long_context_output_multiplier=.01)
        contract.update(max_output_tokens=100, review_output_tokens=100)
        model = contract['models'][contract['generation_model']]
        exhaustive = max(reserve_cost(model, input_tokens, 100) for input_tokens in range(5901))
        self.assertEqual(Decimal(complete_cycle_ceiling(contract)['total_usd']), 4 * Decimal(str(exhaustive)))


    def variant(self, item=None):
        item = self.item if item is None else item
        policy = copy.deepcopy(item.policy)
        policy['max_component_candidate_bytes'] -= 1
        evidence = build_evidence(item.source, 'de', policy, GetBibleMCP(self.fake))
        return policy, evidence

    def journal_events(self):
        return sorted((self.journal / 'events').glob('*.json'))

    def race_proposals(self, items):
        barrier = threading.Barrier(len(items))
        def attempt(arguments):
            item, policy, evidence = arguments
            store = OfflineAdmissionRevisionStore(self.journal, self.root)
            barrier.wait(timeout=10)
            try:
                return store.propose(self.engine, self.campaign['id'], item.entry_id,
                                     policy, evidence, self.root)
            except ContractError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=len(items)) as executor:
            futures = [executor.submit(attempt, arguments) for arguments in items]
            return [future.result(timeout=20) for future in futures]

    def test_proposal_and_preparation_are_durable_idempotent_and_defensive_copies(self):
        before = files(self.root)
        proposal = self.propose()
        first_event = {p.name: p.read_bytes() for p in self.journal_events()}
        self.assertEqual(self.propose(), proposal)
        proposal['proposal']['origin']['budget_usd'] = 999999
        inspected = self.store.inspect()
        saved = inspected['proposals'][proposal['revision_id']]['proposal']
        self.assertEqual(saved['origin']['budget_usd'], self.campaign['budget_usd'])
        cycle = self.prepare(proposal)
        snapshot = cycle.snapshot()
        self.assertEqual(self.prepare(proposal).snapshot(), snapshot)
        self.assertEqual(len(self.store.inspect()['events']), 2)
        for name, content in first_event.items():
            self.assertEqual((self.journal / 'events' / name).read_bytes(), content)
        reopened = OfflineAdmissionRevisionStore(self.journal, self.root)
        self.assertEqual(reopened.inspect(), self.store.inspect())
        self.assertEqual(reopened.prepare(self.engine, proposal['revision_id']).snapshot(), snapshot)
        self.assert_original_unchanged(before)

    def test_different_revision_cannot_reopen_same_entry_after_cancellation(self):
        before = files(self.root)
        proposal = self.propose()
        allocation = self.store.inspect()['projected_allocations']
        original_events = {p.name: p.read_bytes() for p in self.journal_events()}
        policy, evidence = self.variant()
        with self.assertRaises(ContractError):
            self.propose(scripture_policy=policy, evidence=evidence)
        self.store.cancel(proposal['revision_id'], 'Owner stopped this offline examination.')
        cancelled = self.store.inspect()
        self.assertEqual(cancelled['proposals'][proposal['revision_id']]['status'], 'cancelled')
        self.assertEqual(cancelled['projected_allocations'], allocation)
        with self.assertRaises(ContractError): self.prepare(proposal)
        with self.assertRaises(ContractError):
            self.propose(scripture_policy=policy, evidence=evidence)
        self.assertEqual(self.propose()['status'], 'cancelled')
        self.store.cancel(proposal['revision_id'], 'Already cancelled.')
        self.assertEqual(len(self.store.inspect()['events']), 2)
        for name, content in original_events.items():
            self.assertEqual((self.journal / 'events' / name).read_bytes(), content)
        self.assert_original_unchanged(before)

    def test_original_campaign_cancellation_blocks_proposal_and_preparation(self):
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                self.replacement_fixture()
                proposal = self.propose() if prepared else None
                changed = copy.deepcopy(self.campaign)
                changed['cancel_requested'] = True
                self.state.save_campaign(changed)
                before = files(self.root)
                with self.assertRaises(ContractError):
                    self.prepare(proposal) if prepared else self.propose()
                self.assert_original_unchanged(before)

    def test_insufficient_original_budget_never_commits_a_partial_cycle(self):
        self.replacement_fixture(budget=.1)
        before = files(self.root)
        with self.assertRaises(ContractError): self.propose()
        self.assertEqual(self.store.inspect()['events'], [])
        self.assertEqual(self.store.inspect()['projected_allocations'], {})
        self.assert_original_unchanged(before)

    def test_existing_manual_reservations_and_usage_cannot_be_recycled(self):
        for amount in ('reserved_usd', 'reported_usage_usd'):
            with self.subTest(amount=amount):
                self.replacement_fixture(budget=.9)
                changed = copy.deepcopy(self.campaign)
                changed[amount] = .2
                self.state.save_campaign(changed)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assertEqual(self.store.inspect()['events'], [])
                self.assert_original_unchanged(before)


    def test_durable_batch_reservation_cannot_hide_behind_a_stale_campaign_total(self):
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                self.replacement_fixture(articles=2)
                proposal = self.propose() if prepared else None
                other_id = self.by_article[B].entry_id
                self.state.save_task(copy.deepcopy(self.by_article[B].entry['provenance']['task']))
                self.state.save_batch({'id': 'durably-reserved', 'campaign': self.campaign['id'],
                    'stage': 'translate', 'model': self.campaign['model'], 'tasks': [other_id],
                    'status': 'prepared', 'reserved_usd': self.campaign['budget_usd']})
                self.assertEqual(self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd'], 0)
                before = files(self.root)
                with self.assertRaises(ContractError):
                    self.prepare(proposal) if prepared else self.propose()
                self.assertEqual(len(self.store.inspect()['events']), 1 if proposal else 0)
                self.assert_original_unchanged(before)

    def test_same_known_reservation_is_not_counted_twice(self):
        self.replacement_fixture(articles=2, budget=1.9)
        campaign = copy.deepcopy(self.campaign)
        campaign['reserved_usd'] = 1
        self.state.save_campaign(campaign)
        self.state.save_task(copy.deepcopy(self.by_article[B].entry['provenance']['task']))
        self.state.save_batch({'id': 'accounted-batch', 'campaign': campaign['id'],
            'stage': 'translate', 'model': campaign['model'], 'tasks': [self.by_article[B].entry_id],
            'status': 'completed', 'reserved_usd': 1})
        before = files(self.root)
        proposal = self.propose()
        self.prepare(proposal)
        self.assert_original_unchanged(before)


    def reservation_chain(self):
        self.replacement_fixture(articles=2, budget=1.9)
        campaign = copy.deepcopy(self.campaign)
        campaign['reserved_usd'] = 1
        self.state.save_campaign(campaign)
        for identity, status in (('excluded-task', 'cancelled'), ('remaining-task', 'complete')):
            task = copy.deepcopy(self.by_article[B].entry['provenance']['task'])
            task.update(id=identity, status=status)
            self.state.save_task(task)
        parent = {'id': 'parent-batch', 'campaign': campaign['id'], 'stage': 'translate',
            'model': campaign['model'], 'tasks': ['excluded-task', 'remaining-task'],
            'status': 'cancelled_before_submission', 'reserved_usd': 1,
            'exclusion_reason': 'human_editorial_authority', 'excluded_task_ids': ['excluded-task'],
            'replacement_batch': 'replacement-batch', 'remote_id': None}
        child = {'id': 'replacement-batch', 'campaign': campaign['id'], 'stage': 'translate',
            'model': campaign['model'], 'tasks': ['remaining-task'], 'status': 'completed',
            'reserved_usd': 1, 'reservation_reused_from': parent['id']}
        self.state.save_batch(parent)
        self.state.save_batch(child)
        return parent, child

    def test_valid_partitioned_reservation_is_counted_once_without_current_batch_pointers(self):
        self.reservation_chain()
        before = files(self.root)
        observed = inspect_never_paid_hold(self.engine, self.campaign['id'], self.item.entry_id)
        self.assertEqual(Decimal(observed['consumed_usd']), Decimal(1))
        proposal = self.propose()
        self.prepare(proposal)
        self.assert_original_unchanged(before)

    def test_missing_or_malformed_reciprocal_reservation_links_are_held(self):
        for mutation in ('missing_parent_link', 'missing_child_link', 'wrong_parent_link',
                         'wrong_child_link', 'malformed_parent_link', 'malformed_child_link'):
            with self.subTest(mutation=mutation):
                parent, child = self.reservation_chain()
                if mutation == 'missing_parent_link': parent.pop('replacement_batch')
                elif mutation == 'missing_child_link': child.pop('reservation_reused_from')
                elif mutation == 'wrong_parent_link': parent['replacement_batch'] = 'unknown-child'
                elif mutation == 'wrong_child_link': child['reservation_reused_from'] = 'unknown-parent'
                elif mutation == 'malformed_parent_link': parent['replacement_batch'] = []
                else: child['reservation_reused_from'] = []
                self.state.save_batch(parent)
                self.state.save_batch(child)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assertEqual(self.store.inspect()['events'], [])
                self.assert_original_unchanged(before)

    def test_unknown_other_task_and_mismatched_batch_campaign_are_held(self):
        for mutation in ('missing_task', 'wrong_campaign'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture(articles=2)
                other = copy.deepcopy(self.by_article[B].entry['provenance']['task'])
                if mutation == 'wrong_campaign':
                    other['campaign'] = 'different-campaign'
                    self.state.save_task(other)
                self.state.save_batch({'id': 'unresolved-batch', 'campaign': self.campaign['id'],
                    'tasks': [other['id']], 'status': 'completed', 'reserved_usd': 0})
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)

    def test_malformed_or_duplicate_other_task_identity_is_held(self):
        for mutation in ('missing_field', 'duplicate_identity'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture(articles=2)
                task = copy.deepcopy(self.by_article[B].entry['provenance']['task'])
                if mutation == 'missing_field':
                    task.pop('language')
                    self.state.save_task(task)
                else:
                    self.state.save_task(task)
                    self.state.write('state/tasks/duplicate-copy/task.json', task)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)

    def test_present_cancellation_flag_requires_an_exact_boolean(self):
        for flag in (None, 0, 1, '', 'false', [], {}):
            with self.subTest(flag=flag):
                self.replacement_fixture()
                campaign = copy.deepcopy(self.campaign)
                campaign['cancel_requested'] = flag
                self.state.save_campaign(campaign)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)
        self.replacement_fixture()
        campaign = copy.deepcopy(self.campaign)
        campaign['cancel_requested'] = False
        self.state.save_campaign(campaign)
        self.prepare(self.propose())

    def test_prepare_rechecks_budget_consumption_since_proposal(self):
        self.replacement_fixture(budget=.9)
        proposal = self.propose()
        changed = copy.deepcopy(self.campaign)
        changed['reserved_usd'] = .2
        self.state.save_campaign(changed)
        before = files(self.root)
        with self.assertRaises(ContractError): self.prepare(proposal)
        self.assertEqual(len(self.store.inspect()['events']), 1)
        self.assert_original_unchanged(before)

    def test_all_pairs_share_original_cap_and_cancellation_never_refunds_projection(self):
        self.replacement_fixture(articles=2, budget=.9)
        before = files(self.root)
        first = self.propose()
        amount = Decimal(first['proposal']['ceiling']['total_usd'])
        self.assertLessEqual(amount, Decimal(str(self.campaign['budget_usd'])))
        with self.assertRaises(ContractError): self.propose(self.by_article[B])
        self.store.cancel(first['revision_id'], 'Cancelled without recycling its allocation.')
        with self.assertRaises(ContractError): self.propose(self.by_article[B])
        self.assertEqual(self.store.inspect()['projected_allocations'], {self.campaign['id']: str(amount)})
        self.assert_original_unchanged(before)

    def test_crash_before_or_after_proposal_commit_retries_without_duplicate_allocation(self):
        class Crash(RuntimeError): pass
        for boundary in ('before_commit', 'after_commit'):
            with self.subTest(boundary=boundary):
                self.replacement_fixture()
                before = files(self.root)
                head = self.store.inspect()['head']
                def fault(point):
                    if point == boundary: raise Crash(point)
                with self.assertRaises(Crash): self.propose(expected_head=head, fault=fault)
                expected = 0 if boundary == 'before_commit' else 1
                self.assertEqual(len(self.store.inspect()['events']), expected)
                proposal = self.propose(expected_head=head)
                inspection = self.store.inspect()
                self.assertEqual(len(inspection['events']), 1)
                self.assertEqual(inspection['projected_allocations'], {
                    self.campaign['id']: proposal['proposal']['ceiling']['total_usd']})
                self.assertFalse(list((self.journal / 'events').glob('.tmp-*')))
                self.assert_original_unchanged(before)

    def test_crash_before_or_after_prepare_and_cancel_is_replayable(self):
        class Crash(RuntimeError): pass
        for operation in ('prepare', 'cancel'):
            for boundary in ('before_commit', 'after_commit'):
                with self.subTest(operation=operation, boundary=boundary):
                    self.replacement_fixture()
                    before = files(self.root)
                    proposal = self.propose()
                    allocation = self.store.inspect()['projected_allocations']
                    head = self.store.inspect()['head']
                    def fault(point):
                        if point == boundary: raise Crash(point)
                    def invoke(**kwargs):
                        if operation == 'prepare': return self.prepare(proposal, **kwargs)
                        return self.store.cancel(proposal['revision_id'], 'Offline cancellation.', **kwargs)
                    with self.assertRaises(Crash): invoke(expected_head=head, fault=fault)
                    self.assertEqual(len(self.store.inspect()['events']),
                                     1 if boundary == 'before_commit' else 2)
                    invoke(expected_head=head)
                    inspection = self.store.inspect()
                    self.assertEqual(len(inspection['events']), 2)
                    self.assertEqual(inspection['projected_allocations'], allocation)
                    self.assert_original_unchanged(before)

    def test_stale_compare_and_swap_cannot_append_new_proposal_prepare_or_cancel(self):
        self.replacement_fixture(articles=2)
        empty_head = self.store.inspect()['head']
        first = self.propose(expected_head=empty_head)
        first_head = self.store.inspect()['head']
        with self.assertRaisesRegex(ContractError, 'Stale'):
            self.propose(self.by_article[B], expected_head=empty_head)
        second = self.propose(self.by_article[B], expected_head=first_head)
        second_head = self.store.inspect()['head']
        with self.assertRaisesRegex(ContractError, 'Stale'):
            self.prepare(first, expected_head=first_head)
        self.prepare(first, expected_head=second_head)
        with self.assertRaisesRegex(ContractError, 'Stale'):
            self.store.cancel(second['revision_id'], 'Cancel private projection.', expected_head=second_head)
        self.store.cancel(second['revision_id'], 'Cancel private projection.',
                          expected_head=self.store.inspect()['head'])
        self.assertEqual(len(self.store.inspect()['events']), 4)

    def test_final_read_before_commit_rechecks_human_source_and_budget_drift(self):
        for boundary in ('before_recheck', 'before_commit'):
            for operation in ('propose', 'prepare'):
                for mutation in ('human', 'source', 'budget', 'cancel'):
                    with self.subTest(boundary=boundary, operation=operation, mutation=mutation):
                        self.replacement_fixture(budget=.9)
                        proposal = self.propose() if operation == 'prepare' else None
                        def fault(point):
                            if point != boundary: return
                            if mutation == 'human':
                                record = self.state.record('deu', A)
                                record['history'].append({'event': 'human_review'})
                                self.state.save_record(record)
                            elif mutation == 'source':
                                source = self.state.read('state/source.json')
                                source['articles'][A]['translation_key'] = 'c' * 64
                                self.state.write('state/source.json', source)
                            else:
                                campaign = copy.deepcopy(self.campaign)
                                campaign.update({'reserved_usd': .2} if mutation == 'budget'
                                                else {'cancel_requested': True})
                                self.state.save_campaign(campaign)
                        with self.assertRaises(ContractError):
                            self.prepare(proposal, fault=fault) if proposal else self.propose(fault=fault)
                        self.assertEqual(len(self.store.inspect()['events']), 1 if proposal else 0)
                        self.assertEqual((self.paid.upload_calls, self.paid.create_calls), (0, 0))

    def test_tampered_reordered_and_truncated_journals_are_never_replayed(self):
        for mutation in ('malformed_json', 'bytes', 'swap', 'missing_first', 'invalid_name'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                proposal = self.propose()
                self.prepare(proposal)
                paths = self.journal_events()
                if mutation == 'malformed_json': paths[0].write_bytes(b'{')
                elif mutation == 'bytes': paths[0].write_bytes(paths[0].read_bytes() + b' ')
                elif mutation == 'swap':
                    first, second = [path.read_bytes() for path in paths]
                    paths[0].write_bytes(second); paths[1].write_bytes(first)
                elif mutation == 'missing_first': paths[0].unlink()
                else: paths[0].rename(paths[0].with_name('not-an-event.json'))
                before = files(self.root)
                with self.assertRaises(ContractError): self.store.inspect()
                with self.assertRaises(ContractError): self.prepare(proposal)
                self.assert_original_unchanged(before)

    def test_rehashed_semantically_invalid_journal_still_fails_closed(self):
        for mutation in ('funding', 'ceiling', 'advanced_cycle'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                self.propose()
                path = self.journal_events()[0]
                value = json.loads(path.read_bytes())
                if mutation == 'funding': value['event']['funding_authorized'] = True
                elif mutation == 'ceiling': value['event']['proposal']['ceiling']['total_usd'] = '0'
                else:
                    cycle = OfflineComponentCycle.restore(value['event']['proposal']['cycle'])
                    cycle.request()
                    value['event']['proposal']['cycle'] = cycle.snapshot()
                path.unlink()
                replacement = path.with_name(f'{value["sequence"]:08d}-{json_hash(value)}.json')
                replacement.write_bytes(canonical(value) + b'\n')
                with self.assertRaises(ContractError): self.store.inspect()


    def test_rehashed_processing_cannot_substitute_original_model_price_or_contract(self):
        for mutation in ('model', 'price', 'output_limit', 'language', 'prompt'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                proposal = self.propose()
                path = self.journal_events()[0]
                wrapper = json.loads(path.read_bytes())
                value = wrapper['event']['proposal']
                context = value['cycle']['context']
                processing = context['processing']
                model = processing['models'][processing['generation_model']]
                if mutation == 'model': model['api_model'] = 'substituted-model'
                elif mutation == 'price':
                    model.update(input_batch_usd_per_million=0, output_batch_usd_per_million=0)
                elif mutation == 'output_limit': processing['max_output_tokens'] -= 1
                elif mutation == 'language': processing['language_settings']['name'] = 'Substituted language'
                else:
                    processing['prompts']['generation'] = 'A substituted base generation prompt.'
                    processing['prompts_sha256'] = json_hash(processing['prompts'])
                value['cycle'] = OfflineComponentCycle(context['source'], context['evidence'],
                    context['scripture_policy'], processing).snapshot()
                value['ceiling'] = complete_cycle_ceiling(processing)
                path.unlink()
                replacement = path.with_name(f'{wrapper["sequence"]:08d}-{json_hash(wrapper)}.json')
                replacement.write_bytes(canonical(wrapper) + b'\n')
                before = files(self.root)
                with self.assertRaises(ContractError): self.store.inspect()
                with self.assertRaises(ContractError): self.prepare(proposal)
                self.assert_original_unchanged(before)

    def test_concurrent_same_proposals_commit_exactly_once(self):
        before = files(self.root)
        results = self.race_proposals([(self.item, self.item.policy, self.item.evidence)] * 4)
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(len({result['revision_id'] for result in results}), 1)
        self.assertEqual(len(self.store.inspect()['events']), 1)
        self.assert_original_unchanged(before)

    def test_concurrent_different_proposals_for_same_entry_have_only_one_winner(self):
        policy, evidence = self.variant()
        results = self.race_proposals([(self.item, self.item.policy, self.item.evidence),
                                      (self.item, policy, evidence)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ContractError) for result in results), 1)
        self.assertEqual(len(self.store.inspect()['events']), 1)

    def test_concurrent_different_pairs_cannot_oversubscribe_the_original_budget(self):
        self.replacement_fixture(articles=2, budget=.9)
        before = files(self.root)
        results = self.race_proposals([(item, item.policy, item.evidence)
                                      for item in self.by_article.values()])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ContractError) for result in results), 1)
        view = self.store.inspect()
        self.assertEqual(len(view['events']), 1)
        self.assertLessEqual(Decimal(view['projected_allocations'][self.campaign['id']]),
                             Decimal(str(self.campaign['budget_usd'])))
        self.assert_original_unchanged(before)


    def test_separate_processes_serialize_shared_budget_admission(self):
        self.replacement_fixture(articles=2, budget=.9)
        before = files(self.root)
        context = multiprocessing.get_context('fork')
        barrier, output = context.Barrier(2), context.Queue()
        def attempt(item):
            try:
                store = OfflineAdmissionRevisionStore(self.journal, self.root)
                barrier.wait(timeout=10)
                value = store.propose(self.engine, self.campaign['id'], item.entry_id,
                                      item.policy, item.evidence, self.root)
                output.put(('proposed', value['revision_id']))
            except ContractError as exc:
                output.put(('held', str(exc)))
            except Exception as exc:
                output.put(('unexpected', repr(exc)))
        processes = [context.Process(target=attempt, args=(item,)) for item in self.by_article.values()]
        try:
            for process in processes: process.start()
            results = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sorted(result[0] for result in results), ['held', 'proposed'], results)
            self.assertEqual(len(self.store.inspect()['events']), 1)
            self.assert_original_unchanged(before)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            output.close()
            output.join_thread()

    def test_concurrent_preparation_appends_only_one_prepared_event(self):
        proposal = self.propose()
        barrier = threading.Barrier(3)
        def prepare():
            store = OfflineAdmissionRevisionStore(self.journal, self.root)
            barrier.wait(timeout=10)
            return store.prepare(self.engine, proposal['revision_id']).snapshot()
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(prepare) for _ in range(3)]
            snapshots = [future.result(timeout=20) for future in futures]
        self.assertEqual(snapshots, [snapshots[0]] * 3)
        self.assertEqual(len(self.store.inspect()['events']), 2)


    def test_storage_rechecks_root_and_event_directory_before_any_write(self):
        for location in ('events', 'root'):
            with self.subTest(location=location):
                self.replacement_fixture()
                target = self.root / 'state/redirected-private-journal'
                target.mkdir()
                if location == 'events':
                    (self.journal / 'events').rmdir()
                    (self.journal / 'events').symlink_to(target, target_is_directory=True)
                else:
                    (target / 'events').mkdir()
                    self.journal.rename(self.journal.with_name('original-private-revisions'))
                    self.journal.symlink_to(target, target_is_directory=True)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)


    def test_replaced_lock_inode_is_rejected_before_journal_or_state_writes(self):
        lock = self.journal / '.lock'
        lock.rename(self.journal / '.retired-lock')
        lock.write_bytes(b'')
        before = files(self.root)
        journal_before = files(self.journal)
        with self.assertRaises(ContractError): self.store.inspect()
        with self.assertRaises(ContractError): self.propose()
        self.assertEqual(files(self.journal), journal_before)
        self.assert_original_unchanged(before)

    def test_fifo_replacing_event_between_stat_and_open_fails_without_blocking(self):
        proposal = self.propose()
        event = self.journal_events()[0]
        original_open = os.open
        replaced = []
        def replace_before_open(path, flags, *args, **kwargs):
            if path == event.name and flags & os.O_NOFOLLOW:
                event.unlink()
                os.mkfifo(event)
                replaced.append(path)
            return original_open(path, flags, *args, **kwargs)
        before = files(self.root)
        with patch('berean_translation.scripture_admission_revisions.os.open',
                   side_effect=replace_before_open):
            with self.assertRaises(ContractError): self.store.inspect()
        self.assertEqual(replaced, [event.name])
        with self.assertRaises(ContractError): self.prepare(proposal)
        self.assert_original_unchanged(before)

    def test_partial_uncommitted_temporary_file_is_not_a_durable_event(self):
        temporary = self.journal / 'events/.tmp-interrupted-process'
        temporary.write_bytes(b'{"unfinished":')
        before = files(self.root)
        self.assertEqual(self.store.inspect()['events'], [])
        self.propose()
        self.assertEqual(len(self.store.inspect()['events']), 1)
        self.assertEqual(temporary.read_bytes(), b'{"unfinished":')
        self.assert_original_unchanged(before)

    def test_unrelated_source_revision_keeps_original_content_bound_hold_usable(self):
        current = self.state.read('state/source.json')
        current['revision'] = 'b' * 40
        self.state.write('state/source.json', current)
        before = files(self.root)
        proposal = self.propose()
        self.prepare(proposal)
        self.assertEqual(proposal['proposal']['origin']['source_sha256'], json_hash(self.item.source))
        self.assert_original_unchanged(before)


    def test_journal_ancestor_cannot_place_its_events_inside_live_state(self):
        journal = self.parent / 'ancestor-journal'
        state_root = journal / 'events'
        state_root.mkdir(parents=True)
        (state_root / 'original.txt').write_text('Original synthetic state.')
        before = files(state_root)
        with self.assertRaises(ContractError):
            OfflineAdmissionRevisionStore(journal, state_root)
        self.assertEqual(files(state_root), before)

    def test_retained_head_detects_deleted_cancellation_tail_even_on_idempotent_paths(self):
        proposal = self.propose()
        self.prepare(proposal)
        self.store.cancel(proposal['revision_id'], 'Preserve this cancellation.')
        retained = self.store.inspect()['head']
        sorted((self.journal / 'events').glob('*.json'))[-1].unlink()
        self.assertNotEqual(self.store.inspect()['head'], retained)
        with self.assertRaises(ContractError): self.propose(expected_head=retained)
        with self.assertRaises(ContractError): self.prepare(proposal, expected_head=retained)
        with self.assertRaises(ContractError):
            self.store.cancel(proposal['revision_id'], 'Do not recreate a lost tail.', expected_head=retained)

    def test_storage_cannot_live_in_the_live_repository(self):
        for destination in (self.root, self.root / 'state/revisions'):
            with self.subTest(destination=destination), self.assertRaises(ContractError):
                OfflineAdmissionRevisionStore(destination, self.root)
        alias = self.parent / 'repository-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ContractError):
            OfflineAdmissionRevisionStore(alias / 'private-revisions', self.root)


if __name__ == '__main__':
    unittest.main()
