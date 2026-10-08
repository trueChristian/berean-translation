"""Real Engine/collector tests against disposable State and fake providers only.

These tests opt in exclusively in a temporary fixture. They never alter checked-in
runtime State, call GetBible/OpenAI, enable production, or update old publications.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from berean_translation import manual_admission
from berean_translation.common import ContractError, canonical, digest, json_hash, loads, read_json
from berean_translation.engine import Engine
from berean_translation.requests import build_request, reserve_cost
from berean_translation.scripture_association import associate
from berean_translation.scripture_component_evidence import component_policy
from berean_translation.scripture_evidence import build_evidence
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.state import TERMINAL
from support import A, B, REPO_ROOT, queue, setup
from test_scripture_component_evidence import insertion, part, omit
from test_scripture_evidence import FakeMCP, fixture

GATE = 'scripture_components_runtime_enabled'


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def review_report(*, passed=True, score=98, severity=None, complete=True):
    return {'score': score, 'passed': passed, 'findings_complete': complete,
            'findings': [] if severity is None else [{
                'severity': severity, 'location': 'title', 'source_quote': 'Faith',
                'translation_quote': 'Glaube', 'suggested_fix': 'Use Vertrauen.'}]}


def component_parts(verse, authored, kind):
    if kind == 'insert': return insertion(verse, authored)
    end = len(verse.rstrip())
    boundary = verse.index(' ')
    if kind == 'replace':
        operations = [{**part(verse, 0, boundary), 'kind': 'replace', 'id': 'a1',
                       'authored_text': authored}, part(verse, boundary, end)]
        quote = authored + verse[boundary:end]
    elif kind == 'omit':
        boundary += 1
        operations = [omit(verse, 0, boundary),
                      {'kind': 'insert', 'id': 'a1', 'at': boundary, 'authored_text': authored + ' '},
                      part(verse, boundary, end)]
        quote = authored + ' ' + verse[boundary:end]
    elif kind == 'subword_insert':
        operations = [part(verse, 0, 2),
                      {'kind': 'insert', 'id': 'a1', 'at': 2, 'authored_text': authored},
                      part(verse, 2, end)]
        quote = verse[:2] + authored + verse[2:end]
    elif kind == 'punctuation':
        boundary += 1
        operations = [part(verse, 0, boundary),
                      {'kind': 'insert', 'id': 'a1', 'at': boundary, 'authored_text': authored + ' '},
                      {'kind': 'punctuation', 'id': 'a2', 'at': boundary, 'authored_text': ': '},
                      part(verse, boundary, end)]
        quote = verse[:boundary] + authored + ' : ' + verse[boundary:end]
    else: raise AssertionError(kind)
    if end < len(verse): operations.append(omit(verse, end))
    return quote, operations


def runtime_fixture(parent, *, articles=1, budget=5, opt_in=True,
                    model='gpt-4.1-mini', review_model='gpt-4.1-mini',
                    component_kind='insert', reference='John 4:16', review_contract_version=2, ordinary_sibling=False):

    root = parent / 'repository'
    root.mkdir()
    config, state, upstream, provider, git, engine = setup(root, review_contract_version=review_contract_version)
    # Default outputs belong to the simulated collector. A takeover test changes
    # this attribution explicitly without changing global support.py behavior.
    git.bot = True
    def own_output_attribution(relative, **kwargs):
        from berean_translation.gitstore import NoHumanEditEvidence
        raise NoHumanEditEvidence('Fixture path is untracked or collector-owned')
    git.human_edit_evidence = own_output_attribution
    state.gitstore = git
    shutil.copytree(REPO_ROOT / 'data', root / 'data')
    shutil.copytree(REPO_ROOT / 'docs/third-party', root / 'docs/third-party')
    config.runtime['scripture_quotes_enabled'] = True
    config.runtime[GATE] = opt_in
    english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
    target = fixture('luther1545')['structuredContent']['data']['verses'][1]['text']
    printed, operations = component_parts(english, '[indeed]', component_kind)
    upstream.articles = upstream.articles[:articles]
    for item in upstream.articles:
        printed_quote = english.rstrip() if ordinary_sibling and item['id'] == B else printed
        upstream.contents[item['html']['repository_path']] = (
            f'<article data-article-id="{item["id"]}"><p>“{printed_quote}” ({reference}).</p>'
            f'<figure><img src="/images/articles/{item["id"]}-1.jpg" alt="A tree">'
            '<figcaption>A tree.</figcaption></figure></article>')
    upstream.rebuild()
    engine.discover()
    fake = FakeMCP()
    engine.scripture_provider = GetBibleMCP(fake)
    request = queue(state, languages='deu', budget_usd=budget, model=model, review_model=review_model)
    campaign = engine.accept_request(request)
    ledger = state.read(manual_admission.path(campaign['id']))
    manual_admission.validate(state, campaign, ledger)
    items = {}
    for entry_id, entry in ledger['entries'].items():
        if entry['status'] != 'attention': continue
        source = state.source(entry['provenance']['task'])
        scopes = associate(source['html'], source['article']['id'])[2]
        policy = component_policy(root, source, [
            {'scope_sha256': json_hash(scopes[0]), 'operations': operations}])
        evidence = build_evidence(source, 'de', policy, GetBibleMCP(fake))
        # Distinct article payloads expose accidental collector cross-routing.
        target_quote, target_operations = component_parts(target,
            '[wirklich]' if source['article']['id'] == A else '[gewiss]', component_kind)
        candidate = {'html': source['html'].replace(printed, target_quote),
                     'title': 'Glaube', 'subtitle': None, 'section': 'Lehre'}
        selections = [{'quote_id': 'q1', 'block': '/article[1]/p[1]',
                       'start': 1, 'end': 1 + len(target_quote), 'operations': target_operations}]
        items[source['article']['id']] = SimpleNamespace(entry_id=entry_id,
            entry=copy.deepcopy(entry), source=source, policy=policy, evidence=evidence,
            candidate=candidate, selections=selections)
    return SimpleNamespace(root=root, config=config, state=state, upstream=upstream,
        provider=provider, git=git, engine=engine, campaign=campaign, ledger=ledger,
        items=items, fake=fake, request=request)


class ComponentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            blocker = patch(target, side_effect=AssertionError('Network forbidden in Engine fixture'))
            blocker.start()
            self.addCleanup(blocker.stop)
        blocker = patch.object(GetBibleMCP, '_remote',
            side_effect=AssertionError('Real GetBible forbidden in Engine fixture'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.new_fixture()

    def new_fixture(self, **kwargs):
        parent = self.parent / str(len(list(self.parent.iterdir())))
        parent.mkdir()
        self.fx = runtime_fixture(parent, **kwargs)
        for key, value in vars(self.fx).items():
            setattr(self, key, value)
        self.item = self.items[A]
        return self.fx

    def accept(self, item=None, **changes):
        item = item or self.item
        return self.engine.accept_component_revision(self.campaign['id'], item.entry_id,
            changes.pop('scripture_policy', item.policy), changes.pop('evidence', item.evidence), **changes)

    def task(self, item=None):
        return self.state.read(f'state/tasks/{(item or self.item).entry_id}/task.json')

    def reopened_engine(self):
        engine = Engine(self.config, self.upstream.client, self.provider, self.git)
        engine.scripture_provider = GetBibleMCP(self.fake)
        self.engine = engine
        return engine

    def result(self, line, *, verdict=None):
        payload = loads(line['body']['messages'][1]['content'])
        binding = copy.deepcopy(payload['expected_binding'])
        if line['custom_id'].split(':')[1].startswith('review'):
            return {'binding': binding, 'review': review_report() if verdict is None else verdict}
        item = next(item for item in self.items.values() if line['custom_id'].startswith(item.entry_id + ':'))
        candidate = copy.deepcopy(item.candidate)
        if ':correct' in line['custom_id']:
            candidate['title'] = 'Vertrauen'
        return {'binding': binding, 'candidate': candidate,
                'scripture_selections': copy.deepcopy(item.selections)}

    def drive(self, responder=None, ticks=7):
        for _ in range(ticks):
            self.engine.tick(discover_source=False)
            self.provider.complete_all(responder or self.result)
            tasks = self.state.tasks()
            if tasks and all(task['status'] in TERMINAL for task in tasks):
                self.engine.prepare()  # Prove the terminal chain cannot add a request.
                break

    def rows(self, batch=None):
        batch = batch or list(self.provider.batches.values())[-1]
        return [loads(raw) for raw in self.provider.files[batch['output_file_id']].splitlines()]

    def replace_rows(self, rows, batch=None):
        batch = batch or list(self.provider.batches.values())[-1]
        self.provider.files[batch['output_file_id']] = b'\n'.join(canonical(row) for row in rows)

    def assert_no_publication(self):
        self.assertFalse(self.state.record('deu', A).get('published'))
        self.assertFalse((self.root / f'content/deu/articles/{A}.html').exists())

    def assert_frozen_originals(self):
        current = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
        self.assertEqual(manual_admission.contract(current), manual_admission.contract(self.campaign))
        self.assertEqual(self.state.read(f'state/queue/{self.campaign["id"]}.json'), self.request)
        ledger = self.state.read(manual_admission.path(self.campaign['id']))
        for item in self.items.values():
            original = item.entry
            current = ledger['entries'][item.entry_id]
            for key in ('item', 'provenance', 'provenance_sha256'):
                self.assertEqual(current[key], original[key])
            self.assertEqual({k:v for k,v in current.items() if k != 'component_revision'}, original)
            self.assertEqual(self.state.source(original['provenance']['task']), item.source)
        self.assertEqual(self.campaign['scripture_quotes']['version'], '1')

    def test_real_fixture_starts_as_a_never_paid_manual_attention_hold(self):
        self.assertEqual(self.campaign['status'], 'admission_attention')
        self.assertEqual(self.campaign['tasks'], [])
        self.assertEqual(self.campaign['reserved_usd'], 0)
        self.assertEqual(self.item.entry['status'], 'attention')
        self.assertEqual(self.item.entry['reason'], 'source_quote_annotation')
        self.assertIsNone(self.task())
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_checked_in_default_and_absent_or_false_gate_are_closed(self):
        self.assertIs(read_json(REPO_ROOT / 'config/runtime.json').get(GATE, False), False)
        for gate in ('absent', False):
            with self.subTest(gate=gate):
                self.new_fixture(opt_in=False)
                if gate == 'absent': self.config.runtime.pop(GATE, None)
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError): self.accept()
                self.assertEqual(tree_bytes(self.root), before)
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_malformed_opt_in_never_materializes_or_spends(self):
        for value in (True.__class__.__name__, 'true', 1, 0, None, {}, [], 1.0):
            with self.subTest(value=value):
                self.config.runtime[GATE] = value
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError): self.accept()
                self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_aligned_components_use_real_engine_batch_collector_and_publish_new_pair(self):
        self.accept()
        self.assertEqual(self.task()['status'], 'queued')
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
        self.drive()
        task = self.task()
        self.assertEqual(task['status'], 'complete')
        self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (2, 2))
        self.assertEqual(self.state.candidate(task), self.item.candidate)
        publication = self.state.record('deu', A)['published']
        self.assertEqual(publication['task'], task['id'])
        self.assertFalse(publication['human_reviewed'])
        self.assertIn('[wirklich]', (self.root / publication['html_path']).read_text())
        for batch in self.state.batches():
            self.assertEqual(batch['custom_ids'], [task['id'] + ':' + batch['stage']])
            payload = (self.root / f'state/batches/{batch["id"]}/input.jsonl').read_bytes()
            self.assertEqual(digest(payload), batch['payload_sha256'])
            self.assertEqual(loads(payload)['custom_id'], batch['custom_ids'][0])
        self.assert_frozen_originals()

    def test_exact_admission_replay_reuses_original_task_and_allocation(self):
        first = self.accept()
        before = tree_bytes(self.root)
        second = self.accept()
        self.assertEqual(second, first)
        self.assertEqual(tree_bytes(self.root), before)
        self.reopened_engine()
        self.assertEqual(self.accept(), first)
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual(len(self.state.tasks()), 1)
        self.assert_frozen_originals()

    def test_different_rehashed_revision_cannot_reset_the_same_hold(self):
        self.accept()
        changed = copy.deepcopy(self.item.policy)
        changed['max_component_candidate_bytes'] -= 1
        evidence = build_evidence(self.item.source, 'de', changed, GetBibleMCP(self.fake))
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept(scripture_policy=changed, evidence=evidence)
        self.assertEqual(tree_bytes(self.root), before)

    def test_current_defaults_cannot_replace_frozen_models_prompts_or_budget(self):
        frozen = copy.deepcopy(self.campaign)
        self.config.models['gpt-4.1-mini']['input_batch_usd_per_million'] = 999999
        self.config.runtime.update(default_model='gpt-6-luna', max_output_tokens=17)
        self.config.languages['deu']['tag'] = 'af'
        (self.root / 'prompts/translation.txt').write_text('Changed current prompt')
        self.accept()
        self.engine.prepare()
        line = loads(next(iter(self.provider.files.values())))
        self.assertEqual(line['body']['model'], frozen['models'][frozen['model']]['api_model'])
        self.assertTrue(line['body']['messages'][0]['content'].startswith(frozen['prompts']['translation']))
        self.assertEqual(line['body']['max_completion_tokens'], frozen['max_output_tokens'])
        self.assert_frozen_originals()

    def test_insufficient_original_envelope_cannot_reserve_only_generation(self):
        self.new_fixture(budget=.1)
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept()
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_two_revisions_compete_for_same_original_full_cycle_envelope(self):
        self.new_fixture(articles=2, budget=1)
        self.accept()
        budget = self.task()['stage_budget']
        self.assertEqual(set(budget['stages_usd']), {'translate', 'review1', 'correct', 'review2'})
        total = sum((Decimal(str(v)) for v in budget['stages_usd'].values()), Decimal(0))
        self.assertEqual(total, Decimal(str(budget['total_reserved_usd'])))
        self.assertGreater(total * 2, Decimal(str(self.campaign['budget_usd'])))
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept(self.items[B])
        self.assertEqual(tree_bytes(self.root), before)
        self.assertIsNone(self.task(self.items[B]))
        self.assert_frozen_originals()

    def test_complete_full_cycle_and_failed_final_review_never_get_a_fifth_request(self):
        for final_pass in (True, False):
            with self.subTest(final_pass=final_pass):
                self.new_fixture()
                self.accept()
                def respond(line):
                    stage = line['custom_id'].split(':')[1]
                    verdict = None
                    if stage == 'review1' or (stage == 'review2' and not final_pass):
                        verdict = review_report(passed=False, score=90, severity='major')
                    return self.result(line, verdict=verdict)
                self.drive(respond, ticks=10)
                task = self.task()
                self.assertEqual(task['status'], 'complete' if final_pass else 'not_ready')
                self.assertEqual((task['translation_attempts'], task['review_attempts']), (2, 2))
                self.assertEqual(self.provider.create_calls, 4)
                self.assertEqual([loads(self.provider.files[batch['input_file_id']])['custom_id'].split(':')[1]
                                  for batch in self.provider.batches.values()],
                                 ['translate', 'review1', 'correct', 'review2'])
                if not final_pass: self.assert_no_publication()
                self.assert_frozen_originals()

    def test_incomplete_independent_review_holds_without_correction(self):
        self.accept()
        self.drive(lambda line: self.result(line, verdict=review_report(complete=False)))
        task = self.task()
        self.assertEqual(task['status'], 'not_ready')
        self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        self.assertEqual(self.provider.create_calls, 2)
        self.assert_no_publication()

    def test_out_of_order_collector_rows_route_exact_tasks_and_candidates(self):
        self.new_fixture(articles=2)
        for item in self.items.values(): self.accept(item)
        self.drive()
        for item in self.items.values():
            task = self.task(item)
            self.assertEqual(task['status'], 'complete')
            self.assertEqual(self.state.candidate(task), item.candidate)
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        self.assertEqual(self.provider.create_calls, 2)
        self.assert_frozen_originals()

    def test_duplicate_unexpected_missing_and_cross_bound_custom_ids_fail_closed(self):
        for corruption in ('duplicate', 'unexpected', 'missing', 'cross_bound'):
            with self.subTest(corruption=corruption):
                self.new_fixture(articles=2)
                for item in self.items.values(): self.accept(item)
                self.engine.prepare()
                self.provider.complete_all(self.result)
                rows = self.rows()
                if corruption == 'duplicate': rows.append(copy.deepcopy(rows[0]))
                elif corruption == 'unexpected': rows[0]['custom_id'] = 'unrelated:translate'
                elif corruption == 'missing': rows = []
                else:
                    rows[0]['response'], rows[1]['response'] = rows[1]['response'], rows[0]['response']
                self.replace_rows(rows)
                self.engine.collect()
                self.engine.prepare()
                self.assertEqual(self.provider.create_calls, 1)
                for item in self.items.values():
                    self.assertEqual(self.task(item)['status'], 'not_ready')
                    self.assertFalse(self.state.record('deu', item.source['article']['id']).get('published'))

    def test_generation_schema_binding_provider_model_and_response_fail_closed(self):
        corruptions = ('binding', 'missing_selections', 'extra_wrapper', 'wrong_model',
                       'invalid_json', 'refusal', 'truncated', 'ambiguous_choices',
                       'float_status', 'malformed_refusal', 'missing_model')
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                row = self.rows()[0]
                body = row['response']['body']
                result = loads(body['choices'][0]['message']['content'])
                if corruption == 'binding': result['binding']['source_sha256'] = 'f' * 64
                elif corruption == 'missing_selections': del result['scripture_selections']
                elif corruption == 'extra_wrapper': result['arbitrary'] = True
                elif corruption == 'wrong_model': body['model'] = 'unrequested-model'
                elif corruption == 'invalid_json': body['choices'][0]['message']['content'] = '{'
                elif corruption == 'refusal': body['choices'][0]['message']['refusal'] = 'Refused.'
                elif corruption == 'truncated': body['choices'][0]['finish_reason'] = 'length'
                elif corruption == 'ambiguous_choices': body['choices'] *= 2
                elif corruption == 'float_status': row['response']['status_code'] = 200.0
                elif corruption == 'malformed_refusal': body['choices'][0]['message']['refusal'] = False
                else: del body['model']
                if corruption in ('binding', 'missing_selections', 'extra_wrapper'):
                    body['choices'][0]['message']['content'] = canonical(result).decode()
                self.replace_rows([row])
                self.engine.collect()
                self.engine.prepare()
                task = self.task()
                self.assertEqual(task['status'], 'not_ready')
                self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 0))
                self.assertEqual(self.provider.create_calls, 1)
                self.assert_no_publication()

    def test_bad_components_and_unsupported_morphology_hold_without_paid_correction(self):
        for corruption in ('canonical_words', 'authored_brackets', 'component_identity', 'morphology'):
            with self.subTest(corruption=corruption):
                self.new_fixture()
                self.accept()
                def respond(line):
                    result = self.result(line)
                    if corruption == 'canonical_words':
                        result['candidate']['html'] = result['candidate']['html'].replace('Mann', 'Frau')
                    elif corruption == 'authored_brackets':
                        result['candidate']['html'] = result['candidate']['html'].replace('[wirklich]', 'wirklich')
                    elif corruption == 'component_identity':
                        result['scripture_selections'][0]['operations'][1]['id'] = 'a2'
                    else:
                        result['scripture_selections'][0]['operations'][1]['kind'] = 'morphology'
                    return result
                self.drive(respond)
                task = self.task()
                self.assertEqual(task['status'], 'not_ready')
                self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 0))
                self.assertEqual(self.provider.create_calls, 1)
                self.assert_no_publication()

    def test_review_binding_schema_and_actual_model_must_match_exact_candidate(self):
        for corruption in ('candidate_hash', 'missing_complete', 'extra_review', 'boolean_score',
                           'wrong_model', 'incomplete_finding'):
            with self.subTest(corruption=corruption):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                row = self.rows()[0]
                body = row['response']['body']
                result = loads(body['choices'][0]['message']['content'])
                if corruption == 'candidate_hash': result['binding']['candidate_sha256'] = 'a' * 64
                elif corruption == 'missing_complete': del result['review']['findings_complete']
                elif corruption == 'extra_review': result['review']['approved'] = True
                elif corruption == 'boolean_score': result['review']['score'] = True
                elif corruption == 'wrong_model': body['model'] = 'unrequested-review-model'
                else: result['review']['findings'] = [{'severity': 'major'}]
                body['choices'][0]['message']['content'] = canonical(result).decode()
                self.replace_rows([row])
                self.engine.collect()
                self.engine.prepare()
                self.assertEqual(self.task()['status'], 'not_ready')
                self.assertEqual(self.provider.create_calls, 2)
                self.assert_no_publication()

    def change_guard(self, kind):
        if kind == 'source':
            observed = self.state.read('state/source.json')
            observed['articles'][A]['translation_key'] = 'f' * 64
            self.state.write('state/source.json', observed)
        elif kind == 'human_file':
            path = self.root / f'content/deu/articles/{A}.html'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Human author owns every byte.')
        elif kind == 'human_history':
            record = self.state.record('deu', A)
            record['history'].append({'event': 'human_review', 'at': '2026-10-01T00:00:00Z'})
            self.state.save_record(record)
        elif kind == 'cancel':
            campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
            campaign['cancel_requested'] = True
            self.state.save_campaign(campaign)
        else: raise AssertionError(kind)

    def test_source_human_and_cancellation_drift_block_admission(self):
        for kind in ('source', 'human_file', 'human_history', 'cancel'):
            with self.subTest(kind=kind):
                self.new_fixture()
                self.change_guard(kind)
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError): self.accept()
                self.assertEqual(tree_bytes(self.root), before)
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_source_human_and_cancellation_drift_block_materialized_paid_work(self):
        for kind in ('source', 'human_file', 'human_history', 'cancel'):
            with self.subTest(kind=kind):
                self.new_fixture()
                self.accept()
                self.change_guard(kind)
                try: self.engine.prepare()
                except ContractError: pass
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assert_no_publication_unless_human_file(kind)

    def assert_no_publication_unless_human_file(self, kind):
        self.assertFalse(self.state.record('deu', A).get('published'))
        path = self.root / f'content/deu/articles/{A}.html'
        if kind == 'human_file': self.assertEqual(path.read_text(), 'Human author owns every byte.')
        else: self.assertFalse(path.exists())

    def test_late_source_human_or_cancel_drift_cannot_publish_received_result(self):
        for kind in ('source', 'human_file', 'human_history', 'cancel'):
            with self.subTest(kind=kind):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.change_guard(kind)
                try: self.engine.collect()
                except ContractError: pass
                self.assert_no_publication_unless_human_file(kind)
                self.assertEqual(self.provider.create_calls, 2)

    def test_disabled_gate_blocks_already_materialized_request_prepare_and_publish(self):
        self.accept()
        self.config.runtime[GATE] = False
        with self.assertRaises(ContractError): build_request(self.config, self.state, self.task())
        try: self.engine.prepare()
        except ContractError: pass
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
        with self.assertRaises(ContractError): self.engine.publish(self.task())
        self.assert_no_publication()

    def test_unknown_create_before_or_after_remote_success_never_resubmits(self):
        for failure in ('before', 'after'):
            with self.subTest(failure=failure):
                self.new_fixture()
                self.accept()
                self.provider.raise_create = failure
                self.engine.prepare()
                batch = self.state.batches()[0]
                self.assertEqual(batch['status'], 'submission_unknown')
                reserved = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd']
                self.provider.raise_create = None
                self.reopened_engine()
                for _ in range(3): self.engine.collect()
                batch = self.state.batches()[0]
                self.assertEqual(batch['status'], 'submission_unknown' if failure == 'before' else 'submitted')
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 1))
                self.assertEqual(self.task()['translation_attempts'], 1)
                self.assertEqual(self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd'], reserved)
                self.assert_no_publication()

    def test_upload_retry_does_not_reallocate_or_increment_attempts(self):
        self.accept()
        self.provider.raise_upload = True
        self.engine.prepare()
        reserved = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd']
        self.assertEqual(self.provider.create_calls, 0)
        self.provider.raise_upload = False
        self.reopened_engine().collect()
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (2, 1))
        self.assertEqual(self.task()['translation_attempts'], 1)
        self.assertEqual(self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd'], reserved)

    def test_checkpoint_crashes_preserve_upload_intent_and_unknown_create_safety(self):
        for checkpoint in ('reserve task identities', 'persist OpenAI input file',
                           'record batch submission intent', 'persist OpenAI batch identity'):
            with self.subTest(checkpoint=checkpoint):
                self.new_fixture()
                self.accept()
                original = self.git.checkpoint
                def crash(message):
                    original(message)
                    if checkpoint in message: raise RuntimeError('Simulated lost process after durable checkpoint')
                with patch.object(self.git, 'checkpoint', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.prepare()
                uploads, creates = self.provider.upload_calls, self.provider.create_calls
                self.reopened_engine().collect()
                self.assertEqual(self.task()['translation_attempts'], 1)
                self.assertEqual(len(self.state.batches()), 1)
                if checkpoint == 'record batch submission intent':
                    self.assertEqual(self.provider.create_calls, 0)
                    self.assertEqual(self.state.batches()[0]['status'], 'submission_unknown')
                else:
                    self.assertEqual(self.provider.create_calls, 1)
                self.assertEqual(self.provider.upload_calls, uploads if uploads else 1)
                if creates: self.assertEqual(self.provider.create_calls, creates)
                self.assert_no_publication()

    def test_result_replay_never_reaccounts_usage_or_appends_history(self):
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        generation = self.rows()[0]
        self.engine.collect()
        after_generation = tree_bytes(self.root)
        self.engine.receive(self.task(), generation)
        self.assertEqual(tree_bytes(self.root), after_generation)
        self.engine.prepare()
        self.provider.complete_all(self.result)
        review = self.rows()[0]
        self.engine.collect()
        after_review = tree_bytes(self.root)
        self.engine.receive(self.task(), review)
        self.engine.collect()
        self.assertEqual(tree_bytes(self.root), after_review)
        self.assertEqual(self.task()['status'], 'complete')

    def test_existing_publication_is_never_reopened_as_v1_manual_review(self):
        self.accept()
        self.drive()
        original = tree_bytes(self.root / 'content')
        self.config.runtime['scripture_quotes_enabled'] = False
        self.engine.accept_request(queue(self.state, 'later-review', operation='review', languages='deu'))
        self.drive()
        self.assertEqual(len(self.state.tasks()), 1)
        self.assertEqual(self.provider.create_calls, 2)
        self.assertEqual(tree_bytes(self.root / 'content'), original)

    def traced_writes(self, operation):
        writes = []
        original = self.engine.state.write
        def record(path, value):
            writes.append(path)
            return original(path, value)
        with patch.object(self.engine.state, 'write', side_effect=record): operation()
        return writes

    def crash_on_write(self, operation, index, timing):
        original = self.engine.state.write
        calls = 0
        def write(path, value):
            nonlocal calls
            calls += 1
            if calls == index and timing == 'before':
                raise RuntimeError(f'Simulated interruption before {path}')
            result = original(path, value)
            if calls == index and timing == 'after':
                raise RuntimeError(f'Simulated interruption after {path}')
            return result
        with patch.object(self.engine.state, 'write', side_effect=write):
            with self.assertRaises(RuntimeError): operation()
        self.assertEqual(calls, index)

    def test_every_admission_write_crash_reconciles_without_revising_original_history(self):
        writes = self.traced_writes(self.accept)
        self.assertTrue(writes)
        for index, path in enumerate(writes, 1):
            for timing in ('before', 'after'):
                with self.subTest(index=index, path=path, timing=timing):
                    self.new_fixture()
                    self.crash_on_write(self.accept, index, timing)
                    self.reopened_engine()
                    self.accept()
                    admitted = tree_bytes(self.root)
                    self.accept()
                    self.assertEqual(tree_bytes(self.root), admitted)
                    self.assertEqual(len(self.state.tasks()), 1)
                    self.assertEqual(self.task()['id'], self.item.entry_id)
                    self.assertEqual((self.task()['translation_attempts'], self.task()['review_attempts']), (0, 0))
                    self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                    self.assert_frozen_originals()

    def test_every_prepare_write_crash_never_duplicates_a_generation_or_reservation(self):
        self.accept()
        writes = self.traced_writes(self.engine.prepare)
        self.assertTrue(writes)
        for index, path in enumerate(writes, 1):
            for timing in ('before', 'after'):
                with self.subTest(index=index, path=path, timing=timing):
                    self.new_fixture()
                    self.accept()
                    self.crash_on_write(self.engine.prepare, index, timing)
                    self.reopened_engine()
                    self.engine.collect()
                    self.engine.prepare()
                    self.engine.collect()
                    self.assertLessEqual(self.provider.create_calls, 1)
                    self.assertEqual(self.task()['translation_attempts'], 1)
                    self.assertEqual(len(self.state.batches()), 1)
                    batch = self.state.batches()[0]
                    campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
                    self.assertEqual(Decimal(str(campaign['reserved_usd'])), Decimal(str(batch['reserved_usd'])))
                    self.assertEqual(batch['tasks'], [self.item.entry_id])
                    self.assertIn(batch['status'], ('submitted', 'submission_unknown'))
                    self.assert_no_publication()
                    self.assert_frozen_originals()

    def test_every_generation_result_write_crash_replays_once_then_reviews(self):
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        writes = self.traced_writes(self.engine.collect)
        self.assertTrue(writes)
        for index, path in enumerate(writes, 1):
            for timing in ('before', 'after'):
                with self.subTest(index=index, path=path, timing=timing):
                    self.new_fixture()
                    self.accept()
                    self.engine.prepare()
                    self.provider.complete_all(self.result)
                    self.crash_on_write(self.engine.collect, index, timing)
                    self.reopened_engine()
                    self.engine.collect()
                    self.drive()
                    task = self.task()
                    self.assertEqual(task['status'], 'complete')
                    self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
                    self.assertEqual(self.provider.create_calls, 2)
                    history = self.state.record('deu', A)['history']
                    self.assertEqual(sum(row['event'] == 'complete' for row in history), 1)
                    self.assert_frozen_originals()

    def test_every_final_result_write_crash_replays_publication_once(self):
        def review_ready():
            self.accept()
            self.engine.prepare()
            self.provider.complete_all(self.result)
            self.engine.collect()
            self.engine.prepare()
            self.provider.complete_all(self.result)
        review_ready()
        writes = self.traced_writes(self.engine.collect)
        self.assertTrue(writes)
        for index, path in enumerate(writes, 1):
            for timing in ('before', 'after'):
                with self.subTest(index=index, path=path, timing=timing):
                    self.new_fixture()
                    review_ready()
                    self.crash_on_write(self.engine.collect, index, timing)
                    self.reopened_engine()
                    self.engine.collect()
                    self.drive()
                    task = self.task()
                    self.assertEqual(task['status'], 'complete')
                    self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
                    self.assertEqual(self.provider.create_calls, 2)
                    record = self.state.record('deu', A)
                    self.assertEqual(record['published']['task'], task['id'])
                    self.assertEqual(sum(row['event'] == 'complete' for row in record['history']), 1)
                    self.assert_frozen_originals()

    def test_complete_supported_components_pass_engine_with_exact_target_provenance(self):
        for kind in ('replace', 'omit', 'subword_insert', 'punctuation'):
            with self.subTest(kind=kind):
                self.new_fixture(component_kind=kind)
                self.accept()
                self.drive()
                task = self.task()
                self.assertEqual(task['status'], 'complete')
                self.assertEqual(self.state.candidate(task), self.item.candidate)
                self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
                self.assertEqual(self.provider.create_calls, 2)
                self.assert_frozen_originals()

    def test_partial_verse_marker_survives_engine_without_changing_lookup_identity(self):
        self.new_fixture(reference='John 4:16a')
        self.accept()
        self.drive()
        self.assertEqual(self.task()['status'], 'complete')
        self.assertIn('John 4:16a', self.state.candidate(self.task())['html'])
        quote = self.item.evidence['quotes'][0]
        self.assertEqual(quote['component_reference']['lookup_reference'], 'John 4:16')
        self.assertEqual(quote['component_reference']['partial_marker'], 'a')
        self.assert_frozen_originals()

    def test_prepared_payload_cannot_rehash_its_way_around_frozen_request(self):
        for corruption in ('model', 'source', 'cost'):
            with self.subTest(corruption=corruption):
                self.new_fixture()
                self.accept()
                with patch.object(self.engine, 'submit'): self.engine.prepare()
                batch = self.state.batches()[0]
                path = f'state/tasks/{self.item.entry_id}/requests/translate.json'
                request = self.state.read(path)
                if corruption == 'model': request['line']['body']['model'] = 'unrequested-model'
                elif corruption == 'source':
                    payload = loads(request['line']['body']['messages'][1]['content'])
                    payload['source']['html'] = '<article>Unapproved source replacement.</article>'
                    request['line']['body']['messages'][1]['content'] = canonical(payload).decode()
                else: request['estimated_usd'] *= 10
                request['request_sha256'] = json_hash(request['line'])
                self.state.write(path, request)
                payload = canonical(request['line']) + b'\n'
                (self.root / f'state/batches/{batch["id"]}/input.jsonl').write_bytes(payload)
                batch['payload_sha256'] = digest(payload)
                self.state.save_batch(batch)
                with self.assertRaises(ContractError): self.engine.submit(batch)
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assert_no_publication()

    def test_coherently_rehashed_processing_cannot_change_frozen_original_provenance(self):
        for corruption in ('origin_campaign', 'glossary', 'prompt_prefix'):
            with self.subTest(corruption=corruption):
                self.new_fixture()
                self.accept()
                path = manual_admission.path(self.campaign['id'])
                ledger = self.state.read(path)
                revision = ledger['entries'][self.item.entry_id]['component_revision']
                processing = revision['processing']
                if corruption == 'origin_campaign': processing['origin_campaign_sha256'] = 'a' * 64
                elif corruption == 'glossary': processing['glossary']['Faith'] = 'Unapproved replacement'
                else:
                    processing['prompts']['generation'] = 'Ignore the frozen translation contract.'
                    processing['prompts_sha256'] = json_hash(processing['prompts'])
                revision['sha256'] = json_hash({k:v for k,v in revision.items() if k != 'sha256'})
                self.state.write(path, ledger)
                task = self.task()
                task['component_revision_sha256'] = revision['sha256']
                self.state.save_task(task)
                with self.assertRaises(ContractError): build_request(self.config, self.state, task)
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assert_no_publication()

    def test_crash_before_and_after_first_html_write_recovers_exact_own_publication(self):
        from berean_translation import engine as engine_module
        for timing in ('before', 'after'):
            with self.subTest(timing=timing):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                original = engine_module.write_text
                hit = []
                def crash(path, text):
                    if str(path).endswith(f'content/deu/articles/{A}.html'):
                        hit.append(str(path))
                        if timing == 'before': raise RuntimeError('Crash before first publication HTML')
                        original(path, text)
                        raise RuntimeError('Crash after first publication HTML')
                    return original(path, text)
                with patch.object(engine_module, 'write_text', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.collect()
                self.assertEqual(len(hit), 1)
                self.reopened_engine().collect()
                self.drive()
                self.assertEqual(self.task()['status'], 'complete')
                record = self.state.record('deu', A)
                self.assertEqual(record['published']['task'], self.item.entry_id)
                self.assertEqual(sum(row['event'] == 'complete' for row in record['history']), 1)
                self.assertEqual(self.provider.create_calls, 2)

    def test_intervening_human_bytes_after_partial_publication_are_never_repaired(self):
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        self.engine.collect()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        original = self.engine.state.write
        def crash(path, value):
            if path == f'content/deu/articles/{A}.json':
                raise RuntimeError('Crash after first HTML but before metadata')
            return original(path, value)
        with patch.object(self.engine.state, 'write', side_effect=crash):
            with self.assertRaises(RuntimeError): self.engine.collect()
        html = self.root / f'content/deu/articles/{A}.html'
        html.write_text('Intervening human editorial content must stay exact.')
        self.reopened_engine()
        try: self.engine.collect()
        except ContractError: pass
        self.assertEqual(html.read_text(), 'Intervening human editorial content must stay exact.')
        self.assertFalse(self.state.record('deu', A).get('published'))
        self.assertNotEqual(self.task()['status'], 'complete')
        self.assertEqual(self.provider.create_calls, 2)

    def test_cancel_after_partial_materialization_retains_auditable_unpaid_task(self):
        original = self.engine.state.save_task
        def crash(task):
            original(task)
            raise RuntimeError('Crash after task materialization before pair/campaign links')
        with patch.object(self.engine.state, 'save_task', side_effect=crash):
            with self.assertRaises(RuntimeError): self.accept()
        self.assertIsNotNone(self.task())
        self.reopened_engine().cancel_campaign(self.campaign['id'])
        task = self.task()
        self.assertEqual(task['status'], 'cancelled')
        campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
        self.assertIn(task['id'], campaign['tasks'])
        history = self.state.record('deu', A)['history']
        self.assertIn(manual_admission.requested_event(task), history)
        self.assertEqual(sum(row['event'] == 'cancelled' for row in history), 1)
        self.assertEqual((task['translation_attempts'], task['review_attempts']), (0, 0))
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_legacy_incomplete_review_contract_cannot_be_silently_upgraded(self):
        self.new_fixture(review_contract_version=None)
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept()
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_failed_component_stage_does_not_recycle_its_full_cycle_earmark(self):
        self.new_fixture(articles=2, budget=1)
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(lambda line: None)
        self.engine.collect()
        self.assertEqual(self.task()['status'], 'not_ready')
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept(self.items[B])
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual(self.provider.create_calls, 1)
        self.assert_no_publication()

    def test_expired_evidence_blocks_unsubmitted_work_but_never_retries_unknown_create(self):
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None): return cls(2200, 1, 1, tzinfo=timezone.utc)
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                self.new_fixture()
                self.accept()
                if prepared:
                    with patch.object(self.engine, 'submit'): self.engine.prepare()
                with patch('berean_translation.scripture_evidence.datetime', FutureClock):
                    self.engine.collect()
                    self.engine.prepare()
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assert_no_publication()
        self.new_fixture()
        self.accept()
        self.provider.raise_create = 'after'
        self.engine.prepare()
        self.provider.raise_create = None
        with patch('berean_translation.scripture_evidence.datetime', FutureClock):
            self.reopened_engine().collect()
        self.assertEqual(self.state.batches()[0]['status'], 'submitted')
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 1))
        self.assertEqual(self.task()['translation_attempts'], 1)

    def test_consumed_stage_replay_rejects_malformed_response_envelope(self):
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        original = self.rows()[0]
        self.engine.collect()
        for corruption in ('false_refusal', 'float_http_status', 'extra_choice'):
            with self.subTest(corruption=corruption):
                row = copy.deepcopy(original)
                if corruption == 'false_refusal': row['response']['body']['choices'][0]['message']['refusal'] = False
                elif corruption == 'float_http_status': row['response']['status_code'] = 200.0
                else: row['response']['body']['choices'] *= 2
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError): self.engine.receive(self.task(), row)
                self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual(self.provider.create_calls, 1)

    def test_full_cycle_reservation_uses_each_original_models_maximum_frozen_stage_cost(self):
        self.new_fixture(model='gpt-4.1-nano', review_model='gpt-4.1')
        self.accept()
        task = self.task()
        for stage in ('translate', 'review1', 'correct', 'review2'):
            model = self.campaign['models'][self.campaign['review_model'] if stage.startswith('review') else self.campaign['model']]
            output = min(self.campaign['review_output_tokens'] if stage.startswith('review') else self.campaign['max_output_tokens'],
                         model['max_output_tokens'])
            amount = reserve_cost(model, model['context_tokens'] - output, output)
            self.assertEqual(task['stage_budget']['stages_usd'][stage], amount)
        self.assert_frozen_originals()

    def test_existing_ordinary_reservation_competes_with_component_cycle_in_original_campaign(self):
        self.new_fixture(articles=2, budget=.87, ordinary_sibling=True)
        self.engine.prepare()
        self.assertEqual(self.provider.create_calls, 1)
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept()
        self.assertEqual(tree_bytes(self.root), before)
        self.assertIsNone(self.task())
        self.assert_frozen_originals()

    def test_ordinary_sibling_cannot_spend_component_cycles_earmarked_headroom(self):
        self.new_fixture(articles=2, budget=.87, ordinary_sibling=True)
        self.accept()
        self.engine.prepare()
        other = next(task for task in self.state.tasks() if task['article_id'] == B)
        self.assertEqual(other['status'], 'budget_blocked')
        self.assertEqual(other['translation_attempts'], 0)
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(self.state.batches()[0]['tasks'], [self.item.entry_id])
        self.drive()
        self.assertEqual(self.task()['status'], 'complete')
        self.assertEqual(self.provider.create_calls, 2)
        self.assert_frozen_originals()

    def test_rejected_review_result_cannot_be_rewritten_into_publication_authority(self):
        for correction_allowed in (False, True):
            with self.subTest(correction_allowed=correction_allowed):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                rejected = review_report(passed=False, score=80,
                    severity='major' if correction_allowed else None)
                self.provider.complete_all(lambda line: self.result(line, verdict=rejected))
                self.engine.collect()
                self.assertEqual(self.task()['status'], 'queued' if correction_allowed else 'not_ready')
                path = f'state/tasks/{self.item.entry_id}/results/review1.json'
                archived = self.state.read(path)
                archived['result']['review'] = review_report()
                self.state.write(path, archived)
                with self.assertRaises(ContractError): self.engine.publish(self.task())
                self.assert_no_publication()
                self.assertEqual(self.provider.create_calls, 2)

    def test_source_cancel_and_gate_drift_at_upload_or_intent_checkpoint_prevent_create(self):
        for checkpoint in ('persist OpenAI input file', 'record batch submission intent'):
            for kind in ('source', 'cancel', 'human_history', 'gate'):
                with self.subTest(checkpoint=checkpoint, kind=kind):
                    self.new_fixture()
                    self.accept()
                    original = self.git.checkpoint
                    changed = []
                    def update(message):
                        original(message)
                        if checkpoint in message and not changed:
                            changed.append(True)
                            if kind == 'gate': self.config.runtime[GATE] = False
                            else: self.change_guard(kind)
                    with patch.object(self.git, 'checkpoint', side_effect=update):
                        try: self.engine.prepare()
                        except ContractError: pass
                    self.assertTrue(changed)
                    self.assertEqual(self.provider.create_calls, 0)
                    self.assertEqual(self.task()['translation_attempts'], 1)
                    self.assert_no_publication()
                    # A later collector cannot turn a possibly-started intent
                    # back into a fresh billable request after a guard changed.
                    try: self.reopened_engine().collect()
                    except ContractError: pass
                    self.assertEqual(self.provider.create_calls, 0)

    def test_new_component_publication_passes_repository_validation_and_projection(self):
        from berean_translation.validation import validate_repository
        self.accept()
        self.drive()
        report = validate_repository(self.config)
        self.assertEqual(report['published'], 1)
        self.assertEqual(report['ready'], 1)
        projection = self.state.projection(self.config)['articles']
        self.assertEqual([(row['id'], row['language']) for row in projection], [(A, 'deu')])
        self.assertFalse(projection[0]['human_reviewed'])

    def test_cached_binding_only_substitution_cannot_override_submitted_payload(self):
        self.accept()
        self.engine.prepare()
        path = f'state/tasks/{self.item.entry_id}/requests/translate.json'
        request = self.state.read(path)
        request['binding']['source_sha256'] = 'f' * 64
        self.state.write(path, request)
        def respond(line):
            result = self.result(line)
            result['binding'] = copy.deepcopy(request['binding'])
            return result
        self.provider.complete_all(respond)
        self.engine.collect()
        self.assertEqual(self.task()['status'], 'not_ready')
        self.assertEqual(self.task()['review_attempts'], 0)
        self.assertEqual(self.provider.create_calls, 1)
        self.assert_no_publication()

    def test_cached_estimated_cost_only_cannot_free_funds_after_submission(self):
        from berean_translation.scripture_component_runtime import committed_total
        self.new_fixture(articles=2, budget=1)
        self.accept()
        self.engine.prepare()
        path = f'state/tasks/{self.item.entry_id}/requests/translate.json'
        request = self.state.read(path)
        request['estimated_usd'] = self.task()['stage_budget']['total_reserved_usd']
        self.state.write(path, request)
        campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): committed_total(self.state, campaign)
        with self.assertRaises(ContractError): self.accept(self.items[B])
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual(self.provider.create_calls, 1)
        self.assertIsNone(self.task(self.items[B]))

    def test_oversized_complete_review_findings_hold_without_truncation_or_publication(self):
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        self.engine.collect()
        self.engine.prepare()
        verdict = review_report(severity='minor')
        verdict['findings'][0]['suggested_fix'] = 'x' * 40000
        self.provider.complete_all(lambda line: self.result(line, verdict=verdict))
        self.engine.collect()
        self.assertEqual(self.task()['status'], 'not_ready')
        self.assertEqual(self.provider.create_calls, 2)
        self.assert_no_publication()
        path = f'state/tasks/{self.item.entry_id}/results/review1.json'
        self.assertEqual(self.state.read(path)['result']['review'], verdict)

    def test_attempt_counter_changes_cannot_replace_prepared_stage_history(self):
        for stage in ('translate', 'review1'):
            for changed_count in (0, 2):
                with self.subTest(stage=stage, changed_count=changed_count):
                    self.new_fixture()
                    self.accept()
                    self.engine.prepare()
                    if stage == 'review1':
                        self.provider.complete_all(self.result)
                        self.engine.collect()
                        self.engine.prepare()
                    task = self.task()
                    counter = 'review_attempts' if stage == 'review1' else 'translation_attempts'
                    self.assertEqual(task[counter], 1)
                    task[counter] = changed_count
                    self.state.save_task(task)
                    self.provider.complete_all(self.result)
                    before_calls = self.provider.create_calls
                    try: self.engine.collect()
                    except ContractError: pass
                    self.assertEqual(self.task()['stage'], stage)
                    self.assert_no_publication()
                    try: self.engine.prepare()
                    except ContractError: pass
                    self.assertEqual(self.provider.create_calls, before_calls)

    def test_forged_later_stage_holds_before_freezing_request_or_reserving_work(self):
        for forged_stage in ('correct', 'review2'):
            with self.subTest(forged_stage=forged_stage):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                task = self.task()
                self.assertEqual(task['stage'], 'review1')
                task['stage'] = forged_stage
                if forged_stage == 'correct': task['findings'] = review_report(severity='major')['findings']
                self.state.save_task(task)
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError): build_request(self.config, self.state, self.task())
                self.assertEqual(tree_bytes(self.root), before)
                before_campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
                try: self.engine.prepare()
                except ContractError: pass
                self.assertEqual(len(self.state.batches()), 1)
                self.assertEqual((self.task()['translation_attempts'], self.task()['review_attempts']), (1, 0))
                self.assertEqual(self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd'],
                                 before_campaign['reserved_usd'])
                self.assertEqual(self.provider.create_calls, 1)
                self.assert_no_publication()

    def test_changed_correction_findings_hold_before_freezing_or_reservation(self):
        self.accept()
        self.engine.prepare()
        self.provider.complete_all(self.result)
        self.engine.collect()
        self.engine.prepare()
        rejected = review_report(passed=False, score=80, severity='major')
        self.provider.complete_all(lambda line: self.result(line, verdict=rejected))
        self.engine.collect()
        task = self.task()
        self.assertEqual(task['stage'], 'correct')
        task['findings'][0]['suggested_fix'] = 'A replacement instruction absent from the independent review.'
        self.state.save_task(task)
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): build_request(self.config, self.state, self.task())
        self.assertEqual(tree_bytes(self.root), before)
        before_campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
        try: self.engine.prepare()
        except ContractError: pass
        self.assertEqual(len(self.state.batches()), 2)
        self.assertEqual((self.task()['translation_attempts'], self.task()['review_attempts']), (1, 1))
        self.assertEqual(self.state.read(f'state/campaigns/{self.campaign["id"]}.json')['reserved_usd'],
                         before_campaign['reserved_usd'])
        self.assertEqual(self.provider.create_calls, 2)
        self.assert_no_publication()

    def test_cancel_after_partial_batch_reservation_retains_exact_unpaid_allocation(self):
        self.accept()
        with patch.object(self.engine, 'submit'):
            writes = self.traced_writes(self.engine.prepare)
        self.assertTrue(writes)
        for index, path in enumerate(writes, 1):
            for timing in ('before', 'after'):
                with self.subTest(index=index, path=path, timing=timing):
                    self.new_fixture()
                    self.accept()
                    with patch.object(self.engine, 'submit'):
                        self.crash_on_write(self.engine.prepare, index, timing)
                    prepared = bool(self.state.batches())
                    self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                    self.reopened_engine().cancel_campaign(self.campaign['id'])
                    task = self.task()
                    campaign = self.state.read(f'state/campaigns/{self.campaign["id"]}.json')
                    self.assertEqual(task['status'], 'cancelled')
                    self.assertEqual((task['translation_attempts'], task['review_attempts']),
                                     (1 if prepared else 0, 0))
                    if prepared:
                        batch = self.state.batches()[0]
                        self.assertEqual(batch['status'], 'cancelled_before_submission')
                        self.assertGreater(campaign['reserved_usd'], 0)
                        self.assertEqual(campaign['reserved_usd'], batch['reserved_usd'])
                    else:
                        self.assertEqual(campaign['reserved_usd'], 0)
                    self.assertEqual(sum(row['event'] == 'cancelled'
                                         for row in self.state.record('deu', A)['history']), 1)
                    self.engine.collect()
                    self.engine.prepare()
                    self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                    self.assert_no_publication()
                    self.assert_frozen_originals()

    def test_committed_human_takeover_of_identical_partial_outputs_blocks_ai_publication(self):
        for output_boundary in ('html', 'metadata'):
            with self.subTest(output_boundary=output_boundary):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                original = self.engine.state.write
                def crash(path, value):
                    is_boundary = (path == f'content/deu/articles/{A}.json'
                                   if output_boundary == 'html' else
                                   path == f'state/records/deu/{A}.json' and value.get('published'))
                    if is_boundary: raise RuntimeError('Crash after partial publication output')
                    return original(path, value)
                with patch.object(self.engine.state, 'write', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.collect()
                before = tree_bytes(self.root / 'content')
                self.assertTrue(before)
                # A real local-only Git commit proves ownership even though
                # every HTML byte still equals the collector's output intent.
                from berean_translation.gitstore import GitStore
                real_git = GitStore(self.root, publish=False)
                real_git.git('init', '-q')
                relative = f'content/deu/articles/{A}.html' if output_boundary == 'html' else f'content/deu/articles/{A}.json'
                real_git.git('add', '--', relative)
                real_git.git('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                    '-c', 'user.name=Human Reviewer', '-c', 'user.email=reviewer@example.test',
                    'commit', '-qm', 'Human owns this unchanged partial output')
                self.assertEqual(real_git.human_edit_evidence(relative)['author'], 'Human Reviewer')
                self.git = real_git
                try: self.reopened_engine().collect()
                except ContractError: pass
                self.assertEqual(tree_bytes(self.root / 'content'), before)
                self.assertFalse(self.state.record('deu', A).get('published'))
                self.assertNotEqual(self.task()['status'], 'complete')
                self.assertEqual(self.provider.create_calls, 2)

    def test_unexpected_human_attribution_lookup_failures_hold_partial_publication(self):
        for failure in (ContractError('Attribution contract failed'), OSError('Git read failed'),
                        RuntimeError('Unexpected attribution failure')):
            with self.subTest(failure=type(failure).__name__):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                original = self.engine.state.write
                def crash(path, value):
                    if path == f'content/deu/articles/{A}.json':
                        raise RuntimeError('Crash after own HTML write')
                    return original(path, value)
                with patch.object(self.engine.state, 'write', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.collect()
                before = tree_bytes(self.root / 'content')
                with patch.object(self.git, 'human_edit_evidence', side_effect=failure):
                    try: self.reopened_engine().collect()
                    except (ContractError, OSError, RuntimeError): pass
                self.assertEqual(tree_bytes(self.root / 'content'), before)
                self.assertFalse(self.state.record('deu', A).get('published'))
                self.assertNotEqual(self.task()['status'], 'complete')
                self.assertEqual(self.provider.create_calls, 2)

    def test_real_git_ownership_matrix_preserves_human_output_and_deletion_authority(self):
        from berean_translation.gitstore import GitStore
        variants = ('untracked_own', 'clean_bot_own', 'clean_human', 'human_then_bot',
                    'modified', 'staged', 'deleted', 'staged_deletion', 'committed_deletion',
                    'recreated_after_human_deletion')
        for variant in variants:
            with self.subTest(variant=variant):
                self.new_fixture()
                self.accept()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                self.engine.collect()
                self.engine.prepare()
                self.provider.complete_all(self.result)
                original = self.engine.state.write
                def crash(path, value):
                    if path == f'content/deu/articles/{A}.json':
                        raise RuntimeError('Crash after own HTML write')
                    return original(path, value)
                with patch.object(self.engine.state, 'write', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.collect()
                relative = f'content/deu/articles/{A}.html'
                html = self.root / relative
                planned_bytes = html.read_bytes()
                real_git = GitStore(self.root, publish=False)
                real_git.git('init', '-q')
                (self.root / 'fixture-baseline.txt').write_text('Disposable Git authority fixture.')
                real_git.git('add', '--', 'fixture-baseline.txt')
                def commit(author, message):
                    real_git.git('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=' + author, '-c', 'user.email=reviewer@example.test',
                        'commit', '-qm', message)
                commit('github-actions[bot]', 'Disposable collector baseline')
                if variant == 'clean_bot_own':
                    real_git.git('add', '--', relative)
                    commit('github-actions[bot]', 'Collector owns this exact intermediate output')
                elif variant != 'untracked_own':
                    if variant in ('modified', 'staged', 'human_then_bot'):
                        html.write_bytes(planned_bytes + b'\n<!-- Human editorial annotation. -->\n')
                    real_git.git('add', '--', relative)
                    commit('Human Reviewer', 'Human owns this partially published article')
                    self.assertEqual(real_git.human_edit_evidence(relative)['author'], 'Human Reviewer')
                    if variant in ('modified', 'staged', 'human_then_bot'):
                        html.write_bytes(planned_bytes)
                        if variant in ('staged', 'human_then_bot'): real_git.git('add', '--', relative)
                        if variant == 'human_then_bot':
                            commit('github-actions[bot]', 'Bot restores planned bytes after human ownership')
                    elif variant in ('deleted', 'staged_deletion', 'committed_deletion',
                                     'recreated_after_human_deletion'):
                        html.unlink()
                        if variant != 'deleted': real_git.git('add', '-u', '--', relative)
                        if variant in ('committed_deletion', 'recreated_after_human_deletion'):
                            commit('Human Reviewer', 'Human removed this unpublished article')
                        if variant == 'recreated_after_human_deletion': html.write_bytes(planned_bytes)
                    self.assertEqual(real_git.git('log', '-1', '--format=%an', '--', relative).stdout.strip(),
                                     'github-actions[bot]' if variant == 'human_then_bot' else 'Human Reviewer')
                before = tree_bytes(self.root / 'content')
                self.git = real_git
                try: self.reopened_engine().collect()
                except ContractError: pass
                if variant in ('untracked_own', 'clean_bot_own'):
                    self.assertEqual(self.task()['status'], 'complete')
                    self.assertEqual(self.state.record('deu', A)['published']['task'], self.item.entry_id)
                    self.assertEqual(html.read_bytes(), planned_bytes)
                else:
                    self.assertEqual(tree_bytes(self.root / 'content'), before)
                    self.assertFalse(self.state.record('deu', A).get('published'))
                    self.assertNotEqual(self.task()['status'], 'complete')
                self.assertEqual(self.provider.create_calls, 2)

    def install_deleted_human_history(self):
        """Commit and delete a human path inside this disposable fixture only."""
        from berean_translation.gitstore import GitStore
        real_git = GitStore(self.root, publish=False)
        if not (self.root / '.git').exists(): real_git.git('init', '-q')
        relative = f'content/deu/articles/{A}.html'
        html = self.root / relative
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(self.item.candidate['html'])
        def commit(message):
            real_git.git('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                '-c', 'user.name=Human Reviewer', '-c', 'user.email=reviewer@example.test',
                'commit', '-qm', message)
        real_git.git('add', '--', relative)
        commit('Human authored this article before component work')
        html.unlink()
        real_git.git('add', '-u', '--', relative)
        commit('Human removed the article without surrendering editorial authority')
        self.assertEqual(real_git.human_edit_evidence(relative, all_history=True)['author'], 'Human Reviewer')
        self.assertFalse(html.exists())
        self.git = real_git
        self.state.gitstore = real_git
        self.engine.gitstore = real_git
        return real_git

    def test_deleted_human_git_history_blocks_admission_without_any_state_write(self):
        self.install_deleted_human_history()
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): self.accept()
        self.assertEqual(tree_bytes(self.root), before)
        self.assertIsNone(self.task())
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_new_deleted_human_history_blocks_materialized_request_before_reservation(self):
        self.accept()
        self.install_deleted_human_history()
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): build_request(self.config, self.state, self.task())
        self.assertEqual(tree_bytes(self.root), before)
        try: self.engine.prepare()
        except ContractError: pass
        self.assertEqual(self.state.batches(), [])
        self.assertEqual((self.task()['translation_attempts'], self.task()['review_attempts']), (0, 0))
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
        self.assert_no_publication()

    def test_deleted_human_history_at_new_create_checkpoints_prevents_provider_create(self):
        for boundary in ('persist OpenAI input file', 'record batch submission intent'):
            with self.subTest(boundary=boundary):
                self.new_fixture()
                self.accept()
                original = self.git.checkpoint
                changed = []
                def takeover(message):
                    original(message)
                    if boundary in message and not changed:
                        changed.append(True)
                        self.install_deleted_human_history()
                with patch.object(self.git, 'checkpoint', side_effect=takeover):
                    try: self.engine.prepare()
                    except ContractError: pass
                self.assertTrue(changed)
                self.assertEqual(self.provider.create_calls, 0)
                self.assertEqual(self.task()['translation_attempts'], 1)
                self.assert_no_publication()
                try: self.engine.collect()
                except ContractError: pass
                self.assertEqual(self.provider.create_calls, 0)

    def test_deleted_human_history_does_not_block_truthful_unknown_create_reconciliation(self):
        self.accept()
        self.provider.raise_create = 'after'
        self.engine.prepare()
        self.assertEqual(self.state.batches()[0]['status'], 'submission_unknown')
        self.provider.raise_create = None
        self.install_deleted_human_history()
        self.reopened_engine().collect()
        self.assertEqual(self.state.batches()[0]['status'], 'submitted')
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (1, 1))
        self.assert_no_publication()

    def test_component_dispatch_supports_one_argument_read_adapters_and_fails_closed(self):
        from berean_translation.scripture_component_runtime import is_component
        task = {'id': 'legacy-task', 'campaign': 'legacy-campaign'}
        class OneArgumentAdapter:
            def __init__(self, ledger):
                self.ledger, self.paths = ledger, []
            def read(self, path):
                self.paths.append(path)
                return self.ledger
        for ledger in (None, {}):
            with self.subTest(absent=ledger):
                adapter = OneArgumentAdapter(ledger)
                self.assertIs(is_component(adapter, task), False)
                self.assertEqual(adapter.paths, [manual_admission.path(task['campaign'])])
        for ledger in (False, [], [None], '', 'invalid ledger', 0, True):
            with self.subTest(malformed=ledger):
                adapter = OneArgumentAdapter(ledger)
                with self.assertRaises(ContractError): is_component(adapter, task)
                self.assertEqual(adapter.paths, [manual_admission.path(task['campaign'])])
        for origin in ('task', 'ledger'):
            for marker in (None, False, {}, []):
                with self.subTest(origin=origin, marker=marker):
                    current = {**task, 'component_revision_sha256': marker} if origin == 'task' else task
                    ledger = None if origin == 'task' else {
                        'entries': {task['id']: {'component_revision': marker}}}
                    adapter = OneArgumentAdapter(ledger)
                    self.assertIs(is_component(adapter, current), True)
                    with patch('berean_translation.scripture_component_runtime.request',
                               side_effect=ContractError('Component validation reached')) as validate:
                        with self.assertRaisesRegex(ContractError, 'Component validation reached'):
                            build_request(self.config, adapter, current)
                    validate.assert_called_once_with(self.config, adapter, current)
