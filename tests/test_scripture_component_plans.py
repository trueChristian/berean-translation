"""Offline CLI/immutable-queue integration, using synthetic complete chapters.

These tests never establish real language readiness, change checked-in State,
contact a provider, or authorize production component admission.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

from berean_translation import cli, manual_admission
from berean_translation import scripture_component_plans as plans
from berean_translation.collector import collect_window
from berean_translation.common import ContractError, canonical, json_hash, loads, read_json
from berean_translation.engine import Engine
from berean_translation.queue import enqueue_github
from berean_translation.scripture_association import associate
from berean_translation.scripture_component_evidence import (build_component_evidence,
    validate_component_evidence)
from berean_translation.scripture_components import validate_components
from berean_translation.scripture_provider import GetBibleMCP
from jacques_plan_support import (ARTICLE, CAMPAIGN, CHAPTER_LENGTHS, LANGUAGES,
    ROMANS_2_4, SOURCE_HASH, authorization, fixture)
from support import REPO_ROOT
from test_scripture_component_runtime import tree_bytes


def rehash(value):
    value['sha256'] = json_hash({key: item for key, item in value.items() if key != 'sha256'})
    return value


class JacquesComponentPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            blocker = patch(target, side_effect=AssertionError('Network forbidden in Jacques plan fixture'))
            blocker.start(); self.addCleanup(blocker.stop)
        blocker = patch.object(GetBibleMCP, '_remote',
            side_effect=AssertionError('Real GetBible forbidden in Jacques plan fixture'))
        blocker.start(); self.addCleanup(blocker.stop)
        self.new_fixture()

    def new_fixture(self, **kwargs):
        directory = self.parent / str(len(list(self.parent.iterdir())))
        directory.mkdir()
        self.fx = fixture(directory, **kwargs)
        for key, value in vars(self.fx).items():
            setattr(self, key, value)
        return self.fx

    def inspect(self):
        return plans.inspect_jacques(self.engine, self.chapters)

    def request_for(self, package=None, languages=('deu',), *, run_id='123456'):
        package = package or self.inspect()
        selected = [entry['entry_id'] for entry in package['entries'] if entry['language'] in languages]
        return plans.build_request(package, selected_entry_ids=selected,
            request_id='component-gh-' + run_id, authorization=authorization(run_id))

    def queue_request(self, request):
        self.state.write(f'state/queue/{request["id"]}.json', request)

    def entry(self, language='deu'):
        return next(entry for entry in self.ledger['entries'].values()
                    if entry['item']['language'] == language)

    def task(self, language='deu'):
        return self.state.read(f'state/tasks/{self.entry(language)["task_id"]}/task.json')

    def assert_unadmitted(self):
        self.assertEqual(self.state.tasks(), [])
        self.assertEqual(self.state.batches(), [])
        current = self.state.read(f'state/campaigns/{CAMPAIGN}.json')
        self.assertEqual(current['reserved_usd'], 0)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def assert_originals(self):
        current = self.state.read(f'state/campaigns/{CAMPAIGN}.json')
        self.assertEqual(manual_admission.contract(current), manual_admission.contract(self.campaign))
        self.assertEqual(self.state.read(f'state/queue/{CAMPAIGN}.json'), self.request)
        current_entries = self.state.read(manual_admission.path(CAMPAIGN))['entries']
        for identity, original in self.ledger['entries'].items():
            self.assertEqual({key: value for key, value in current_entries[identity].items()
                              if key != 'component_revision'}, original)
        self.assertEqual(current['scripture_quotes']['version'], '1')
        self.assertEqual(current['budget_usd'], self.campaign['budget_usd'])
        self.assertEqual(current['models'], self.campaign['models'])

    def test_exact_existing_article_and_readonly_default_have_zero_proved_entries(self):
        before = tree_bytes(self.root)
        result = plans.inspect_jacques(self.engine)
        self.assertEqual(result['campaign_id'], CAMPAIGN)
        self.assertEqual(result['article_id'], ARTICLE)
        self.assertEqual(result['source_sha256'], SOURCE_HASH)
        self.assertEqual(result['entries'], [])
        self.assertEqual({hold['language'] for hold in result['holds']}, set(LANGUAGES))
        self.assertEqual(self.chapters.calls, [])
        self.assertEqual(tree_bytes(self.root), before)
        self.assert_unadmitted()
        self.assertIs(read_json(REPO_ROOT / 'config/runtime.json').get(
            'scripture_components_runtime_enabled', False), False)

    def test_complete_synthetic_inspection_covers_exact_scope_and_allusion_chapters(self):
        before = tree_bytes(self.root)
        result = self.inspect()
        self.assertEqual(result['holds'], [])
        self.assertEqual({entry['language'] for entry in result['entries']}, set(LANGUAGES))
        self.assertEqual(json_hash(result['source']), SOURCE_HASH)
        self.assertEqual(result['sha256'], rehash(copy.deepcopy(result))['sha256'])
        self.assertEqual(len(self.chapters.calls), 18)
        self.assertEqual(len(set(self.chapters.calls)), 18)
        for entry in result['entries']:
            evidence, policy = entry['evidence'], entry['scripture_policy']
            self.assertEqual(policy['source_association_version'], '3')
            self.assertEqual(evidence['source_association_version'], '3')
            self.assertEqual(len(evidence['quotes']), 1)
            self.assertEqual(len(evidence['references']), 7)
            self.assertEqual(sum(ref['classification'] == 'reference_only'
                                 for ref in evidence['references']), 6)
            self.assertEqual({tuple(map(int, key.split('/')[1:])) for key in evidence['lookups']
                              if key.startswith('kjv/')}, set(CHAPTER_LENGTHS))
            for envelope in evidence['lookups'].values():
                args = envelope['arguments']
                self.assertEqual(len(envelope['result']['data']['verses']),
                                 CHAPTER_LENGTHS[args['book'], args['chapter']])
            self.assertEqual(validate_component_evidence(result['source'], evidence,
                policy, require_fresh=True), evidence)
            quote = evidence['quotes'][0]
            self.assertEqual(quote['source_quote'], '…the goodness of God [that] leadeth thee to repentance')
            self.assertEqual(quote['printed_reference'], 'Rom 2:4')
            self.assertEqual(entry['origin']['budget_usd'], self.campaign['budget_usd'])
            self.assertEqual(entry['origin']['models_sha256'], json_hash(self.campaign['models']))
        self.assertEqual(tree_bytes(self.root), before)
        self.assert_unadmitted()

    def test_narrow_source_plan_preserves_authored_insertion_and_boundary_ellipsis(self):
        source = self.source.snapshot(ARTICLE)
        plan = plans.plan_jacques(source, ROMANS_2_4)
        scope = associate(source['html'], ARTICLE)[2][0]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]['scope_sha256'], json_hash(scope))
        proof = validate_components(ROMANS_2_4, scope['source_quote'],
                                    plan[0]['operations'], version='1')
        self.assertTrue(proof['independent_review_required'])
        authored = [operation['authored_text'] for operation in plan[0]['operations']
                    if operation['kind'] in ('insert', 'punctuation')]
        self.assertIn('…', authored)
        self.assertIn('[that]', ''.join(authored))
        self.assertNotIn('replace', {operation['kind'] for operation in plan[0]['operations']})
        self.assertEqual(json_hash(source), SOURCE_HASH)

    def test_ambiguous_or_unsupported_source_transformations_never_produce_a_plan(self):
        source = self.source.snapshot(ARTICLE)
        for verse in (ROMANS_2_4 + ' ' + ROMANS_2_4,
                      ROMANS_2_4.replace('leadeth', 'leads'),
                      ROMANS_2_4.replace('the goodness', 'the  goodness'),
                      ROMANS_2_4.replace('God leadeth', 'God truly leadeth'),
                      ROMANS_2_4.replace('the goodness', 'somethe goodness')):
            with self.subTest(verse=verse), self.assertRaises(ContractError):
                plans.plan_jacques(source, verse)
        for replacement in ('[this]', '[tha]t', 'that', '[that] ...'):
            changed = copy.deepcopy(source)
            changed['html'] = changed['html'].replace('[that]', replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ContractError):
                plans.plan_jacques(changed, ROMANS_2_4)

    def test_partial_provider_failures_are_language_holds_without_fallback_or_mutation(self):
        self.chapters.failures['luther1545/45/2'] = TimeoutError('Synthetic provider outage')
        before = tree_bytes(self.root)
        package = self.inspect()
        self.assertNotIn('deu', {entry['language'] for entry in package['entries']})
        self.assertIn('deu', {hold['language'] for hold in package['holds']})
        self.assertEqual(len(package['entries']), 11)
        self.assertEqual(tree_bytes(self.root), before)
        self.assert_unadmitted()

    def test_missing_expired_mismatched_and_incomplete_chapters_hold_closed(self):
        def expired(result):
            result['cache']['expires_at'] = '2000-01-01T00:00:00+00:00'
        def wrong_edition(result):
            result['data']['abbreviation'] = 'kjv'
        def missing_target(result):
            result['data']['verses'] = [v for v in result['data']['verses'] if v['verse'] != 4]
        def partial_target(result):
            result['data']['verses'] = result['data']['verses'][3:4]
        for mutate in (expired, wrong_edition, missing_target, partial_target):
            with self.subTest(mutate=mutate.__name__):
                self.chapters.mutations = {'luther1545/45/2': mutate}
                package = self.inspect()
                self.assertNotIn('deu', {entry['language'] for entry in package['entries']})
                self.assertIn('deu', {hold['language'] for hold in package['holds']})
                self.assert_unadmitted()
        self.chapters.mutations = {'kjv/40/5': lambda result: result['data']['verses'].pop()}
        package = self.inspect()
        self.assertEqual(package['entries'], [])
        self.assertEqual(len(package['holds']), 12)

    def test_complete_chapter_backstop_holds_other_unassociated_scripture(self):
        def add_hidden_match(result):
            result['data']['verses'][0]['text'] = 'Why are you in prison? You are only a youth. What brought you here?'
        self.chapters.mutations['kjv/40/5'] = add_hidden_match
        before = tree_bytes(self.root)
        package = self.inspect()
        self.assertEqual(package['entries'], [])
        self.assertTrue(all('unassociated_source_quote' in hold['detail']
                            or hold['reason'] == 'unassociated_source_quote' for hold in package['holds']))
        self.assertEqual(tree_bytes(self.root), before)

    def test_explicit_selection_and_exact_workflow_authority_are_required(self):
        self.new_fixture(languages=('deu',))
        package = self.inspect()
        good = self.request_for(package)
        plans.validate_request(self.config, good)
        selected = [entry['entry_id'] for entry in package['entries'] if entry['language'] == 'deu']
        for selection in ((), [], ['all'], ['not-an-entry'], selected * 2):
            with self.subTest(selection=selection), self.assertRaises(ContractError):
                plans.build_request(package, selected_entry_ids=selection,
                    request_id='component-gh-123456', authorization=authorization())
        for field, value in (('kind', 'workflow'), ('repository', 'wrong/repository'),
                             ('run_id', True), ('actor', ''), ('workflow_ref', 'refs/heads/draft')):
            changed = authorization(); changed[field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                plans.build_request(package, selected_entry_ids=selected,
                    request_id='component-gh-123456', authorization=changed)
        self.assert_unadmitted()

    def test_selected_queue_pauses_while_gate_closed_then_replays_idempotently(self):
        self.new_fixture(languages=('deu',))
        request = self.request_for()
        self.queue_request(request)
        before = canonical(request)
        self.engine.accept_queue()
        self.assert_unadmitted()
        self.assertIsNone(self.state.read(f'state/queue-errors/{request["id"]}.json'))
        self.config.runtime['scripture_components_runtime_enabled'] = True
        self.engine.accept_queue()
        self.assertEqual([task['language'] for task in self.state.tasks()], ['deu'])
        self.assertEqual(self.task()['status'], 'queued')
        self.assert_originals()
        after = tree_bytes(self.root)
        self.engine.accept_queue()
        self.assertEqual(tree_bytes(self.root), after)
        self.assertEqual(canonical(self.state.read(f'state/queue/{request["id"]}.json')), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_source_metadata_human_cancel_and_budget_drift_recheck_at_collection(self):
        for drift in ('html', 'metadata', 'human', 'cancel', 'usage', 'models'):
            with self.subTest(drift=drift):
                self.new_fixture(gate=True, languages=('deu',))
                request = self.request_for()
                self.queue_request(request)
                if drift in ('html', 'metadata'):
                    if drift == 'html': self.source.source['html'] += '\n'
                    else: self.source.source['article']['title'] += ' Changed'
                    self.source.inventory['articles'][ARTICLE]['translation_key'] = 'a' * 64
                elif drift == 'human':
                    record = self.state.record('deu', ARTICLE)
                    record['history'].append({'event': 'human_review'})
                    self.state.save_record(record)
                else:
                    campaign = self.state.read(f'state/campaigns/{CAMPAIGN}.json')
                    if drift == 'cancel': campaign['cancel_requested'] = True
                    elif drift == 'usage': campaign['reported_usage_usd'] = campaign['budget_usd']
                    else: campaign['models'][campaign['model']]['input_per_million'] = 999
                    self.state.save_campaign(campaign)
                if drift == 'models':
                    # The pre-existing original-campaign validator rejects this
                    # corruption before the component queue branch is reached.
                    with self.assertRaises(ContractError): self.engine.accept_queue()
                else:
                    self.engine.accept_queue()
                self.assert_unadmitted()

    def test_expired_archived_evidence_cannot_be_selected_or_admitted(self):
        self.new_fixture(gate=True, languages=('deu',))
        request = self.request_for()
        # Expiry is assessed against the clock; preserve the immutable receipt.
        future = datetime.now(timezone.utc) + timedelta(days=2)
        from berean_translation import scripture_evidence
        with patch.object(scripture_evidence, 'datetime') as clock:
            clock.now.return_value = future
            clock.fromisoformat.side_effect = datetime.fromisoformat
            with self.assertRaises(ContractError):
                self.request_for(request['package'])
            self.queue_request(request)
            self.engine.accept_queue()
        self.assert_unadmitted()

    def test_expired_materialized_request_replays_without_refresh_or_reallocation(self):
        self.new_fixture(gate=True, languages=('deu',))
        request = self.request_for()
        self.queue_request(request)
        self.engine.accept_queue()
        self.assertTrue(plans.request_materialized(self.state, request))
        before = tree_bytes(self.root)
        from berean_translation import scripture_evidence
        future = datetime.now(timezone.utc) + timedelta(days=2)
        with patch.object(scripture_evidence, 'datetime') as clock:
            clock.now.return_value = future
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.engine.accept_queue()
            self.assertTrue(plans.request_materialized(self.state, request))
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual(len(self.state.tasks()), 1)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
        self.assert_originals()

    def test_package_and_request_tamper_never_write_an_admission(self):
        self.new_fixture(gate=True, languages=('deu',))
        original = self.request_for()
        def package_changed(request, field, value):
            request['package'][field] = value
            rehash(request['package'])
            request['package_sha256'] = request['package']['sha256']
        mutations = {
            'hash': lambda r: r.update(package_sha256='0' * 64),
            'source': lambda r: r['package']['source'].update(html='changed'),
            'campaign': lambda r: package_changed(r, 'campaign_id', 'another-campaign'),
            'funding': lambda r: package_changed(r, 'funding_allocated', True),
            'publication': lambda r: package_changed(r, 'publication_ready', True),
            'empty': lambda r: r.update(selected_entry_ids=[]),
            'unknown': lambda r: r.update(selected_entry_ids=['0' * 32]),
            'duplicate': lambda r: r.update(selected_entry_ids=r['selected_entry_ids'] * 2),
            'extra': lambda r: r.update(allow_unverified=True),
            'scope': lambda r: r['package']['entries'][0]['scripture_policy'][
                'authored_components']['plans'][0].update(scope_sha256='0' * 64),
            'provider': lambda r: r['package']['entries'][0]['evidence'][
                'lookups']['luther1545/45/2']['result']['source'].update(url='https://example.test'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(original)
                mutate(changed)
                self.queue_request(changed)
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError):
                    plans.accept_request(self.engine, changed)
                self.assertEqual(tree_bytes(self.root), before)
                self.assert_unadmitted()

    def test_rehashed_language_policy_and_budget_tamper_fail_before_any_selected_write(self):
        self.new_fixture(gate=True, languages=('afr', 'deu'))
        original = self.request_for(languages=('afr', 'deu'))
        for change in ('language', 'policy', 'budget', 'processing', 'origin'):
            with self.subTest(change=change):
                request = copy.deepcopy(original)
                entry = request['package']['entries'][-1]
                if change == 'language':
                    entry['evidence'] = copy.deepcopy(request['package']['entries'][0]['evidence'])
                elif change == 'policy':
                    entry['scripture_policy']['prompt_addendum'] += '\nUnapproved policy text.'
                    tag = self.campaign['language_settings'][entry['language']]['tag']
                    entry['evidence'] = build_component_evidence(request['package']['source'],
                        tag, entry['scripture_policy'], self.chapters)
                    ledger_entry = self.ledger['entries'][entry['entry_id']]
                    entry['stage_budget'], entry['processing_sha256'] = plans._processing(
                        self.config, self.campaign, ledger_entry, entry['scripture_policy'])
                elif change == 'budget':
                    entry['stage_budget']['total_reserved_usd'] = 0
                elif change == 'processing':
                    entry['processing_sha256'] = '0' * 64
                else:
                    entry['origin']['budget_usd'] = 999
                rehash(entry); rehash(request['package'])
                request['package_sha256'] = request['package']['sha256']
                self.queue_request(request)
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError):
                    plans.accept_request(self.engine, request)
                self.assertEqual(tree_bytes(self.root), before)
                self.assert_unadmitted()

    def test_external_artifacts_and_cache_cannot_modify_repository_or_escape_with_symlink(self):
        for path in (self.root, self.root / 'state' / 'plan.json', self.root.parent):
            with self.subTest(path=path), self.assertRaises(ContractError):
                plans.external_path(self.root, path)
        link = self.parent / 'repository-link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ContractError):
            plans.external_path(self.root, link / 'cache')
        external = self.parent / 'private-evidence'
        external.mkdir()
        provider = plans.ChapterProvider(self.root, external)
        with self.assertRaises(ContractError): provider.chapter('kjv', 43, 3)
        with self.assertRaises(ContractError): provider.chapter('unapproved', 45, 2)
        with self.assertRaises(ContractError): provider.chapter('luther1545', 45, 2)
        self.assertEqual(list(external.iterdir()), [])
        self.assert_unadmitted()

    def test_cached_provider_verifies_whole_chapter_hash_identity_and_bounds(self):
        external = self.parent / 'private-evidence'
        external.mkdir()
        original = self.chapters.chapter('luther1545', 45, 2)
        path = external / 'luther1545-45-2.json'
        path.write_bytes(canonical(original))
        cached = plans.ChapterProvider(self.root, external)
        self.assertEqual(cached.chapter('luther1545', 45, 2), original)
        for mutation in ('hash', 'endpoint', 'arguments', 'partial', 'future', 'oversized'):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(original)
                if mutation == 'hash': value['result_sha256'] = '0' * 64
                elif mutation == 'endpoint': value['endpoint'] = 'https://example.test'
                elif mutation == 'arguments': value['arguments']['chapter'] = 3
                elif mutation == 'partial':
                    value['result']['data']['verses'] = value['result']['data']['verses'][3:4]
                    value['result_sha256'] = json_hash(value['result'])
                elif mutation == 'future':
                    value['retrieved_at'] = '2099-01-01T00:00:00+00:00'
                else: value['padding'] = 'x' * (plans.MAX_PACKAGE_BYTES + 1)
                path.write_bytes(canonical(value))
                with self.assertRaises(ContractError):
                    plans.ChapterProvider(self.root, external).chapter('luther1545', 45, 2)
        self.assert_unadmitted()

    def test_null_or_invalid_cached_receipt_never_triggers_replacement_fetch(self):
        external = self.parent / 'private-evidence'
        external.mkdir()
        path = external / 'luther1545-45-2.json'
        for raw in (b'null', b'{}', b'{broken JSON'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises((ContractError, ValueError)):
                    plans.ChapterProvider(self.root, external, fetch=True).chapter('luther1545', 45, 2)
                self.assertEqual(path.read_bytes(), raw)
        self.assert_unadmitted()

    def test_archived_cache_cannot_extend_original_ttl_or_invent_fetch_time(self):
        external = self.parent / 'private-evidence'
        external.mkdir()
        path = external / 'luther1545-45-2.json'
        original = self.chapters.chapter('luther1545', 45, 2)
        mutations = {
            'extended_ttl': lambda v: v['cache'].update(
                expires_at=(datetime.now(timezone.utc) + timedelta(days=31)).isoformat()),
            'declared_short_ttl': lambda v: v['cache'].update(max_retention_seconds=1),
            'excessive_retention': lambda v: v['cache'].update(max_retention_seconds=31 * 86400),
            'boolean_retention': lambda v: v['cache'].update(max_retention_seconds=True),
            'missing_fetch_time': lambda v: v['source'].pop('fetched_at'),
            'malformed_fetch_time': lambda v: v['source'].update(fetched_at='not-a-time'),
            'naive_fetch_time': lambda v: v['source'].update(fetched_at='2026-10-08T00:00:00'),
            'future_fetch_time': lambda v: v['source'].update(
                fetched_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = copy.deepcopy(original)
                mutate(value['result'])
                value['result_sha256'] = json_hash(value['result'])
                path.write_bytes(canonical(value))
                with self.assertRaises(ContractError):
                    plans.ChapterProvider(self.root, external, fetch=True).chapter('luther1545', 45, 2)
                self.assertEqual(loads(path.read_bytes()), value)
        self.assert_unadmitted()

    def workflow_environment(self, **changes):
        auth = authorization()
        environment = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_WORKFLOW_REF': auth['workflow_ref'],
            'GITHUB_REPOSITORY': auth['repository'], 'GITHUB_RUN_ID': auth['run_id'],
            'GITHUB_ACTOR': auth['actor'], 'GH_TOKEN': 'synthetic-test-token'}
        environment.update(changes)
        return environment

    def run_cli(self, command, *, environment=None, enqueue=None):
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment or {}, clear=True))
            stack.enter_context(patch.object(cli, 'Config', return_value=self.config))
            stack.enter_context(patch.object(cli, 'State', return_value=self.state))
            stack.enter_context(patch.object(cli, 'SourceClient', return_value=self.source))
            stack.enter_context(patch.object(cli, 'GitStore', return_value=self.git))
            stack.enter_context(patch.object(plans, 'ChapterProvider', return_value=self.chapters))
            stack.enter_context(patch.object(cli, 'OpenAIProvider',
                side_effect=AssertionError('Inspection must never construct a model client')))
            send = stack.enter_context(patch.object(cli, 'enqueue_github',
                side_effect=enqueue or AssertionError('Unexpected queue write')))
            stack.enter_context(redirect_stdout(output)); stack.enter_context(redirect_stderr(errors))
            status = cli.main(['--root', str(self.root), *command])
        return status, output.getvalue(), errors.getvalue(), send

    def test_cli_inspection_and_workflow_defaults_are_readonly_with_empty_selection(self):
        self.new_fixture(languages=('deu',))
        for command, environment in ((['inspect-scripture-components'], {}),
                (['enqueue-scripture-components-env'], self.workflow_environment())):
            with self.subTest(command=command):
                before = tree_bytes(self.root)
                status, output, error, send = self.run_cli(command, environment=environment)
                self.assertEqual((status, error), (0, ''))
                result = loads(output)
                self.assertEqual(result['selected_entry_ids'], [])
                self.assertIs(result['funding_allocated'], False)
                self.assertIs(result['publication_ready'], False)
                self.assertEqual(tree_bytes(self.root), before)
                send.assert_not_called(); self.assert_unadmitted()
        doc = yaml.load((REPO_ROOT / 'docs/historical-workflows/ai-scripture-components.yml').read_text(),
                        Loader=yaml.BaseLoader)
        inputs = doc['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['entry_ids']['default'], '')
        self.assertEqual(inputs['dry_run']['default'], 'true')
        self.assertEqual(set(doc['on']), {'workflow_dispatch'})
        self.assertIn("github.ref == 'refs/heads/main'", doc['jobs']['inspect']['if'])
        script = '\n'.join(step.get('run', '') for step in doc['jobs']['inspect']['steps'])
        self.assertIn('enqueue-scripture-components-env', script)
        self.assertNotIn(' tick', script)
        self.assertNotIn('OPENAI_API_KEY', canonical(doc).decode())

    def test_cli_rejects_forged_dispatch_context_and_nonexplicit_selection(self):
        cases = [{'GITHUB_ACTIONS': 'false'}, {'GITHUB_EVENT_NAME': 'schedule'},
            {'GITHUB_REF': 'refs/heads/draft'}, {'GITHUB_WORKFLOW_REF': 'refs/heads/main'},
            {'INPUT_DRY_RUN': 'false'}, {'INPUT_DRY_RUN': 'yes'}]
        for changes in cases:
            with self.subTest(changes=changes):
                before = tree_bytes(self.root)
                status, _, errors, send = self.run_cli(['enqueue-scripture-components-env'],
                    environment=self.workflow_environment(**changes))
                self.assertEqual(status, 1); self.assertIn('ERROR:', errors)
                send.assert_not_called()
                self.assertEqual(tree_bytes(self.root), before)
                self.assertEqual(self.chapters.calls, [])
                self.assert_unadmitted()

    def test_cli_persists_selected_immutable_request_and_rerun_cannot_refresh_it(self):
        self.new_fixture(languages=('deu',))
        selected = self.entry()['task_id']
        def enqueue(config, request, repository, token):
            self.assertEqual(config, self.config)
            self.assertEqual(repository, authorization()['repository'])
            self.assertEqual(token, 'synthetic-test-token')
            self.queue_request(request)
            return {'synthetic_persisted': request['id']}
        environment = self.workflow_environment(INPUT_DRY_RUN='false', INPUT_ENTRY_IDS=selected)
        status, output, errors, send = self.run_cli(['enqueue-scripture-components-env'],
            environment=environment, enqueue=enqueue)
        self.assertEqual((status, errors), (0, '')); send.assert_called_once()
        request = self.state.read('state/queue/component-gh-123456.json')
        self.assertEqual(request['selected_entry_ids'], [selected])
        self.assert_unadmitted()
        before = tree_bytes(self.root)
        with patch.object(plans, 'inspect_jacques', side_effect=AssertionError('Rerun must reuse request')):
            status, output, errors, send = self.run_cli(['enqueue-scripture-components-env'],
                environment=environment)
        self.assertEqual((status, errors), (0, ''))
        self.assertIs(loads(output)['already_queued'], True); send.assert_not_called()
        self.assertEqual(tree_bytes(self.root), before)
        for changes in ({'INPUT_ENTRY_IDS': ''}, {'GITHUB_ACTOR': 'different-owner'}):
            status, _, errors, send = self.run_cli(['enqueue-scripture-components-env'],
                environment={**environment, **changes})
            self.assertEqual(status, 1); self.assertIn('different immutable', errors)
            send.assert_not_called(); self.assertEqual(tree_bytes(self.root), before)

    def test_remote_queue_collision_reuses_exact_bytes_and_rejects_different_selection(self):
        self.new_fixture(languages=('deu',))
        request = self.request_for()
        calls = []
        changed = False
        def transport(method, url, token, body=None):
            calls.append((method, url, body))
            if method == 'PUT':
                self.assertEqual(loads(base64.b64decode(body['content'])), request)
                self.assertEqual(body['branch'], 'main')
                raise HTTPError(url, 422, 'Synthetic existing path', None, None)
            existing = copy.deepcopy(request)
            if changed: existing['selected_entry_ids'] = []
            return {'content': base64.b64encode(canonical(existing)).decode()}
        result = enqueue_github(self.config, request, authorization()['repository'],
                                'synthetic-test-token', transport=transport)
        self.assertIs(result['already_queued'], True)
        self.assertEqual([call[0] for call in calls], ['PUT', 'GET'])
        changed = True
        with self.assertRaises(ContractError):
            enqueue_github(self.config, request, authorization()['repository'],
                           'synthetic-test-token', transport=transport)
        with self.assertRaises(ContractError):
            enqueue_github(self.config, request, 'another/repository',
                           'synthetic-test-token', transport=transport)
        self.assert_unadmitted()

    def test_large_queue_collision_reads_only_matching_git_blob_and_rejects_tamper(self):
        self.new_fixture(languages=('deu',))
        request = self.request_for()
        repository = authorization()['repository']
        raw = canonical(request) + b'\n'
        sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        metadata_holds = {'missing_sha', 'metadata_sha', 'metadata_size', 'oversized_metadata'}
        for mutation in ('none', *sorted(metadata_holds), 'wrong_sha', 'encoding', 'content',
                         'blob_size', 'oversized_encoding', 'same_json_different_bytes'):
            with self.subTest(mutation=mutation):
                calls = []
                def transport(method, url, token, body=None):
                    calls.append((method, url))
                    if method == 'PUT':
                        raise HTTPError(url, 422, 'Synthetic existing path', None, None)
                    if '/contents/' in url:
                        return {'encoding': 'none',
                            'size': (len(raw) + 1 if mutation == 'metadata_size' else
                                     plans.MAX_PACKAGE_BYTES + 100 if mutation == 'oversized_metadata' else len(raw)),
                            'sha': ('' if mutation == 'missing_sha' else
                                    'b' * 40 if mutation == 'metadata_sha' else sha)}
                    self.assertEqual(url, f'https://api.github.com/repos/{repository}/git/blobs/{sha}')
                    value = copy.deepcopy(request)
                    if mutation == 'content': value['selected_entry_ids'] = []
                    encoded = base64.b64encode(canonical(value) + b'\n').decode()
                    if mutation == 'oversized_encoding': encoded += ' ' * (2 * len(raw) + 128)
                    if mutation == 'same_json_different_bytes':
                        encoded = base64.b64encode(canonical(value)).decode()
                    return {'sha': 'b' * 40 if mutation == 'wrong_sha' else sha,
                        'encoding': 'none' if mutation == 'encoding' else 'base64',
                        'size': len(raw) + 1 if mutation == 'blob_size' else len(raw),
                        'content': encoded}
                if mutation == 'none':
                    result = enqueue_github(self.config, request, repository,
                        'synthetic-test-token', transport=transport)
                    self.assertIs(result['already_queued'], True)
                else:
                    with self.assertRaises(ContractError):
                        enqueue_github(self.config, request, repository,
                            'synthetic-test-token', transport=transport)
                self.assertEqual(len(calls), 2 if mutation in metadata_holds else 3)
        self.assert_unadmitted()

    def test_selected_aggregate_budget_must_fit_before_first_revision(self):
        self.new_fixture(languages=('deu', 'afr'))
        package = self.inspect()
        maximum = max(Decimal(str(entry['stage_budget']['total_reserved_usd']))
                      for entry in package['entries'])
        self.new_fixture(gate=True, languages=('deu', 'afr'), budget=float(maximum * Decimal('1.5')))
        request = self.request_for(languages=('deu', 'afr'))
        self.assertEqual(len(request['package']['entries']), 2)
        self.queue_request(request)
        before = tree_bytes(self.root)
        with self.assertRaises(ContractError): plans.accept_request(self.engine, request)
        self.assertEqual(tree_bytes(self.root), before)
        self.assert_unadmitted()

    def test_interrupted_selected_queue_replays_same_revision_and_task_once(self):
        for boundary in ('freeze additive component admission', 'materialize never-paid component revision'):
            with self.subTest(boundary=boundary):
                self.new_fixture(gate=True, languages=('deu', 'afr'))
                request = self.request_for(languages=('deu', 'afr'))
                self.queue_request(request)
                original = self.git.checkpoint
                def crash(message):
                    original(message)
                    if boundary in message: raise RuntimeError('Synthetic collector interruption')
                with patch.object(self.git, 'checkpoint', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.accept_queue()
                self.engine = Engine(self.config, self.source, self.provider, self.git)
                self.engine.scripture_provider = self.chapters
                self.engine.accept_queue()
                tasks = self.state.tasks()
                self.assertEqual({task['language'] for task in tasks}, {'deu', 'afr'})
                self.assertEqual({task['id'] for task in tasks}, set(request['selected_entry_ids']))
                self.assertTrue(all(task['translation_attempts'] == task['review_attempts'] == 0
                                    for task in tasks))
                before = tree_bytes(self.root)
                self.engine.accept_queue()
                self.assertEqual(tree_bytes(self.root), before)
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))
                self.assert_originals()

    def test_incomplete_revision_rechecks_human_and_current_source_before_other_selected_write(self):
        for drift in ('human', 'source'):
            with self.subTest(drift=drift):
                self.new_fixture(gate=True, languages=('deu', 'afr'))
                request = self.request_for(languages=('deu', 'afr'))
                self.queue_request(request)
                def crash(message):
                    if 'freeze additive component admission' in message:
                        raise RuntimeError('Synthetic crash after immutable intent')
                with patch.object(self.git, 'checkpoint', side_effect=crash):
                    with self.assertRaises(RuntimeError): self.engine.accept_queue()
                entries = self.state.read(manual_admission.path(CAMPAIGN))['entries']
                interrupted = next(entry for entry in entries.values() if 'component_revision' in entry)
                if drift == 'human':
                    record = self.state.record(interrupted['item']['language'], ARTICLE)
                    record['history'].append({'event': 'human_review'})
                    self.state.save_record(record)
                else:
                    self.source.source['html'] += '\nChanged current source.'
                    self.source.inventory['articles'][ARTICLE]['translation_key'] = 'a' * 64
                before = tree_bytes(self.root)
                with self.assertRaises(ContractError): plans.accept_request(self.engine, request)
                self.assertEqual(tree_bytes(self.root), before)
                self.assertEqual(self.state.tasks(), [])
                self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (0, 0))

    def test_synthetic_selected_queue_runs_existing_serialized_collector_end_to_end(self):
        self.new_fixture(gate=True, languages=('deu',))
        request = self.request_for()
        self.queue_request(request)
        entry = request['package']['entries'][0]
        quote = entry['evidence']['quotes'][0]
        source = request['package']['source']
        candidate = {'html': source['html'], **{field: source['article'][field]
                     for field in ('title', 'subtitle', 'section')}}
        # Fake target chapters deliberately contain the exact English string.
        # The simulator can therefore return its structural identity selection.
        selections = [{'quote_id': quote['id'], 'block': quote['block'],
            'start': quote['source_start'], 'end': quote['source_end'],
            'operations': copy.deepcopy(quote['component_source']['operations'])}]
        def responder(line):
            payload = loads(line['body']['messages'][1]['content'])
            binding = copy.deepcopy(payload['expected_binding'])
            if ':review' in line['custom_id']:
                return {'binding': binding, 'review': {'score': 98, 'passed': True,
                    'findings_complete': True, 'findings': []}}
            return {'binding': binding, 'candidate': copy.deepcopy(candidate),
                    'scripture_selections': copy.deepcopy(selections)}
        elapsed = 0
        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds
            self.provider.complete_all(responder)
        result = collect_window(self.engine, wait_seconds=180, poll_seconds=30,
                                monotonic=lambda: elapsed, sleep=sleep)
        task = self.task()
        self.assertEqual(task['status'], 'complete')
        self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (2, 2))
        self.assertEqual(result['stop_reason'], 'no_submitted_batches')
        self.assertEqual(self.state.candidate(task), candidate)
        publication = self.state.record('deu', ARTICLE)['published']
        self.assertEqual(publication['task'], task['id'])
        self.assertIs(publication['human_reviewed'], False)
        self.assertTrue((self.root / publication['html_path']).is_file())
        self.assert_originals()
        before = tree_bytes(self.root)
        self.engine.accept_queue()
        self.assertEqual(tree_bytes(self.root), before)
        self.assertEqual((self.provider.upload_calls, self.provider.create_calls), (2, 2))


if __name__ == '__main__':
    unittest.main()
