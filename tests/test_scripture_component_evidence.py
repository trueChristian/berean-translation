"""Integrated v2 offline proofs; live admission/publication remain disabled."""
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from berean_translation.common import ContractError, json_hash, read_json, canonical
from berean_translation.scripture_association import associate
from berean_translation.scripture_component_evidence import (
    component_policy, validate_component_policy, validate_component_evidence,
    check_component_selections)
from berean_translation.scripture_evidence import (
    ScriptureAttention, policy, frozen_policy, build_evidence, check_selections,
    load_evidence, freeze_scripture_evidence, normalize_scripture_candidate,
    validate_scripture_candidate, adopt_scripture_selection_audit)
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.requests import build_request
from test_scripture_evidence import source, fixture, FakeMCP
from support import setup, queue

ROOT = Path(__file__).resolve().parents[1]
BLOCK = '/article[1]/p[1]'


def part(verse, start, end=None):
    end = len(verse) if end is None else end
    return {'kind': 'canonical', 'start': start, 'end': end, 'text': verse[start:end]}


def omit(verse, start, end=None):
    return {**part(verse, start, end), 'kind': 'omit', 'reason': 'excerpt_boundary'}


def insertion(verse, text):
    position = verse.index(' ') + 1
    end = len(verse.rstrip())
    operations = [part(verse, 0, position), {'kind': 'insert', 'id': 'a1', 'at': position,
        'authored_text': text + ' '}, part(verse, position, end)]
    if end != len(verse): operations.append(omit(verse, end))
    return verse[:position] + text + ' ' + verse[position:end], operations


def example():
    english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
    target = fixture('luther1545')['structuredContent']['data']['verses'][1]['text']
    printed, operations = insertion(english, '[indeed]')
    src = source(printed)
    scopes = associate(src['html'], src['article']['id'])[2]
    contract = component_policy(ROOT, src, [{'scope_sha256': json_hash(scopes[0]), 'operations': operations}])
    quote, target_operations = insertion(target, '[wirklich]')
    candidate = {'html': source(quote)['html'], 'title': 'Lehre', 'subtitle': None, 'section': None}
    selections = [{'quote_id': 'q1', 'block': BLOCK, 'start': 1, 'end': 1 + len(quote),
                   'operations': target_operations}]
    return src, contract, candidate, selections


class MemoryState:
    def __init__(self, src, contract, bundle):
        self.src = src
        digest = json_hash(bundle)
        self.task = {'id': 'test', 'campaign': 'test', 'scripture_evidence_path': f'state/scripture/{digest}.json',
                     'scripture_evidence_sha256': digest}
        self.values = {self.task['scripture_evidence_path']: copy.deepcopy(bundle),
                       'state/campaigns/test.json': {'scripture_quotes': copy.deepcopy(contract)}}
        self.writes = []
    def source(self, task): return copy.deepcopy(self.src)
    def read(self, path, default=None): return copy.deepcopy(self.values.get(path, default))
    def write(self, path, value): self.writes.append((path, value)); self.values[path] = copy.deepcopy(value)
    def candidate(self, task): return None


class ComponentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source, self.contract, self.candidate, self.selections = example()
        self.fake = FakeMCP()
        self.provider = GetBibleMCP(self.fake)
    def build(self, **kwargs):
        return build_evidence(kwargs.get('source', self.source), kwargs.get('tag', 'de'),
                              kwargs.get('contract', self.contract), kwargs.get('provider', self.provider))

    def test_default_policy_is_historical_and_annotations_still_hold(self):
        original = policy(ROOT)
        self.assertEqual(original['version'], '1')
        self.assertNotIn('authored_components', original)
        with self.assertRaisesRegex(ScriptureAttention, 'source_quote_annotation'):
            self.build(contract=original)
        self.assertEqual(self.fake.calls, [])

    def test_explicit_new_frozen_policy_does_not_modify_source_or_old_policy(self):
        before = canonical(self.source)
        self.assertEqual(frozen_policy({'scripture_quotes': self.contract}), self.contract)
        self.assertEqual(canonical(self.source), before)
        self.assertEqual(frozen_policy({'scripture_quotes': {'version': '1'}}), {'version': '1'})
        self.assertIsNone(frozen_policy({}))

    def test_complete_source_provider_candidate_binding_and_readonly_replay(self):
        original = canonical([self.source, self.contract, self.candidate, self.selections])
        evidence = self.build()
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(evidence['version'], '2')
        self.assertEqual(evidence['component_contract_sha256'], json_hash(self.contract))
        self.assertEqual(evidence['quotes'][0]['component_source']['quote'],
                         associate(self.source['html'], self.source['article']['id'])[2][0]['source_quote'])
        with patch.object(GetBibleMCP, '_remote', side_effect=AssertionError('no network on replay')):
            self.assertEqual(validate_component_evidence(self.source, evidence, self.contract), evidence)
            proof = check_selections(evidence, self.candidate, self.selections, source=self.source,
                                      component_contract=self.contract)
        self.assertEqual(proof['candidate_sha256'], json_hash(self.candidate))
        self.assertEqual(proof['source_sha256'], json_hash(self.source))
        self.assertTrue(proof['independent_review_required'])
        self.assertFalse(proof['runtime_admission_supported'])
        self.assertEqual(original, canonical([self.source, self.contract, self.candidate, self.selections]))
        self.assertEqual(len(self.fake.calls), 2)

    def test_source_and_scope_drift_fail_before_provider_access(self):
        changed = copy.deepcopy(self.source); changed['article']['title'] += '!'
        with self.assertRaisesRegex(ScriptureAttention, 'component_source_changed'): self.build(source=changed)
        for mutate in ('hash', 'missing', 'duplicate', 'reordered'):
            contract = copy.deepcopy(self.contract)
            plans = contract['authored_components']['plans']
            if mutate == 'hash': plans[0]['scope_sha256'] = 'a' * 64
            elif mutate == 'missing': plans.clear()
            else: plans.append(copy.deepcopy(plans[0]))
            contract['authored_components']['plans_sha256'] = json_hash(plans)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(ScriptureAttention, 'component_scope_changed'):
                self.build(contract=contract)
        self.assertEqual(self.fake.calls, [])

    def test_bad_policy_versions_bounds_and_plan_hash_are_held(self):
        for change in ('version', 'association', 'normalization', 'plan_version', 'hash', 'bound', 'map'):
            contract = copy.deepcopy(self.contract)
            if change == 'version': contract['version'] = '3'
            elif change == 'association': contract['source_association_version'] = '1'
            elif change == 'normalization': contract['selection_normalization_version'] = '1'
            elif change == 'plan_version': contract['authored_components']['version'] = '2'
            elif change == 'hash': contract['authored_components']['plans_sha256'] = 'a' * 64
            elif change == 'bound': contract['max_evidence_bytes'] = True
            elif change == 'map':
                contract['edition_map']['locales']['de']['abbreviation'] = 'kjv'
                contract['edition_map_sha256'] = json_hash(contract['edition_map'])
            with self.subTest(change=change), self.assertRaises(ContractError):
                frozen_policy({'scripture_quotes': contract})

    def test_unavailable_edition_never_uses_another(self):
        for tag in ('bn', 'hi', 'id', 'sw', 'ur'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ScriptureAttention, 'missing_edition'):
                self.build(tag=tag)
        self.assertEqual(self.fake.calls, [])

    def test_missing_wrong_or_changed_provider_envelope_is_held(self):
        good = self.provider.chapter('kjv', 43, 4)
        for change in ('hash', 'edition', 'verse', 'scope', 'endpoint', 'timestamp'):
            value = copy.deepcopy(good)
            if change == 'hash': value['result_sha256'] = 'a' * 64
            elif change == 'edition': value['arguments']['translation'] = 'luther1545'
            elif change == 'verse': value['result']['data']['verses'].append(copy.deepcopy(value['result']['data']['verses'][0]))
            elif change == 'scope': value['result']['scope']['chapter'] = 5
            elif change == 'endpoint': value['endpoint'] = 'https://example.test/'
            elif change == 'timestamp': value['retrieved_at'] = 'not-a-date'
            if change in ('verse', 'scope'): value['result_sha256'] = json_hash(value['result'])
            cached = SimpleNamespace(chapter=lambda *args: value)
            with self.subTest(change=change), self.assertRaisesRegex(ScriptureAttention, 'component_provider_changed'):
                self.build(provider=cached)

    def test_frozen_evidence_and_contract_versions_cannot_be_swapped(self):
        evidence = self.build()
        state = MemoryState(self.source, self.contract, evidence)
        self.assertEqual(load_evidence(state, state.task), evidence)
        for change in ('source', 'provider', 'candidate_proof', 'version', 'old_contract'):
            copy_state = MemoryState(self.source, self.contract, evidence)
            if change == 'source': copy_state.src['article']['title'] += '!'
            elif change == 'old_contract': copy_state.values['state/campaigns/test.json']['scripture_quotes'] = policy(ROOT)
            else:
                item = copy_state.values[copy_state.task['scripture_evidence_path']]
                if change == 'provider': item['lookups'].clear()
                elif change == 'version': item['version'] = '1'
                else: item['quotes'][0]['component_source_proof']['independent_review_required'] = False
            with self.subTest(change=change), self.assertRaises(ContractError): load_evidence(copy_state, copy_state.task)

    def test_forged_rehashed_metadata_fails_full_recomputation(self):
        evidence = self.build()
        for change in ('lookups', 'quote', 'proof', 'structure', 'contract'):
            modified = copy.deepcopy(evidence)
            if change == 'lookups': modified['lookups'].clear()
            elif change == 'quote': modified['quotes'][0]['source_quote'] += '!'
            elif change == 'proof': modified['quotes'][0]['component_source_proof']['independent_review_required'] = False
            elif change == 'structure': modified['source_structure_sha256'] = 'a' * 64
            elif change == 'contract': modified['component_contract_sha256'] = 'a' * 64
            state = MemoryState(self.source, self.contract, modified)
            with self.subTest(change=change), self.assertRaises(ContractError): load_evidence(state, state.task)

    def test_candidate_words_scope_brackets_identity_and_metadata_are_all_bound(self):
        evidence = self.build()
        for change in ('words', 'brackets', 'quote_extent', 'reference', 'html', 'title', 'missing', 'duplicate', 'offset', 'component'):
            candidate, selections = copy.deepcopy(self.candidate), copy.deepcopy(self.selections)
            if change == 'words': candidate['html'] = candidate['html'].replace('Mann', 'Frau')
            elif change == 'brackets': candidate['html'] = candidate['html'].replace('[wirklich]', 'wirklich')
            elif change == 'quote_extent': candidate['html'] = candidate['html'].replace('”', ' extra”')
            elif change == 'reference': candidate['html'] = candidate['html'].replace('4:16', '4:15')
            elif change == 'html': candidate['html'] = candidate['html'].replace('<p>', '<p><em>').replace('</p>', '</em></p>')
            elif change == 'title': candidate['title'] = ''
            elif change == 'missing': selections.clear()
            elif change == 'duplicate': selections.append(copy.deepcopy(selections[0]))
            elif change == 'offset': selections[0]['end'] -= 1
            elif change == 'component': selections[0]['operations'][1]['id'] = 'a2'
            with self.subTest(change=change), self.assertRaises(ContractError):
                check_component_selections(evidence, candidate, selections, source=self.source, contract=self.contract)

    def test_complete_context_and_policy_mandatory_for_candidate_check(self):
        evidence = self.build()
        for src, contract in ((None, self.contract), (self.source, None)):
            with self.assertRaisesRegex(ScriptureAttention, 'component_contract'):
                check_component_selections(evidence, self.candidate, self.selections, source=src, contract=contract)

    def test_expired_provider_evidence_is_readable_but_cannot_be_newly_frozen(self):
        evidence = self.build()
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch('berean_translation.scripture_evidence.datetime', FutureClock):
            self.assertEqual(validate_component_evidence(self.source, evidence, self.contract), evidence)
            with self.assertRaisesRegex(ScriptureAttention, 'evidence_expired'):
                validate_component_evidence(self.source, evidence, self.contract, require_fresh=True)

    def test_unclaimed_unmarked_component_repetition_remains_held(self):
        src = copy.deepcopy(self.source)
        src['html'] = src['html'].replace('</article>', '<p>Ordinary prose remains.</p></article>')
        scopes = associate(src['html'], src['article']['id'])[2]
        plans = copy.deepcopy(self.contract['authored_components']['plans'])
        plans[0]['scope_sha256'] = json_hash(scopes[0])
        contract = component_policy(ROOT, src, plans)
        evidence = self.build(source=src, contract=contract)
        candidate = copy.deepcopy(self.candidate)
        rendered = associate(candidate['html'], src['article']['id'], evidence=evidence)[2][0]['source_quote']
        candidate['html'] = candidate['html'].replace('</article>', '<p>' + rendered + '</p></article>')
        with self.assertRaisesRegex(ScriptureAttention, 'quote_scope_changed'):
            check_component_selections(evidence, candidate, self.selections, source=src, contract=contract)

    def test_freeze_normalize_publish_and_adopt_are_explicitly_disabled(self):
        evidence = self.build(); state = MemoryState(self.source, self.contract, evidence)
        engine = SimpleNamespace(state=SimpleNamespace(record=lambda *args: {}),
            human_protected=lambda *args: False, config=SimpleNamespace(root=ROOT))
        calls = (
            lambda: freeze_scripture_evidence(engine, self.source, 'deu', frozen_policy=self.contract, provider=self.provider),
            lambda: normalize_scripture_candidate(state, state.task, self.candidate),
            lambda: validate_scripture_candidate(state, state.task, self.candidate),
            lambda: adopt_scripture_selection_audit(state, state.task))
        for call in calls:
            with self.assertRaisesRegex(ScriptureAttention, 'component_runtime_not_enabled'): call()
        self.assertEqual(state.writes, [])

    def test_complete_source_cannot_be_shortened_to_target_excerpt(self):
        english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
        target = fixture('luther1545')['structuredContent']['data']['verses'][1]['text']
        src = source(english)
        scope = associate(src['html'], src['article']['id'])[2][0]
        contract = component_policy(ROOT, src, [{'scope_sha256': json_hash(scope), 'operations': [part(english, 0)]}])
        evidence = self.build(source=src, contract=contract)
        short = target.split()[0]
        candidate = {'html': source(short)['html'], 'title': 'Lehre', 'subtitle': None, 'section': None}
        claims = [{'quote_id': 'q1', 'block': BLOCK, 'start': 1, 'end': 1 + len(short),
                   'operations': [part(target, 0, len(short)), omit(target, len(short))]}]
        with self.assertRaisesRegex(ScriptureAttention, 'component_excerpt_changed'):
            check_component_selections(evidence, candidate, claims, source=src, contract=contract)

    def test_prepared_submission_guard_blocks_upload_and_create(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, paid, _, engine = setup(Path(directory))
            engine.discover()
            campaign = engine.accept_request(queue(state, languages='deu'))
            with patch.object(engine, 'submit'):
                engine.prepare()
            batch = state.batches()[0]
            campaign = state.read(f'state/campaigns/{campaign["id"]}.json')
            campaign['scripture_quotes'] = self.contract
            state.save_campaign(campaign)
            with self.assertRaisesRegex(ScriptureAttention, 'component_runtime_not_enabled'):
                engine.submit(batch)
            self.assertEqual(paid.upload_calls, 0)
            self.assertEqual(paid.create_calls, 0)

    def test_component_policy_and_evidence_markers_cannot_be_downgraded(self):
        downgraded = copy.deepcopy(self.contract); downgraded['version'] = '1'
        with self.assertRaisesRegex(ScriptureAttention, 'component_contract'):
            frozen_policy({'scripture_quotes': downgraded})
        with self.assertRaisesRegex(ScriptureAttention, 'component_contract'):
            self.build(contract=downgraded)
        del downgraded['authored_components']
        with self.assertRaisesRegex(ScriptureAttention, 'component_contract'):
            frozen_policy({'scripture_quotes': downgraded})
        evidence = self.build(); evidence['version'] = '1'
        state = MemoryState(self.source, policy(ROOT), evidence)
        with self.assertRaisesRegex(ScriptureAttention, 'component_contract'): load_evidence(state, state.task)
        with self.assertRaisesRegex(ScriptureAttention, 'component_contract'):
            check_selections(evidence, self.candidate, self.selections, source=self.source)

    def test_component_contract_introduced_at_intent_checkpoint_cannot_create(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, paid, _, engine = setup(Path(directory))
            engine.discover()
            campaign = engine.accept_request(queue(state, languages='deu'))
            with patch.object(engine, 'submit'): engine.prepare()
            batch = state.batches()[0]
            checkpoint = engine.checkpoint
            def change_at_intent(message):
                checkpoint(message)
                if 'submission intent' in message:
                    current = state.read(f'state/campaigns/{campaign["id"]}.json')
                    current['scripture_quotes'] = self.contract
                    state.save_campaign(current)
            with patch.object(engine, 'checkpoint', side_effect=change_at_intent):
                with self.assertRaisesRegex(ScriptureAttention, 'component_runtime_not_enabled'):
                    engine.submit(batch)
            self.assertEqual(paid.upload_calls, 1)
            self.assertEqual(paid.create_calls, 0)

    def test_incomplete_policy_provenance_and_oversized_candidate_are_held(self):
        contract = copy.deepcopy(self.contract); del contract['edition_map_provenance']
        with self.assertRaisesRegex(ScriptureAttention, 'component_contract'):
            frozen_policy({'scripture_quotes': contract})
        contract = copy.deepcopy(self.contract); contract['max_component_candidate_bytes'] = 10
        evidence = self.build(contract=contract)
        with self.assertRaisesRegex(ScriptureAttention, 'selection_size_limit'):
            check_component_selections(evidence, self.candidate, self.selections, source=self.source, contract=contract)

    def test_every_paid_request_stage_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, upstream, paid, _, engine = setup(Path(directory))
            engine.discover()
            campaign = engine.accept_request(queue(state, languages='deu'))
            task = state.read(f'state/tasks/{campaign["tasks"][0]}/task.json')
            campaign['scripture_quotes'] = self.contract
            state.save_campaign(campaign)
            for stage in ('translate', 'correct', 'review1', 'review2'):
                task['stage'] = stage
                with self.subTest(stage=stage), self.assertRaisesRegex(ScriptureAttention, 'component_runtime_not_enabled'):
                    build_request(config, state, task)
            self.assertEqual(paid.upload_calls, 0)
            self.assertEqual(paid.create_calls, 0)


class AuthenticJoelComponentTests(unittest.TestCase):
    """Fresh Oct 7 adapter envelopes; original Oct 6 envelopes were not recovered."""
    def test_real_complete_source_and_fresh_evidence_preserve_the_morphology_hold(self):
        src_path = ROOT / 'state/sources/c54fa4226bc3571d72d5ba970bfd4c62db65eb663495f74611d338b1b68642ab.json'
        candidate_path = ROOT / 'state/tasks/7d239350b115e86d70e78bffc1c791b5/candidate.json'
        src, accepted = read_json(src_path), read_json(candidate_path)
        before = canonical([src, accepted])
        self.assertEqual(json_hash(src), 'c54fa4226bc3571d72d5ba970bfd4c62db65eb663495f74611d338b1b68642ab')
        self.assertEqual(json_hash(accepted), '7151479b3bf135e78f6d053acda6ec461415e31fe2aac4369da30a25ac6a583f')
        envelopes = {edition: read_json(ROOT / f'tests/fixtures/scripture-components/{edition}-joel2-fresh.json')
                     for edition in ('kjv', 'almeida')}
        expected_hashes = {'kjv': 'f82f97adca471020930ec1a6a12fe5b900995fac8003a0acfe3ce9c18bf9f423',
                           'almeida': '742f8b66bb7f6d22e1f6bed5fedb4929e9bb99442a2f7bba657cc6a8ced8e82c'}
        for edition, envelope in envelopes.items():
            self.assertEqual(json_hash(envelope['result']), expected_hashes[edition])
            self.assertEqual(envelope['result_sha256'], expected_hashes[edition])
        english, target = [next(v['text'] for v in envelopes[e]['result']['data']['verses'] if v['verse'] == 25)
                           for e in ('kjv', 'almeida')]
        operations = [omit(english, 0, 11), part(english, 11, 22),
            {**part(english, 22, 25), 'kind': 'replace', 'id': 'a1', 'authored_text': '[us]'},
            part(english, 25, 62), {'kind': 'punctuation', 'id': 'a2', 'at': 62, 'authored_text': '!'},
            omit(english, 62)]
        original_scope = associate(src['html'], src['article']['id'])[2][0]
        contract = component_policy(ROOT, src, [{'scope_sha256': json_hash(original_scope), 'operations': operations}])
        class CachedJoel:
            calls = []
            def chapter(self, edition, book, chapter):
                self.calls.append((edition, book, chapter))
                if (book, chapter) != (29, 2): raise AssertionError('No invented fixture lookup')
                return copy.deepcopy(envelopes[edition])
        class ArchiveClock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2026, 10, 7, 22, 29, tzinfo=timezone.utc)
        cached = CachedJoel()
        with patch('berean_translation.scripture_evidence.datetime', ArchiveClock):
            evidence = build_evidence(src, 'pt', contract, cached)
        self.assertEqual(cached.calls, [('kjv', 29, 2), ('almeida', 29, 2)])
        self.assertEqual(evidence['quotes'][0]['component_reference']['partial_marker'], 'a')
        self.assertEqual(evidence['quotes'][0]['printed_reference'], 'Joel 2:25a')
        self.assertEqual(evidence['quotes'][0]['component_source']['quote'], original_scope['source_quote'])
        proposed = copy.deepcopy(accepted)
        proposed['html'] = proposed['html'].replace('restituir-nos os anos que o gafanhoto devorou!',
            'restituir-[nos] os annos que comeu o gafanhoto!')
        self.assertNotEqual(proposed, accepted)
        target_scope = associate(proposed['html'], src['article']['id'], evidence=evidence)[2][0]
        target_operations = [omit(target, 0, 2), part(target, 2, 12),
            {**part(target, 12, 15), 'kind': 'replace', 'id': 'a1', 'authored_text': '[nos]'},
            omit(target, 15, 19), part(target, 19, 50),
            {'kind': 'punctuation', 'id': 'a2', 'at': 50, 'authored_text': '!'}, omit(target, 50)]
        claims = [{'quote_id': 'q1', 'block': target_scope['block'], 'start': target_scope['source_start'],
                   'end': target_scope['source_end'], 'operations': target_operations}]
        with self.assertRaisesRegex(ScriptureAttention, 'morphology_or_subword_replacement'):
            check_component_selections(evidence, proposed, claims, source=src, contract=contract)
        self.assertEqual(before, canonical([read_json(src_path), read_json(candidate_path)]))


if __name__ == '__main__': unittest.main()
