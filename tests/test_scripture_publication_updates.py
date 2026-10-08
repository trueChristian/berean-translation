"""Completed-publication update journals, using only disposable offline fixtures.

The predecessor is accepted by the real runtime with FakeProvider Batch rows.
Every update runs outside repository State; sockets and real GetBible transport
are blocked, and every repository byte is compared before and after each flow.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from berean_translation.common import ContractError, canonical, digest, json_hash
from berean_translation.scripture_association import associate
from berean_translation.scripture_component_evidence import component_policy
from berean_translation.scripture_evidence import build_evidence
from berean_translation.scripture_component_processing import processing_contract
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.scripture_publication_updates import (
    OfflinePublicationUpdateCycle, OfflinePublicationUpdateStore,
    inspect_completed_publication, _projection)
from berean_translation.state import State
from support import A, B, REPO_ROOT, drive, queue, setup
from test_scripture_component_evidence import insertion
from test_scripture_component_processing import provider_row, verdict
from test_scripture_evidence import FakeMCP, fixture


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def completed_fixture(parent, *, articles=1, model='gpt-4.1-mini',
                      review_model='gpt-4.1-mini', review_version=2, rereview=False):
    """Generate genuine completed task, accepted files, and archived results."""
    root = parent / 'repository'
    root.mkdir()
    config, state, upstream, provider, git, engine = setup(
        root, review_contract_version=review_version)
    shutil.copytree(REPO_ROOT / 'data', root / 'data')
    shutil.copytree(REPO_ROOT / 'docs/third-party', root / 'docs/third-party')
    english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
    target = fixture('luther1545')['structuredContent']['data']['verses'][1]['text']
    printed, operations = insertion(english, '[indeed]')
    translated, target_operations = insertion(target, '[wirklich]')
    upstream.articles = upstream.articles[:articles]
    for item in upstream.articles:
        upstream.contents[item['html']['repository_path']] = (
            f'<article data-article-id="{item["id"]}"><p>“{printed}” (John 4:16).</p>'
            f'<figure><img src="/images/articles/{item["id"]}-1.jpg" alt="A tree">'
            '<figcaption>A tree.</figcaption></figure></article>')
    upstream.rebuild()
    queue(state, languages='deu', budget_usd=5, model=model, review_model=review_model)
    drive(engine, provider)
    if rereview:
        queue(state, 'review-completed', operation='review', languages='deu', issues='all',
              budget_usd=5, model=model, review_model=review_model)
        drive(engine, provider)
    campaign = state.read('state/campaigns/review-completed.json') if rereview else state.campaigns()[0]
    by_article, fake = {}, FakeMCP()
    for task in state.tasks():
        if state.record(task['language'], task['article_id']).get('latest_task') != task['id']:
            continue
        if task['status'] != 'complete':
            raise AssertionError('Fixture must reach actual accepted runtime publication')
        source = state.source(task)
        record = state.record(task['language'], task['article_id'])
        publication = record['published']
        accepted, _, _ = state.publication_candidate(publication)
        archived = state.candidate(task)
        if accepted != archived:
            raise AssertionError('Runtime accepted candidate and archive must agree')
        scopes = associate(source['html'], source['article']['id'])[2]
        policy = component_policy(root, source, [
            {'scope_sha256': json_hash(scopes[0]), 'operations': operations}])
        evidence = build_evidence(source, 'de', policy, GetBibleMCP(fake))
        candidate = copy.deepcopy(accepted)
        candidate['html'] = candidate['html'].replace(printed, translated)
        candidate['title'] = 'Lehre'
        selections = [{'quote_id': 'q1', 'block': '/article[1]/p[1]',
            'start': 1, 'end': 1 + len(translated), 'operations': target_operations}]
        by_article[task['article_id']] = SimpleNamespace(task=task, source=source,
            record=record, publication=publication, accepted=accepted, archived=archived,
            policy=policy, evidence=evidence, candidate=candidate, selections=selections)
    journal = parent / 'private-updates'
    store = OfflinePublicationUpdateStore(journal, root)
    funding = {'id': 'offline-update-envelope', 'currency': 'USD', 'budget_usd': 5,
        'reserved_usd': 0, 'reported_usage_usd': 0, 'offline_only': True,
        'funding_authorized': False}
    return SimpleNamespace(root=root, config=config, state=state, upstream=upstream,
        provider=provider, git=git, engine=engine, campaign=campaign, by_article=by_article,
        fake=fake, journal=journal, store=store, funding=funding,
        original_provider_calls=(provider.upload_calls, provider.create_calls))


class OfflinePublicationUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            blocker = patch(target, side_effect=AssertionError('Network forbidden'))
            blocker.start()
            self.addCleanup(blocker.stop)
        blocker = patch.object(GetBibleMCP, '_remote', side_effect=AssertionError('No remote GetBible'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.use_fixture(completed_fixture(self.parent))

    def use_fixture(self, value):
        self.fixture = value
        for key, content in vars(value).items(): setattr(self, key, content)
        self.item = self.by_article[A]

    def replacement_fixture(self, **kwargs):
        parent = self.parent / ('fixture-' + str(len(list(self.parent.iterdir()))))
        parent.mkdir()
        self.use_fixture(completed_fixture(parent, **kwargs))

    def propose(self, item=None, **kwargs):
        item = self.item if item is None else item
        return self.store.propose(self.engine, 'deu', item.task['article_id'],
            kwargs.pop('predecessor_task_id', item.task['id']),
            kwargs.pop('scripture_policy', item.policy), kwargs.pop('evidence', item.evidence),
            self.root, kwargs.pop('funding', self.funding), **kwargs)

    def value(self, update):
        return self.store.inspect()['updates'][update['update_id']]

    def snapshot(self, update):
        return self.value(update)['cycle']

    def request(self, update, **kwargs):
        return self.store.request(self.engine, update['update_id'], **kwargs)

    def receive(self, update, row, **kwargs):
        return self.store.receive(self.engine, update['update_id'], row, **kwargs)

    def accept(self, update, **kwargs):
        return self.store.accept(self.engine, update['update_id'], **kwargs)

    def generation(self, request, item=None, candidate=None):
        item = self.item if item is None else item
        return provider_row(request, {'binding': copy.deepcopy(request['binding']),
            'candidate': copy.deepcopy(item.candidate if candidate is None else candidate),
            'scripture_selections': copy.deepcopy(item.selections)})

    def review(self, request, report=None):
        return provider_row(request, {'binding': copy.deepcopy(request['binding']),
            'review': verdict() if report is None else report})

    def to_review(self, update=None, item=None):
        update = self.propose(item) if update is None else update
        generation = self.request(update)
        self.receive(update, self.generation(generation, item))
        return update, self.request(update)

    def approved(self):
        update, review = self.to_review()
        self.receive(update, self.review(review))
        return update

    def assert_original_unchanged(self, before):
        self.assertEqual(files(self.root), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls),
                         self.original_provider_calls)

    def events(self):
        return sorted((self.journal / 'events').glob('*.json'))

    def variant(self, item=None):
        item = self.item if item is None else item
        policy = copy.deepcopy(item.policy)
        policy['max_component_candidate_bytes'] -= 1
        return policy, build_evidence(item.source, 'de', policy, GetBibleMCP(self.fake))

    def test_fixture_has_actual_completed_publication_and_source_candidate_hashes(self):
        item = self.item
        self.assertEqual(item.task['status'], 'complete')
        self.assertEqual(item.record['latest_task'], item.task['id'])
        self.assertEqual(item.publication['task'], item.task['id'])
        self.assertEqual(item.task['source_snapshot'], f'state/sources/{json_hash(item.source)}.json')
        self.assertEqual(item.accepted, item.archived)
        self.assertEqual(digest(self.state.path(item.publication['html_path']).read_bytes()),
                         item.publication['html_sha256'])
        self.assertEqual(json_hash(self.state.read(item.publication['metadata_path'])),
                         item.publication['metadata_sha256'])
        self.assertEqual((item.task['translation_attempts'], item.task['review_attempts']), (1, 1))
        self.assertEqual(self.original_provider_calls, (2, 2))
        before = files(self.root)
        proof = inspect_completed_publication(self.engine, 'deu', A, item.task['id'])
        self.assertIsInstance(proof, dict)
        self.assert_original_unchanged(before)

    def test_complete_update_is_durable_but_never_changes_state_or_publication(self):
        before, reads = files(self.root), len(self.fake.calls)
        with patch.object(State, 'write', side_effect=AssertionError('Live State mutation forbidden')):
            update = self.approved()
            self.assertEqual(self.snapshot(update)['status'], 'review_accepted_offline')
            self.assertEqual(self.snapshot(update)['candidate'], self.item.candidate)
            self.assertFalse(self.snapshot(update)['publication_authorized'])
            self.accept(update)
            self.assertEqual(self.value(update)['status'], 'accepted_offline')
            self.assertEqual(self.snapshot(update)['generation_attempts'], 1)
            self.assertEqual(self.snapshot(update)['review_attempts'], 1)
        self.assertEqual(len(self.fake.calls), reads)
        self.assert_original_unchanged(before)
        reopened = OfflinePublicationUpdateStore(self.journal, self.root)
        self.assertEqual(reopened.inspect(), self.store.inspect())
        self.assertEqual(len(self.store.inspect()['events']), 6)

    def test_original_funding_attempts_models_and_prompts_remain_frozen(self):
        original = copy.deepcopy(self.campaign)
        self.config.models['gpt-4.1-mini']['input_batch_usd_per_million'] = 99999
        self.config.runtime.update(default_model='gpt-6-luna', max_output_tokens=17)
        self.config.languages['deu']['tag'] = 'af'
        (self.root / 'prompts/translation.txt').write_text('Changed current defaults')
        before = files(self.root)
        update = self.propose()
        request = self.request(update)
        processing = self.snapshot(update)['context']['processing']
        for model in (processing['generation_model'], processing['review_model']):
            self.assertEqual(processing['models'][model], original['models'][model])
        self.assertEqual(request['line']['body']['model'], original['models'][original['model']]['api_model'])
        self.assertTrue(processing['prompts']['generation'].startswith(original['prompts']['translation']))
        self.assertEqual(self.state.read(f'state/campaigns/{original["id"]}.json'), original)
        self.assertEqual(self.state.read(f'state/tasks/{self.item.task["id"]}/task.json'), self.item.task)
        self.assert_original_unchanged(before)

    def test_legacy_completed_review_contract_can_only_start_fresh_complete_review(self):
        self.replacement_fixture(review_version=None)
        self.assertNotIn('review_contract_version', self.campaign)
        before = files(self.root)
        update, review = self.to_review()
        schema = review['line']['body']['response_format']['json_schema']['schema']
        self.assertIn('findings_complete', schema['properties']['review']['required'])
        self.receive(update, self.review(review))
        self.accept(update)
        self.assert_original_unchanged(before)

    def test_fresh_review_is_required_and_prior_scores_are_not_review_input(self):
        before = files(self.root)
        update = self.propose()
        with self.assertRaises(ContractError): self.accept(update)
        generation = self.request(update)
        with self.assertRaises(ContractError): self.accept(update)
        self.receive(update, self.generation(generation))
        with self.assertRaises(ContractError): self.accept(update)
        review = self.request(update)
        payload = json.loads(review['line']['body']['messages'][1]['content'])
        self.assertEqual(review['binding']['candidate_sha256'], json_hash(self.item.candidate))
        self.assertEqual(payload['translation'], self.item.candidate)
        def assert_no_verdict(value):
            if isinstance(value, dict):
                self.assertFalse({'score', 'quality_score', 'passed', 'findings'} & set(value))
                for child in value.values(): assert_no_verdict(child)
            elif isinstance(value, list):
                for child in value: assert_no_verdict(child)
        assert_no_verdict(payload)
        self.assert_original_unchanged(before)

    def test_failed_update_keeps_last_accepted_publication_and_frozen_allocation(self):
        before = files(self.root)
        update, review = self.to_review()
        allocation = self.store.inspect()['projected_allocations']
        self.receive(update, self.review(review, verdict(score=94, passed=False)))
        self.assertEqual(self.snapshot(update)['status'], 'held')
        with self.assertRaises(ContractError): self.accept(update)
        with self.assertRaises(ContractError): self.request(update)
        self.assertEqual(self.store.inspect()['projected_allocations'], allocation)
        self.assertEqual(self.state.record('deu', A)['published'], self.item.publication)
        self.assert_original_unchanged(before)

    def test_bounded_correction_then_failed_final_review_never_restarts_attempts(self):
        before = files(self.root)
        update, review = self.to_review()
        self.receive(update, self.review(review, verdict(score=90, passed=False, severity='major')))
        correction = self.request(update)
        self.assertEqual(correction['stage'], 'correct')
        corrected = copy.deepcopy(self.item.candidate)
        corrected['title'] = 'Unterweisung'
        self.receive(update, self.generation(correction, candidate=corrected))
        final = self.request(update)
        self.assertEqual(final['stage'], 'review2')
        self.receive(update, self.review(final, verdict(score=99, severity='critical')))
        snapshot = self.snapshot(update)
        self.assertEqual(snapshot['status'], 'held')
        self.assertEqual((snapshot['generation_attempts'], snapshot['review_attempts']), (2, 2))
        with self.assertRaises(ContractError): self.request(update)
        with self.assertRaises(ContractError): self.accept(update)
        self.assertEqual(self.propose()['update_id'], update['update_id'])
        self.assertEqual(self.snapshot(update), snapshot)
        self.assert_original_unchanged(before)

    def test_archived_accepted_review_cannot_be_recycled_for_new_candidate(self):
        before = files(self.root)
        update, review = self.to_review()
        old = self.state.read(f'state/tasks/{self.item.task["id"]}/results/review1.json')
        with self.assertRaises(ContractError): self.receive(update, old)
        # Even relabelling the old transport ID cannot manufacture new bindings.
        old['custom_id'] = review['line']['custom_id']
        self.receive(update, old)
        self.assertEqual(self.snapshot(update)['status'], 'held')
        with self.assertRaises(ContractError): self.accept(update)
        self.assert_original_unchanged(before)

    def test_old_first_review_binding_cannot_approve_corrected_candidate(self):
        update, first = self.to_review()
        archived = self.review(first)
        self.receive(update, self.review(first, verdict(score=90, passed=False, severity='major')))
        correction = self.request(update)
        corrected = copy.deepcopy(self.item.candidate); corrected['title'] = 'Unterweisung'
        self.receive(update, self.generation(correction, candidate=corrected))
        final = self.request(update)
        archived['custom_id'] = final['line']['custom_id']
        self.receive(update, archived)
        self.assertEqual(self.snapshot(update)['status'], 'held')
        with self.assertRaises(ContractError): self.accept(update)

    def test_completed_predecessor_stage_and_attempt_bounds_are_not_resettable(self):
        changes = [('translation_attempts', value) for value in (True, -1, 3, 999, 1.0)]
        changes += [('review_attempts', value) for value in (True, 0, 3, 999, 1.0)]
        changes += [('stage', value) for value in ('translate', 'correct', 'queued')]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.replacement_fixture()
                task = copy.deepcopy(self.item.task); task[key] = value
                self.state.save_task(task)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assertEqual(self.store.inspect()['events'], [])
                self.assert_original_unchanged(before)

    def test_genuine_completed_review_only_predecessor_keeps_zero_generation_history(self):
        self.replacement_fixture(rereview=True)
        self.assertEqual(self.campaign['operation'], 'review')
        self.assertEqual(self.item.task['translation_attempts'], 0)
        self.assertEqual(self.item.task['review_attempts'], 1)
        before = files(self.root)
        update = self.approved(); self.accept(update)
        self.assertEqual(self.snapshot(update)['generation_attempts'], 1)
        self.assertEqual(self.state.read(f'state/tasks/{self.item.task["id"]}/task.json'), self.item.task)
        self.assert_original_unchanged(before)

    def test_exact_predecessor_is_required_not_just_matching_published_bytes(self):
        for mutation in ('wrong_id', 'latest', 'publication_task', 'incomplete', 'failed', 'missing'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                identity = self.item.task['id']
                if mutation == 'wrong_id': identity = 'another-task'
                elif mutation in ('latest', 'publication_task'):
                    record = copy.deepcopy(self.item.record)
                    if mutation == 'latest': record['latest_task'] = 'another-task'
                    else: record['published']['task'] = 'another-task'
                    self.state.save_record(record)
                elif mutation == 'missing': self.state.path(f'state/tasks/{identity}/task.json').unlink()
                else:
                    task = copy.deepcopy(self.item.task)
                    task['status'] = 'awaiting_review' if mutation == 'incomplete' else 'not_ready'
                    self.state.save_task(task)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose(predecessor_task_id=identity)
                self.assertEqual(self.store.inspect()['events'], [])
                self.assert_original_unchanged(before)

    def mutate_predecessor(self, mutation):
        if mutation in ('source', 'removed'):
            source = self.state.read('state/source.json')
            if mutation == 'source': source['articles'][A]['translation_key'] = 'e' * 64
            else: del source['articles'][A]
            self.state.write('state/source.json', source)
        elif mutation == 'snapshot':
            source = copy.deepcopy(self.item.source); source['html'] += '<p>Changed.</p>'
            self.state.write(self.item.task['source_snapshot'], source)
        elif mutation == 'candidate':
            candidate = copy.deepcopy(self.item.archived); candidate['title'] += '!'
            self.state.write(f'state/tasks/{self.item.task["id"]}/candidate.json', candidate)
        elif mutation in ('html', 'metadata', 'notice'):
            key = 'metadata_path' if mutation == 'metadata' else 'html_path'
            path = self.state.path(self.item.publication[key])
            if mutation == 'metadata':
                metadata = self.state.read(self.item.publication[key]); metadata['title'] += ' changed'
                self.state.write(self.item.publication[key], metadata)
            else:
                path.write_bytes(path.read_bytes() + (b' ' if mutation != 'notice' else b'<aside>Edited</aside>'))
        elif mutation == 'campaign':
            campaign = copy.deepcopy(self.campaign); campaign['models']['gpt-4.1-mini']['api_model'] = 'changed'
            self.state.save_campaign(campaign)
        elif mutation == 'task':
            task = copy.deepcopy(self.item.task); task['translation_key'] = 'f' * 64
            self.state.save_task(task)
        elif mutation == 'archived_review':
            path = f'state/tasks/{self.item.task["id"]}/results/review1.json'
            review = self.state.read(path); review['audit-drift'] = True
            self.state.write(path, review)
        else:
            record = copy.deepcopy(self.item.record)
            if mutation == 'human': record['published']['human_reviewed'] = True
            elif mutation == 'human_history': record['history'].append({'event': 'human_review'})
            elif mutation == 'edit_issue': record['published']['edit_issue'] = {'reason': 'human editorial issue'}
            elif mutation == 'latest': record['latest_task'] = 'newer-task'
            else: record['published']['source_revision'] = 'f' * 40
            self.state.save_record(record)

    def test_source_publication_candidate_human_and_frozen_predecessor_drift_block_new_work(self):
        mutations = ('source', 'removed', 'snapshot', 'candidate', 'html', 'metadata', 'notice',
                     'human', 'human_history', 'edit_issue', 'latest', 'publication', 'campaign', 'task')
        for phase in ('propose', 'request', 'receive', 'accept'):
            for mutation in mutations:
                with self.subTest(phase=phase, mutation=mutation):
                    self.replacement_fixture()
                    update = None if phase == 'propose' else self.propose()
                    row = self.generation(self.request(update)) if phase == 'receive' else None
                    if phase == 'accept':
                        update, review = self.to_review(update)
                        self.receive(update, self.review(review))
                    self.mutate_predecessor(mutation)
                    before, journal = files(self.root), files(self.journal)
                    with self.assertRaises(ContractError):
                        if phase == 'propose': self.propose()
                        elif phase == 'request': self.request(update)
                        elif phase == 'receive': self.receive(update, row)
                        else: self.accept(update)
                    self.assertEqual(files(self.journal), journal)
                    self.assert_original_unchanged(before)

    def test_distinct_funding_is_required_and_predecessor_money_cannot_be_recycled(self):
        before = files(self.root)
        for bad in (None, {}, {'budget_usd': 5}, {**self.funding, 'funding_authorized': True},
                    {**self.funding, 'currency': 'EUR'}, {**self.funding, 'budget_usd': True},
                    {**self.funding, 'budget_usd': 'NaN'}, {**self.funding, 'reserved_usd': -1}):
            with self.subTest(funding=bad), self.assertRaises(ContractError): self.propose(funding=bad)
        self.assertEqual(self.store.inspect()['events'], [])
        self.assert_original_unchanged(before)

    def test_insufficient_envelope_never_appends_partial_cycle(self):
        self.funding['budget_usd'] = .01
        before = files(self.root)
        with self.assertRaises(ContractError): self.propose()
        self.assertEqual(self.store.inspect()['events'], [])
        self.assertEqual(self.store.inspect()['projected_allocations'], {})
        self.assert_original_unchanged(before)

    def test_all_pairs_share_one_frozen_envelope_and_cancellation_never_refunds(self):
        self.replacement_fixture(articles=2)
        self.funding['budget_usd'] = .9
        before = files(self.root)
        update = self.propose()
        amount = Decimal(update['proposal']['ceiling']['total_usd'])
        self.assertLessEqual(amount, Decimal('.9'))
        with self.assertRaises(ContractError): self.propose(self.by_article[B])
        self.store.cancel(update['update_id'], 'Stop the offline preview.')
        with self.assertRaises(ContractError): self.propose(self.by_article[B])
        self.assertEqual(self.store.inspect()['projected_allocations'], {self.funding['id']: str(amount)})
        self.assert_original_unchanged(before)

    def test_consumed_envelope_and_changed_funding_id_do_not_reset_predecessor(self):
        for consumed in ('reserved_usd', 'reported_usage_usd'):
            with self.subTest(consumed=consumed):
                self.replacement_fixture()
                self.funding.update(budget_usd=.9, **{consumed: .2})
                with self.assertRaises(ContractError): self.propose()
                self.assertEqual(self.store.inspect()['events'], [])
        self.replacement_fixture()
        update = self.propose()
        allocation = self.store.inspect()['projected_allocations']
        self.store.cancel(update['update_id'], 'Cancellation is permanent.')
        funding = {**self.funding, 'id': 'new-envelope', 'budget_usd': 500}
        with self.assertRaises(ContractError): self.propose(funding=funding)
        self.assertEqual(self.store.inspect()['projected_allocations'], allocation)
        self.assertEqual(self.propose()['status'], 'cancelled')

    def test_same_envelope_cannot_change_cap_consumption_or_currency(self):
        self.replacement_fixture(articles=2)
        self.propose()
        before = files(self.journal)
        for changes in ({'budget_usd': 50}, {'reserved_usd': .1},
                        {'reported_usage_usd': .1}, {'currency': 'EUR'}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.propose(self.by_article[B], funding={**self.funding, **changes})
        self.assertEqual(files(self.journal), before)

    def test_cancelled_or_accepted_predecessor_cannot_acquire_distinct_successor(self):
        for status in ('cancelled', 'accepted_offline', 'held'):
            with self.subTest(status=status):
                self.replacement_fixture()
                update = self.propose()
                if status == 'cancelled': self.store.cancel(update['update_id'], 'Stop.')
                elif status == 'accepted_offline':
                    update, review = self.to_review(update)
                    self.receive(update, self.review(review)); self.accept(update)
                else:
                    update, review = self.to_review(update)
                    self.receive(update, self.review(review, verdict(score=94, passed=False)))
                policy, evidence = self.variant()
                before = files(self.journal)
                with self.assertRaises(ContractError): self.propose(scripture_policy=policy, evidence=evidence)
                self.assertEqual(files(self.journal), before)

    def race_proposals(self, choices):
        barrier = threading.Barrier(len(choices))
        def attempt(choice):
            item, policy, evidence = choice
            store = OfflinePublicationUpdateStore(self.journal, self.root)
            barrier.wait(timeout=10)
            try:
                return store.propose(self.engine, 'deu', item.task['article_id'], item.task['id'],
                    policy, evidence, self.root, self.funding)
            except ContractError as error: return error
        with ThreadPoolExecutor(max_workers=len(choices)) as executor:
            pending = [executor.submit(attempt, choice) for choice in choices]
            return [future.result(timeout=30) for future in pending]

    def test_concurrent_same_predecessor_and_identical_proposal_allocate_once(self):
        before = files(self.root)
        results = self.race_proposals([(self.item, self.item.policy, self.item.evidence)] * 4)
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(len({result['update_id'] for result in results}), 1)
        self.assertEqual(len(self.store.inspect()['events']), 1)
        self.assert_original_unchanged(before)

    def test_concurrent_different_successors_for_same_predecessor_only_one_wins(self):
        policy, evidence = self.variant()
        before = files(self.root)
        results = self.race_proposals([(self.item, self.item.policy, self.item.evidence),
                                      (self.item, policy, evidence)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(len(self.store.inspect()['events']), 1)
        self.assert_original_unchanged(before)

    def test_concurrent_pairs_cannot_oversubscribe_shared_envelope(self):
        self.replacement_fixture(articles=2)
        self.funding['budget_usd'] = .9
        before = files(self.root)
        results = self.race_proposals([(item, item.policy, item.evidence) for item in self.by_article.values()])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        inspection = self.store.inspect()
        self.assertEqual(len(inspection['events']), 1)
        self.assertLessEqual(Decimal(inspection['projected_allocations'][self.funding['id']]), Decimal('.9'))
        self.assert_original_unchanged(before)

    def test_crashes_before_and_after_every_event_replay_without_duplicate_budget_or_attempts(self):
        class Crash(RuntimeError): pass
        for operation in ('propose', 'request', 'receive', 'accept', 'cancel'):
            for boundary in ('before_commit', 'after_commit'):
                with self.subTest(operation=operation, boundary=boundary):
                    self.replacement_fixture()
                    update = None if operation == 'propose' else self.propose()
                    row = self.generation(self.request(update)) if operation == 'receive' else None
                    if operation == 'accept':
                        update, review = self.to_review(update)
                        self.receive(update, self.review(review))
                    before = files(self.root)
                    prior = self.store.inspect()
                    def fault(point):
                        if point == boundary: raise Crash(point)
                    def invoke(**kwargs):
                        if operation == 'propose': return self.propose(**kwargs)
                        if operation == 'request': return self.request(update, **kwargs)
                        if operation == 'receive': return self.receive(update, row, **kwargs)
                        if operation == 'accept': return self.accept(update, **kwargs)
                        return self.store.cancel(update['update_id'], 'Offline cancellation.', **kwargs)
                    with self.assertRaises(Crash): invoke(expected_head=prior['head'], fault=fault)
                    self.assertEqual(len(self.store.inspect()['events']),
                        len(prior['events']) + (boundary == 'after_commit'))
                    self.store = OfflinePublicationUpdateStore(self.journal, self.root)
                    invoke(expected_head=prior['head'])
                    result = self.store.inspect()
                    self.assertEqual(len(result['events']), len(prior['events']) + 1)
                    if operation != 'propose':
                        self.assertEqual(result['projected_allocations'], prior['projected_allocations'])
                    if operation in ('request', 'receive'):
                        self.assertEqual(self.snapshot(update)['generation_attempts'], 1)
                    self.assertFalse(list((self.journal / 'events').glob('.tmp-*')))
                    self.assert_original_unchanged(before)

    def test_duplicate_requests_responses_acceptance_return_copies_without_new_events(self):
        before = files(self.root)
        update = self.propose()
        self.assertEqual(self.propose()['update_id'], update['update_id'])
        request = self.request(update)
        self.assertEqual(self.request(update), request)
        request['line']['body']['model'] = 'mutated-return-value'
        actual = self.request(update)
        self.assertNotEqual(request, actual)
        row = self.generation(actual)
        self.receive(update, row)
        saved = self.store.inspect()
        self.receive(update, copy.deepcopy(row))
        self.assertEqual(self.store.inspect(), saved)
        review = self.request(update); result = self.review(review)
        self.receive(update, result); self.accept(update)
        accepted = self.store.inspect()
        self.accept(update)
        self.assertEqual(self.store.inspect(), accepted)
        saved['updates'].clear()
        self.assertTrue(self.store.inspect()['updates'])
        self.assert_original_unchanged(before)

    def test_stale_expected_head_never_appends_new_events(self):
        self.replacement_fixture(articles=2)
        empty = self.store.inspect()['head']
        first = self.propose(expected_head=empty)
        after_first = self.store.inspect()['head']
        with self.assertRaisesRegex(ContractError, '[Ss]tale'): self.propose(self.by_article[B], expected_head=empty)
        second = self.propose(self.by_article[B], expected_head=after_first)
        with self.assertRaisesRegex(ContractError, '[Ss]tale'): self.request(first, expected_head=after_first)
        current = self.store.inspect()['head']
        request = self.request(first, expected_head=current)
        with self.assertRaisesRegex(ContractError, '[Ss]tale'):
            self.receive(first, self.generation(request), expected_head=current)
        with self.assertRaisesRegex(ContractError, '[Ss]tale'):
            self.store.cancel(second['update_id'], 'Stop.', expected_head=current)
        before = files(self.journal)
        for invalid in ('missing', True, '', 'f' * 64):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                self.request(first, expected_head=invalid)
        self.assertEqual(files(self.journal), before)

    def test_final_commit_rechecks_source_human_candidate_and_publication_drift(self):
        for operation in ('propose', 'request', 'receive', 'accept'):
            for boundary in ('before_recheck', 'before_commit'):
                for mutation in ('source', 'human_history', 'candidate', 'publication'):
                    with self.subTest(operation=operation, boundary=boundary, mutation=mutation):
                        self.replacement_fixture()
                        update = None if operation == 'propose' else self.propose()
                        row = self.generation(self.request(update)) if operation == 'receive' else None
                        if operation == 'accept':
                            update, review = self.to_review(update)
                            self.receive(update, self.review(review))
                        prior = self.store.inspect()
                        def fault(point):
                            if point == boundary: self.mutate_predecessor(mutation)
                        with self.assertRaises(ContractError):
                            if operation == 'propose': self.propose(fault=fault)
                            elif operation == 'request': self.request(update, fault=fault)
                            elif operation == 'receive': self.receive(update, row, fault=fault)
                            else: self.accept(update, fault=fault)
                        self.assertEqual(self.store.inspect(), prior)
                        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), self.original_provider_calls)

    def test_malformed_tampered_reordered_and_truncated_events_fail_closed(self):
        for mutation in ('malformed_json', 'bytes', 'swap', 'missing_first', 'invalid_name'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                update = self.propose(); self.request(update)
                paths = self.events()
                if mutation == 'malformed_json': paths[0].write_bytes(b'{')
                elif mutation == 'bytes': paths[0].write_bytes(paths[0].read_bytes() + b' ')
                elif mutation == 'swap':
                    first, second = [path.read_bytes() for path in paths]
                    paths[0].write_bytes(second); paths[1].write_bytes(first)
                elif mutation == 'missing_first': paths[0].unlink()
                else: paths[0].rename(paths[0].with_name('not-an-event.json'))
                before = files(self.root)
                with self.assertRaises(ContractError): self.store.inspect()
                with self.assertRaises(ContractError): self.request(update)
                self.assert_original_unchanged(before)

    def test_rehashed_semantic_event_tampering_cannot_activate_funding_or_lower_ceiling(self):
        for mutation in ('funding', 'ceiling', 'identity', 'kind', 'extra'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                self.propose()
                path = self.events()[0]
                wrapper = json.loads(path.read_bytes())
                if mutation == 'funding': wrapper['event']['funding_authorized'] = True
                elif mutation == 'ceiling': wrapper['event']['proposal']['ceiling']['total_usd'] = '0'
                elif mutation == 'identity': wrapper['event']['update_id'] = 'f' * 64
                elif mutation == 'kind': wrapper['event']['kind'] = 'published'
                else: wrapper['event']['bypass_review'] = True
                path.unlink()
                replacement = path.with_name(f'{wrapper["sequence"]:08d}-{json_hash(wrapper)}.json')
                replacement.write_bytes(canonical(wrapper) + b'\n')
                before = files(self.root)
                with self.assertRaises(ContractError): self.store.inspect()
                self.assert_original_unchanged(before)

    def test_orphan_uncertain_batch_blocks_even_without_current_task_batch_pointer(self):
        for phase in ('propose', 'request'):
            with self.subTest(phase=phase):
                self.replacement_fixture()
                update = self.propose() if phase == 'request' else None
                self.state.save_batch({'id': 'unknown-submission', 'campaign': self.campaign['id'],
                    'tasks': [self.item.task['id']], 'status': 'submission_unknown',
                    'reserved_usd': .1, 'stage': 'translate', 'model': self.item.task['model']})
                self.assertFalse(self.item.task.get('batch'))
                before, journal = files(self.root), files(self.journal)
                with self.assertRaises(ContractError):
                    self.propose() if phase == 'propose' else self.request(update)
                self.assertEqual(files(self.journal), journal)
                self.assert_original_unchanged(before)

    def test_unarchivable_response_is_durable_hold_and_never_refunds_or_restarts(self):
        before = files(self.root)
        update = self.propose(); request = self.request(update)
        row = self.generation(request)
        row['oversized-audit'] = 'x' * 1100000
        self.receive(update, row)
        saved = self.store.inspect()
        self.assertEqual(self.value(update)['status'], 'held')
        self.assertFalse(self.snapshot(update)['replayable'])
        reopened = OfflinePublicationUpdateStore(self.journal, self.root)
        self.assertEqual(reopened.inspect(), saved)
        self.store = reopened
        with self.assertRaises(ContractError): self.request(update)
        with self.assertRaises(ContractError): self.accept(update)
        self.assertEqual(self.propose()['status'], 'held')
        self.store.cancel(update['update_id'], 'Retain the failed update allocation.')
        self.assertEqual(self.store.inspect()['projected_allocations'], saved['projected_allocations'])
        self.assert_original_unchanged(before)

    def test_lost_cancellation_tail_is_detected_by_retained_expected_head(self):
        update = self.propose()
        self.store.cancel(update['update_id'], 'This cancellation must stay recorded.')
        retained = self.store.inspect()['head']
        self.events()[-1].unlink()
        before = files(self.journal)
        with self.assertRaises(ContractError): self.store.inspect(expected_head=retained)
        with self.assertRaises(ContractError): self.request(update, expected_head=retained)
        with self.assertRaises(ContractError): self.propose(expected_head=retained)
        self.assertEqual(files(self.journal), before)

    def test_orphan_active_task_for_same_pair_blocks_proposal_and_request(self):
        for phase in ('propose', 'request'):
            for status in ('queued', 'in_batch'):
                with self.subTest(phase=phase, status=status):
                    self.replacement_fixture()
                    update = self.propose() if phase == 'request' else None
                    orphan = copy.deepcopy(self.item.task)
                    orphan.update(id='f' * 32, status=status, stage='translate',
                                  translation_attempts=0, review_attempts=0)
                    if status == 'in_batch': orphan['batch'] = 'unresolved-batch'
                    self.state.save_task(orphan)
                    before, journal = files(self.root), files(self.journal)
                    with self.assertRaises(ContractError):
                        self.propose() if phase == 'propose' else self.request(update)
                    self.assertEqual(files(self.journal), journal)
                    self.assert_original_unchanged(before)

    def test_expiry_at_acceptance_commit_cannot_authorize_update_but_history_replays(self):
        update = self.approved()
        before, prior = files(self.root), self.store.inspect()
        class MutableClock(datetime):
            expired = False
            @classmethod
            def now(cls, tz=None):
                return datetime(2100, 1, 1, tzinfo=timezone.utc) if cls.expired else datetime.now(tz)
        def fault(point):
            if point == 'before_commit': MutableClock.expired = True
        with patch('berean_translation.scripture_evidence.datetime', MutableClock):
            with self.assertRaisesRegex(ContractError, 'evidence_expired'):
                self.accept(update, fault=fault)
            self.assertEqual(self.store.inspect(), prior)
        self.assert_original_unchanged(before)

    def test_already_accepted_result_replays_after_expiry_as_offline_history_only(self):
        update = self.approved(); self.accept(update)
        before, prior = files(self.root), self.store.inspect()
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch('berean_translation.scripture_evidence.datetime', FutureClock):
            self.assertEqual(self.store.inspect(), prior)
            self.assertEqual(self.value(update)['status'], 'accepted_offline')
            self.assertFalse(self.value(update)['acceptance']['publication_authorized'])
            with self.assertRaises(ContractError): self.request(update)
        self.assert_original_unchanged(before)

    def test_unrelated_english_revision_is_allowed_but_changed_current_issue_is_not(self):
        before = self.state.read('state/source.json')
        current = copy.deepcopy(before); current['revision'] = 'c' * 40
        self.state.write('state/source.json', current)
        update = self.propose()
        self.request(update)
        self.assertEqual(self.snapshot(update)['context']['source'], self.item.source)
        self.replacement_fixture()
        current = self.state.read('state/source.json')
        current['articles'][A]['issue_id'] = 'different-issue'
        self.state.write('state/source.json', current)
        before = files(self.root)
        with self.assertRaises(ContractError): self.propose()
        self.assert_original_unchanged(before)

    def test_canonical_publication_paths_cannot_be_replaced_by_matching_copies(self):
        for key in ('html_path', 'metadata_path'):
            with self.subTest(path=key):
                self.replacement_fixture()
                record = copy.deepcopy(self.item.record)
                old = self.state.path(record['published'][key])
                replacement = old.with_name('copied-' + old.name)
                replacement.write_bytes(old.read_bytes())
                record['published'][key] = replacement.relative_to(self.root).as_posix()
                self.state.save_record(record)
                before = files(self.root)
                with self.assertRaises(ContractError): self.propose()
                self.assert_original_unchanged(before)

    def test_every_request_binds_exact_completed_predecessor_and_original_candidate(self):
        update, review = self.to_review()
        for record in self.snapshot(update)['records']:
            payload = json.loads(record['line']['body']['messages'][1]['content'])
            completed = payload['completed_publication_update']
            self.assertEqual(completed['update_id'], update['update_id'])
            self.assertEqual(completed['predecessor_candidate'], self.item.archived)
            origin = completed['origin']
            self.assertEqual(origin['predecessor_task_id'], self.item.task['id'])
            self.assertEqual(origin['candidate_sha256'], json_hash(self.item.archived))
            self.assertEqual(origin['publication_sha256'], json_hash(self.item.publication))
            self.assertEqual(origin['source_sha256'], json_hash(self.item.source))
            self.assertEqual(origin['task_sha256'], json_hash(self.item.task))
            self.assertEqual(origin['campaign_sha256'], json_hash(self.campaign))
            self.assertEqual(record['request_sha256'], json_hash(record['line']))
            self.assertEqual(record['input_bound'], len(canonical(record['line']['body'])) + 4096)
        self.assertNotEqual(review['binding']['candidate_sha256'], json_hash(self.item.archived))
        self.assertEqual(review['binding']['candidate_sha256'], json_hash(self.item.candidate))

    def test_forged_cross_update_response_and_conflicting_duplicate_cannot_rewrite_history(self):
        self.replacement_fixture(articles=2)
        first, first_review = self.to_review()
        second, second_review = self.to_review(item=self.by_article[B])
        old = self.review(first_review)
        before = files(self.journal)
        with self.assertRaises(ContractError): self.receive(second, old)
        self.assertEqual(files(self.journal), before)
        old['custom_id'] = second_review['line']['custom_id']
        self.receive(second, old)
        self.assertEqual(self.snapshot(second)['status'], 'held')
        correct = self.review(first_review)
        self.receive(first, correct)
        changed = copy.deepcopy(correct)
        changed['response']['body']['usage']['prompt_tokens'] = 201
        before = files(self.journal)
        with self.assertRaises(ContractError): self.receive(first, changed)
        self.assertEqual(files(self.journal), before)

    def test_expired_evidence_replays_archived_history_but_cannot_start_a_new_request(self):
        update = self.propose()
        before = self.store.inspect()
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch('berean_translation.scripture_evidence.datetime', FutureClock):
            self.assertEqual(self.store.inspect(), before)
            with self.assertRaisesRegex(ContractError, 'evidence_expired'): self.request(update)
            self.assertEqual(self.store.inspect(), before)

    def test_replayed_snapshots_cannot_reset_attempts_change_candidate_or_replace_predecessor(self):
        update = self.approved()
        saved = self.snapshot(update)
        self.assertEqual(OfflinePublicationUpdateCycle.restore(saved).snapshot(), saved)
        for mutation in ('attempts', 'candidate', 'predecessor', 'publication', 'record', 'status'):
            with self.subTest(mutation=mutation):
                snapshot = copy.deepcopy(saved)
                if mutation == 'attempts': snapshot['generation_attempts'] = 0
                elif mutation == 'candidate': snapshot['candidate']['title'] = 'Changed'
                elif mutation == 'predecessor':
                    snapshot['context']['publication_update']['predecessor_candidate']['title'] += '!'
                elif mutation == 'publication':
                    snapshot['context']['publication_update']['origin']['publication_sha256'] = 'f' * 64
                elif mutation == 'record': snapshot['records'].pop(0)
                else: snapshot['status'] = 'awaiting_request'
                with self.assertRaises(ContractError): OfflinePublicationUpdateCycle.restore(snapshot)

    def test_self_consistent_rehash_cannot_rewrite_frozen_predecessor_semantics(self):
        for mutation in ('record_language', 'publication_path', 'active_inventory',
                         'campaign_tasks', 'source_revision', 'publication_hash',
                         'translation_attempts', 'review_attempts', 'completed_stage'):
            with self.subTest(mutation=mutation):
                self.replacement_fixture()
                self.propose()
                path = self.events()[0]
                wrapper = json.loads(path.read_bytes())
                proposal = wrapper['event']['proposal']
                context = proposal['cycle']['context']
                if mutation == 'record_language': proposal['original_record']['language'] = 'fra'
                elif mutation == 'publication_path':
                    proposal['original_publication']['html_path'] = 'content/deu/articles/copied.html'
                elif mutation == 'active_inventory':
                    proposal['original_inventory']['tasks'][self.item.task['id']]['status'] = 'in_batch'
                elif mutation == 'campaign_tasks': proposal['original_campaign']['tasks'] = []
                elif mutation == 'source_revision': proposal['original_publication']['source_revision'] = 'f' * 40
                elif mutation == 'publication_hash': proposal['original_publication']['html_sha256'] = 'f' * 64
                else:
                    key = 'stage' if mutation == 'completed_stage' else mutation
                    proposal['original_task'][key] = 'translate' if key == 'stage' else 999
                    proposal['original_inventory']['tasks'][self.item.task['id']] = copy.deepcopy(proposal['original_task'])
                    proposal['original_inventory']['batch_tasks'][self.item.task['id']] = copy.deepcopy(proposal['original_task'])
                proposal['original_record']['published'] = copy.deepcopy(proposal['original_publication'])
                origin = proposal['origin']
                for original, digest_key in (('original_campaign', 'campaign_sha256'),
                        ('original_task', 'task_sha256'), ('original_record', 'record_sha256'),
                        ('original_publication', 'publication_sha256'),
                        ('original_inventory', 'pair_inventory_sha256')):
                    origin[digest_key] = json_hash(proposal[original])
                origin['publication_html_sha256'] = proposal['original_publication']['html_sha256']
                identity = json_hash({'origin': origin, 'policy_sha256': proposal['policy_sha256'],
                    'evidence_sha256': proposal['evidence_sha256'], 'funding': proposal['funding']})
                campaign, task = _projection(proposal['original_campaign'], proposal['original_task'],
                                             context['scripture_policy'], identity)
                processing = processing_contract(self.root, campaign, task)
                publication_update = copy.deepcopy(context['publication_update'])
                publication_update.update(update_id=identity, origin=copy.deepcopy(origin))
                proposal['cycle'] = OfflinePublicationUpdateCycle(context['source'], context['evidence'],
                    context['scripture_policy'], processing, publication_update).snapshot()
                wrapper['event']['update_id'] = identity
                path.unlink()
                replacement = path.with_name(f'{wrapper["sequence"]:08d}-{json_hash(wrapper)}.json')
                replacement.write_bytes(canonical(wrapper) + b'\n')
                before = files(self.root)
                with self.assertRaises(ContractError): self.store.inspect()
                self.assert_original_unchanged(before)

    def test_private_journal_cannot_be_inside_repository_or_redirected_into_state(self):
        before = files(self.root)
        with self.assertRaises(ContractError): OfflinePublicationUpdateStore(self.root / 'state/updates', self.root)
        with self.assertRaises(ContractError): OfflinePublicationUpdateStore(self.root, self.root)
        self.assert_original_unchanged(before)
        target = self.root / 'state/redirected-updates'; target.mkdir()
        (self.journal / 'events').rmdir()
        (self.journal / 'events').symlink_to(target, target_is_directory=True)
        before = files(self.root)
        with self.assertRaises(ContractError): self.propose()
        self.assert_original_unchanged(before)


if __name__ == '__main__':
    unittest.main()
