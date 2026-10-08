"""Offline component request/replay tests: fake evidence and fake Batch rows only."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from berean_translation.common import ContractError, canonical, json_hash
from berean_translation.requests import reserve_cost
from berean_translation.scripture_component_processing import (
    OfflineComponentCycle, processing_contract, response_schemas,
    validate_processing_contract)
from berean_translation.scripture_evidence import build_evidence
from berean_translation.scripture_provider import GetBibleMCP
from support import setup, queue
from test_scripture_component_evidence import example
from test_scripture_evidence import FakeMCP


def verdict(*, score=98, passed=True, complete=True, severity=None):
    findings = [] if severity is None else [{
        'severity': severity, 'location': 'title', 'source_quote': 'Teaching',
        'translation_quote': 'Lehre', 'suggested_fix': 'Use Unterweisung.'}]
    return {'score': score, 'passed': passed, 'findings': findings,
            'findings_complete': complete}


def provider_row(request, result):
    return {'custom_id': request['line']['custom_id'], 'error': None,
            'response': {'status_code': 200, 'request_id': 'offline-request',
                         'body': {'model': request['line']['body']['model'],
                                  'id': 'offline-response',
                                  'choices': [{'finish_reason': 'stop', 'message': {
                                      'content': json.dumps(result, ensure_ascii=False),
                                      'refusal': None}}],
                                  'usage': {'prompt_tokens': 200, 'completion_tokens': 100}}}}


class OfflineComponentProcessingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.config, self.state, _, self.paid, _, self.engine) = setup(
            self.root, review_contract_version=2)
        self.engine.discover()
        self.campaign = self.engine.accept_request(queue(self.state, languages='deu'))
        self.task = self.state.read(f'state/tasks/{self.campaign["tasks"][0]}/task.json')
        self.source, self.policy, self.candidate, self.selections = example()
        # These are private fixture copies, never edits of committed runtime state.
        self.task['source_snapshot'] = f'state/sources/{json_hash(self.source)}.json'
        self.campaign['scripture_quotes'] = copy.deepcopy(self.policy)
        self.fake = FakeMCP()
        self.evidence = build_evidence(self.source, 'de', self.policy, GetBibleMCP(self.fake))
        self.contract = processing_contract(self.root, self.campaign, self.task)
        self.cycle = self.new_cycle()
        self.remote_guard = patch.object(GetBibleMCP, '_remote',
            side_effect=AssertionError('Offline processing must never access the provider'))
        self.remote_guard.start()
        self.addCleanup(self.remote_guard.stop)

    def new_cycle(self, contract=None):
        return OfflineComponentCycle(self.source, self.evidence, self.policy,
                                     self.contract if contract is None else contract)

    def generation(self, request, candidate=None, selections=None):
        return provider_row(request, {
            'binding': copy.deepcopy(request['binding']),
            'candidate': copy.deepcopy(self.candidate if candidate is None else candidate),
            'scripture_selections': copy.deepcopy(self.selections if selections is None else selections)})

    def review(self, request, report=None):
        return provider_row(request, {'binding': copy.deepcopy(request['binding']),
                                     'review': verdict() if report is None else report})

    def to_review(self, cycle=None, candidate=None):
        cycle = self.cycle if cycle is None else cycle
        request = cycle.request()
        cycle.receive(self.generation(request, candidate))
        return cycle.request()

    def to_final_review(self, cycle=None):
        cycle = self.cycle if cycle is None else cycle
        review = self.to_review(cycle)
        cycle.receive(self.review(review, verdict(score=90, passed=False, severity='major')))
        correction = cycle.request()
        corrected = copy.deepcopy(self.candidate)
        corrected['title'] = 'Unterweisung'
        cycle.receive(self.generation(correction, corrected))
        return cycle.request()

    def assert_held(self, cycle, row):
        snapshot = cycle.receive(row)
        self.assertEqual(snapshot['status'], 'held')
        self.assertTrue(snapshot['failure'])
        self.assertFalse(snapshot['publication_authorized'])
        with self.assertRaises(ContractError):
            cycle.request()
        return snapshot

    def test_happy_path_is_two_offline_requests_without_runtime_writes_or_paid_calls(self):
        files = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        review = self.to_review()
        final = self.cycle.receive(self.review(review))
        self.assertEqual(final['status'], 'review_accepted_offline')
        self.assertEqual((final['generation_attempts'], final['review_attempts']), (1, 1))
        self.assertEqual(final['candidate'], self.candidate)
        self.assertEqual(final['selections'], self.selections)
        self.assertEqual(final['selection_proof']['candidate_sha256'], json_hash(self.candidate))
        self.assertTrue(final['offline_only'])
        self.assertFalse(final['funding_authorized'])
        self.assertFalse(final['publication_authorized'])
        self.assertEqual((self.paid.upload_calls, self.paid.create_calls), (0, 0))
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(files, {str(p.relative_to(self.root)): p.read_bytes()
                                for p in self.root.rglob('*') if p.is_file()})
        with self.assertRaises(ContractError): self.cycle.request()

    def test_full_four_stage_path_binds_corrected_candidate_and_stops(self):
        review = self.to_final_review()
        before = self.cycle.snapshot()
        self.assertEqual([r['stage'] for r in before['records']],
                         ['translate', 'review1', 'correct', 'review2'])
        self.assertEqual([r['binding']['ordinal'] for r in before['records']], [1, 2, 3, 4])
        self.assertEqual((before['generation_attempts'], before['review_attempts']), (2, 2))
        self.assertEqual(before['candidate']['title'], 'Unterweisung')
        self.assertEqual(review['binding']['candidate_sha256'], json_hash(before['candidate']))
        final = self.cycle.receive(self.review(review))
        self.assertEqual(final['status'], 'review_accepted_offline')
        self.assertEqual(OfflineComponentCycle.restore(final).snapshot(), final)
        with self.assertRaises(ContractError): self.cycle.request()

    def test_failed_final_review_has_no_fifth_request_or_second_correction(self):
        review = self.to_final_review()
        final = self.assert_held(self.cycle, self.review(
            review, verdict(score=99, passed=True, severity='critical')))
        self.assertEqual(len(final['records']), 4)
        self.assertEqual((final['generation_attempts'], final['review_attempts']), (2, 2))
        self.assertEqual(OfflineComponentCycle.restore(final).snapshot(), final)

    def test_incomplete_review_cannot_pass_or_authorize_correction(self):
        for findings in (None, 'major'):
            with self.subTest(findings=findings):
                cycle = self.new_cycle()
                review = self.to_review(cycle)
                final = self.assert_held(cycle, self.review(
                    review, verdict(score=99, complete=False, severity=findings)))
                self.assertEqual(len(final['records']), 2)

    def test_high_score_major_or_critical_findings_never_pass(self):
        for severity in ('major', 'critical'):
            with self.subTest(severity=severity):
                cycle = self.new_cycle()
                review = self.to_review(cycle)
                snapshot = cycle.receive(self.review(review, verdict(severity=severity)))
                self.assertEqual(snapshot['status'], 'awaiting_request')
                self.assertEqual(snapshot['stage'], 'correct')
                self.assertFalse(snapshot['publication_authorized'])

    def test_failed_review_without_findings_does_not_invent_a_correction(self):
        review = self.to_review()
        self.assert_held(self.cycle, self.review(review, verdict(score=94, passed=False)))

    def test_review_score_and_complete_schema_are_validated(self):
        changes = ('missing_complete', 'extra', 'bad_score', 'boolean_score',
                   'bad_complete', 'bad_severity', 'incomplete_finding', 'too_many')
        for change in changes:
            with self.subTest(change=change):
                cycle = self.new_cycle()
                review = self.to_review(cycle)
                report = verdict()
                if change == 'missing_complete': del report['findings_complete']
                elif change == 'extra': report['candidate'] = self.candidate
                elif change == 'bad_score': report['score'] = 101
                elif change == 'boolean_score': report['score'] = True
                elif change == 'bad_complete': report['findings_complete'] = 1
                else:
                    report = verdict(severity='minor')
                    if change == 'bad_severity': report['findings'][0]['severity'] = 'unknown'
                    elif change == 'incomplete_finding': del report['findings'][0]['source_quote']
                    else: report['findings'] *= 31
                self.assert_held(cycle, self.review(review, report))

    def test_refusals_truncation_invalid_json_and_missing_results_fail_closed(self):
        for stage in ('translate', 'review1'):
            for change in ('refusal', 'truncated', 'missing', 'ambiguous', 'invalid_json',
                           'non_object', 'http_error', 'provider_error', 'missing_model'):
                with self.subTest(stage=stage, change=change):
                    cycle = self.new_cycle()
                    request = cycle.request() if stage == 'translate' else self.to_review(cycle)
                    row = self.generation(request) if stage == 'translate' else self.review(request)
                    body = row['response']['body']
                    if change == 'refusal': body['choices'][0]['message']['refusal'] = 'Refused.'
                    elif change == 'truncated': body['choices'][0]['finish_reason'] = 'length'
                    elif change == 'missing': body['choices'] = []
                    elif change == 'ambiguous': body['choices'] *= 2
                    elif change == 'invalid_json': body['choices'][0]['message']['content'] = '{'
                    elif change == 'non_object': body['choices'][0]['message']['content'] = '[]'
                    elif change == 'http_error': row['response']['status_code'] = 500
                    elif change == 'provider_error': row['error'] = {'code': 'provider_error'}
                    else: del body['model']
                    self.assert_held(cycle, row)

    def test_malformed_falsey_refusals_and_noninteger_http_statuses_fail_closed(self):
        for refusal in (False, [], {}, 0, ''):
            with self.subTest(refusal=refusal):
                cycle = self.new_cycle()
                request = cycle.request()
                row = self.generation(request)
                row['response']['body']['choices'][0]['message']['refusal'] = refusal
                self.assert_held(cycle, row)
        cycle = self.new_cycle()
        request = cycle.request()
        row = self.generation(request); row['response']['status_code'] = 200.0
        self.assert_held(cycle, row)

    def test_actual_response_model_must_match_this_frozen_request(self):
        for stage in ('translate', 'review1'):
            with self.subTest(stage=stage):
                cycle = self.new_cycle()
                request = cycle.request() if stage == 'translate' else self.to_review(cycle)
                row = self.generation(request) if stage == 'translate' else self.review(request)
                row['response']['body']['model'] = 'another-model'
                self.assert_held(cycle, row)

    def test_every_binding_field_and_exact_wrapper_keys_are_enforced(self):
        for key in self.cycle.request()['binding']:
            with self.subTest(key=key):
                cycle = self.new_cycle()
                request = cycle.request()
                row = self.generation(request)
                result = json.loads(row['response']['body']['choices'][0]['message']['content'])
                result['binding'][key] = 'different'
                row['response']['body']['choices'][0]['message']['content'] = json.dumps(result)
                self.assert_held(cycle, row)
        for change in ('missing', 'extra'):
            with self.subTest(wrapper=change):
                cycle = self.new_cycle()
                request = cycle.request()
                result = {'binding': request['binding'], 'candidate': self.candidate,
                          'scripture_selections': self.selections}
                if change == 'missing': del result['scripture_selections']
                else: result['score'] = 98
                self.assert_held(cycle, provider_row(request, result))

    def test_binding_ordinal_requires_exact_integer_not_boolean_or_float(self):
        for value in (True, 1.0):
            with self.subTest(value=value, type=type(value).__name__):
                cycle = self.new_cycle()
                request = cycle.request()
                binding = copy.deepcopy(request['binding']); binding['ordinal'] = value
                self.assert_held(cycle, provider_row(request, {
                    'binding': binding, 'candidate': self.candidate,
                    'scripture_selections': self.selections}))

    def test_changed_or_unsafe_candidate_and_selection_fail_before_review(self):
        for change in ('words', 'reference', 'unsafe_html', 'metadata', 'proof'):
            with self.subTest(change=change):
                cycle = self.new_cycle()
                request = cycle.request()
                candidate, selections = copy.deepcopy(self.candidate), copy.deepcopy(self.selections)
                if change == 'words': candidate['html'] = candidate['html'].replace('Mann', 'Frau')
                elif change == 'reference': candidate['html'] = candidate['html'].replace('4:16', '4:15')
                elif change == 'unsafe_html': candidate['html'] += '<script>alert(1)</script>'
                elif change == 'metadata': candidate['title'] = ''
                else: selections[0]['operations'][1]['id'] = 'another-component'
                self.assert_held(cycle, self.generation(request, candidate, selections))

    def test_old_high_score_cannot_be_adopted_for_another_stage(self):
        first = self.to_review()
        old_approval = self.review(first)
        self.cycle.receive(self.review(first, verdict(score=90, passed=False, severity='major')))
        correction = self.cycle.request()
        self.cycle.receive(self.generation(correction))
        final = self.cycle.request()
        old_approval['custom_id'] = final['line']['custom_id']
        self.assert_held(self.cycle, old_approval)

    def test_old_high_score_cannot_be_adopted_for_a_changed_candidate(self):
        first = self.to_review()
        old_approval = self.review(first)
        changed = copy.deepcopy(self.candidate)
        changed['title'] = 'Unterweisung'
        other = self.new_cycle()
        other_review = self.to_review(other, changed)
        self.assertNotEqual(first['binding']['candidate_sha256'], other_review['binding']['candidate_sha256'])
        self.assertNotEqual(first['binding']['selection_proof_sha256'],
                            other_review['binding']['selection_proof_sha256'])
        self.assert_held(other, old_approval)

    def test_review_cannot_replace_candidate_even_with_valid_binding(self):
        request = self.to_review()
        self.assert_held(self.cycle, provider_row(request, {
            'binding': request['binding'], 'review': verdict(), 'candidate': self.candidate}))
        self.assertEqual(self.cycle.snapshot()['candidate'], self.candidate)

    def test_every_request_contains_full_context_and_exact_budgeted_schema_binding(self):
        self.to_final_review()
        snapshot = self.cycle.snapshot()
        for record in snapshot['records']:
            with self.subTest(stage=record['stage']):
                line, binding = record['line'], record['binding']
                body = line['body']
                payload = json.loads(body['messages'][1]['content'])
                self.assertEqual(payload['source'], {'html': self.source['html'], **{
                    key: self.source['article'][key] for key in ('title', 'subtitle', 'section')}})
                self.assertEqual(payload['scripture_evidence'], self.evidence)
                self.assertEqual(payload['expected_binding'], binding)
                self.assertEqual(binding['source_sha256'], json_hash(self.source))
                self.assertEqual(binding['evidence_sha256'], json_hash(self.evidence))
                self.assertEqual(binding['processing_contract_sha256'], json_hash(self.contract))
                self.assertEqual(record['request_sha256'], json_hash(line))
                self.assertEqual((line['method'], line['url']), ('POST', '/v1/chat/completions'))
                schema_kind = 'review' if record['stage'].startswith('review') else 'generation'
                model_name = self.contract['review_model' if schema_kind == 'review' else 'generation_model']
                model = self.contract['models'][model_name]
                self.assertEqual(body['response_format']['json_schema']['schema'],
                                 self.contract['schemas'][schema_kind])
                self.assertTrue(body['response_format']['json_schema']['strict'])
                self.assertEqual(body['messages'][0]['content'], self.contract['prompts'][schema_kind])
                self.assertEqual(record['input_bound'], len(canonical(body)) + 4096)
                self.assertEqual(record['estimated_usd'], reserve_cost(
                    model, record['input_bound'], body['max_completion_tokens']))
                unsigned = copy.deepcopy(body)
                del payload['expected_binding']
                unsigned['messages'][1]['content'] = canonical(payload).decode()
                self.assertEqual(binding['input_sha256'], json_hash(unsigned))
                self.assertGreater(len(canonical(body)), len(canonical(unsigned)))
                no_schema = copy.deepcopy(body); del no_schema['response_format']
                self.assertGreater(record['input_bound'], len(canonical(no_schema)) + 4096)
                if record['stage'] == 'translate':
                    self.assertNotIn('translation', payload)
                    self.assertIsNone(binding['candidate_sha256'])
                else:
                    self.assertEqual(binding['candidate_sha256'], json_hash(payload['translation']))
                    self.assertEqual(binding['selection_proof_sha256'],
                                     json_hash(payload['scripture_selection_audit']))
                    self.assertEqual(payload['scripture_selections'], self.selections)
                if record['stage'] == 'correct':
                    self.assertEqual(payload['correction_findings'],
                                     verdict(score=90, passed=False, severity='major')['findings'])

    def test_request_is_idempotent_and_returned_copies_cannot_mutate_history(self):
        request = self.cycle.request()
        original = copy.deepcopy(request)
        frozen = self.cycle.snapshot()
        self.assertEqual(request, self.cycle.request())
        request['line']['body']['messages'][0]['content'] = 'tampered'
        request['binding']['ordinal'] = 100
        request['line']['body']['response_format']['json_schema']['schema'].clear()
        frozen['context']['source']['html'] = 'changed'
        self.assertEqual(self.cycle.request(), original)
        self.assertEqual(self.cycle.snapshot()['records'][0], original)
        self.assertEqual(self.cycle.snapshot()['generation_attempts'], 1)
        self.assertEqual(self.cycle.snapshot()['context']['source'], self.source)

    def test_constructor_copies_all_input_context(self):
        source, evidence, policy, contract = copy.deepcopy(
            (self.source, self.evidence, self.policy, self.contract))
        cycle = OfflineComponentCycle(source, evidence, policy, contract)
        frozen = cycle.snapshot()
        source['html'] = 'changed'
        evidence['quotes'].clear()
        policy['authored_components']['plans'].clear()
        contract['prompts']['generation'] = 'changed'
        self.assertEqual(cycle.snapshot(), frozen)

    def test_terminal_snapshot_candidate_proof_review_and_records_are_copies(self):
        request = self.to_review()
        self.cycle.receive(self.review(request))
        original = self.cycle.snapshot()
        changed = self.cycle.snapshot()
        changed['candidate']['title'] = 'changed'
        changed['selection_proof'].clear()
        changed['review']['score'] = 0
        changed['records'].clear()
        changed['selections'].clear()
        self.assertEqual(self.cycle.snapshot(), original)

    def test_response_is_copied_and_exact_duplicate_is_idempotent(self):
        request = self.cycle.request()
        row = self.generation(request)
        first = self.cycle.receive(row)
        self.assertEqual(self.cycle.receive(copy.deepcopy(row)), first)
        row['response']['body']['choices'][0]['message']['content'] = '{}'
        self.assertEqual(self.cycle.snapshot(), first)
        with self.assertRaises(ContractError): self.cycle.receive(row)
        final_request = self.cycle.request()
        final_row = self.review(final_request)
        final = self.cycle.receive(final_row)
        self.assertEqual(self.cycle.receive(copy.deepcopy(final_row)), final)

    def test_response_idempotency_does_not_accept_changed_numeric_types(self):
        request = self.cycle.request()
        row = self.generation(request)
        first = self.cycle.receive(row)
        changed = copy.deepcopy(row)
        changed['response']['body']['usage']['prompt_tokens'] = 200.0
        with self.assertRaises(ContractError): self.cycle.receive(changed)
        self.assertEqual(self.cycle.snapshot(), first)

    def test_wrong_response_id_cannot_consume_or_overwrite_pending_request(self):
        request = self.cycle.request()
        before = self.cycle.snapshot()
        row = self.generation(request)
        row['custom_id'] = 'different-request'
        with self.assertRaises(ContractError): self.cycle.receive(row)
        self.assertEqual(self.cycle.snapshot(), before)
        self.cycle.receive(self.generation(request))
        next_request = self.cycle.request()
        pending = self.cycle.snapshot()
        with self.assertRaises(ContractError): self.cycle.receive(self.generation(request))
        self.assertEqual(self.cycle.snapshot(), pending)
        self.assertEqual(self.cycle.request(), next_request)

    def test_restore_replays_pending_and_completed_snapshots_exactly(self):
        snapshots = [self.cycle.snapshot()]
        generation = self.cycle.request(); snapshots.append(self.cycle.snapshot())
        self.cycle.receive(self.generation(generation)); snapshots.append(self.cycle.snapshot())
        review = self.cycle.request(); snapshots.append(self.cycle.snapshot())
        self.cycle.receive(self.review(review)); snapshots.append(self.cycle.snapshot())
        for snapshot in snapshots:
            with self.subTest(status=snapshot['status'], records=len(snapshot['records'])):
                restored = OfflineComponentCycle.restore(copy.deepcopy(snapshot))
                self.assertEqual(restored.snapshot(), snapshot)
                if snapshot['status'] == 'awaiting_response':
                    self.assertEqual(restored.request(), snapshot['records'][-1])

    def test_snapshot_tampering_attempt_resets_and_extra_requests_are_rejected(self):
        final_review = self.to_final_review()
        final = self.cycle.receive(self.review(final_review))
        for change in ('context', 'context_hash', 'request', 'response', 'candidate', 'proof',
                       'review', 'status', 'attempts', 'reset', 'fifth', 'version', 'extra'):
            with self.subTest(change=change):
                snapshot = copy.deepcopy(final)
                if change == 'context': snapshot['context']['source']['html'] += '!'
                elif change == 'context_hash': snapshot['context_sha256'] = 'a' * 64
                elif change == 'request': snapshot['records'][0]['line']['body']['model'] = 'different'
                elif change == 'response': snapshot['records'][0]['response']['response']['body']['id'] += '!'
                elif change == 'candidate': snapshot['candidate']['title'] += '!'
                elif change == 'proof': snapshot['selection_proof']['candidate_sha256'] = 'a' * 64
                elif change == 'review': snapshot['review']['score'] = 100
                elif change == 'status': snapshot['status'] = 'awaiting_request'
                elif change == 'attempts': snapshot['generation_attempts'] = 0
                elif change == 'reset': snapshot['records'].pop(0)
                elif change == 'fifth': snapshot['records'].append(copy.deepcopy(snapshot['records'][0]))
                elif change == 'version': snapshot['version'] = '2'
                else: snapshot['adopted'] = True
                with self.assertRaises(ContractError): OfflineComponentCycle.restore(snapshot)

    def test_snapshot_numeric_type_tampering_cannot_replay_as_identical_history(self):
        self.cycle.request()
        original = self.cycle.snapshot()
        for change in ('attempts', 'ordinal', 'input_bound', 'offline_flag'):
            with self.subTest(change=change):
                snapshot = copy.deepcopy(original)
                if change == 'attempts': snapshot['generation_attempts'] = True
                elif change == 'ordinal': snapshot['records'][0]['binding']['ordinal'] = True
                elif change == 'input_bound':
                    snapshot['records'][0]['input_bound'] = float(snapshot['records'][0]['input_bound'])
                else:
                    snapshot['records'][0]['offline_only'] = 1
                with self.assertRaises(ContractError): OfflineComponentCycle.restore(snapshot)

    def test_expired_evidence_can_replay_but_never_refresh_or_start_new_request(self):
        self.to_review()
        snapshot = self.cycle.snapshot()
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch('berean_translation.scripture_evidence.datetime', FutureClock):
            restored = OfflineComponentCycle.restore(snapshot)
            self.assertEqual(restored.snapshot(), snapshot)
            pending = restored.request()
            restored.receive(self.review(pending, verdict(score=90, passed=False, severity='major')))
            with self.assertRaisesRegex(ContractError, 'evidence_expired'): restored.request()

    def test_no_processing_contract_for_old_attempts_batches_or_wrong_origin(self):
        changes = {'translation_attempts': 1, 'review_attempts': 1, 'batch': 'batch-existing',
                   'stage': 'correct', 'status': 'held', 'campaign': 'another',
                   'source_snapshot': 'state/sources/not-canonical.json'}
        for key, value in changes.items():
            with self.subTest(key=key):
                task = copy.deepcopy(self.task); task[key] = value
                before = copy.deepcopy(task)
                with self.assertRaises(ContractError): processing_contract(self.root, self.campaign, task)
                self.assertEqual(task, before)
        task = copy.deepcopy(self.task)
        task['models'][task['model']]['api_model'] = 'different'
        with self.assertRaises(ContractError): processing_contract(self.root, self.campaign, task)

    def test_attempt_counters_cannot_use_false_or_float_to_look_unattempted(self):
        for key in ('translation_attempts', 'review_attempts'):
            for value in (False, 0.0, -1, None):
                with self.subTest(key=key, value=value):
                    task = copy.deepcopy(self.task); task[key] = value
                    with self.assertRaises(ContractError):
                        processing_contract(self.root, self.campaign, task)

    def test_processing_requires_explicit_component_and_complete_review_versions(self):
        for key, value in (('review_contract_version', None), ('review_contract_version', 1),
                           ('scripture_quotes', None), ('scripture_quotes', {'version': '1'})):
            with self.subTest(key=key, value=value):
                campaign = copy.deepcopy(self.campaign)
                if value is None: del campaign[key]
                else: campaign[key] = value
                with self.assertRaises(ContractError): processing_contract(self.root, campaign, self.task)

    def test_actual_source_and_evidence_language_must_match_frozen_processing(self):
        for change in ('source_hash', 'article_id', 'language'):
            with self.subTest(change=change):
                contract = copy.deepcopy(self.contract)
                if change == 'source_hash': contract['source_sha256'] = 'a' * 64
                elif change == 'article_id': contract['article_id'] = 'another'
                else: contract['language_settings']['tag'] = 'fr'
                with self.assertRaises(ContractError): self.new_cycle(contract)

    def test_matching_substitute_policy_and_evidence_cannot_replace_frozen_campaign_policy(self):
        substitute = copy.deepcopy(self.policy)
        substitute['max_component_candidate_bytes'] -= 1
        evidence = build_evidence(self.source, 'de', substitute, GetBibleMCP(FakeMCP()))
        self.assertNotEqual(evidence, self.evidence)
        with self.assertRaises(ContractError):
            OfflineComponentCycle(self.source, evidence, substitute, self.contract)

    def test_response_schemas_are_strict_required_and_independent_copies(self):
        schemas = response_schemas()
        def check(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                    self.assertEqual(set(node['required']), set(node['properties']))
                for child in node.values(): check(child)
            elif isinstance(node, list):
                for child in node: check(child)
        check(schemas)
        self.assertEqual(set(schemas['generation']['properties']),
                         {'binding', 'candidate', 'scripture_selections'})
        self.assertEqual(set(schemas['review']['properties']), {'binding', 'review'})
        self.assertIn('findings_complete', schemas['review']['properties']['review']['required'])
        schemas['generation']['properties']['binding']['properties']['version']['enum'].append('999')
        self.assertNotEqual(schemas, response_schemas())
        self.assertEqual(response_schemas(), self.contract['schemas'])

    def test_unknown_contract_versions_activation_and_changed_frozen_fields_rejected(self):
        for change in ('version', 'schema_version', 'review_contract_version', 'limits',
                       'offline_only', 'funding_authorized', 'publication_authorized',
                       'extra', 'prompt', 'prompt_hash', 'schema', 'schema_hash', 'threshold',
                       'result_limit', 'context_limit', 'snapshot_limit'):
            with self.subTest(change=change):
                contract = copy.deepcopy(self.contract)
                if change in ('version', 'schema_version'): contract[change] = '999'
                elif change == 'review_contract_version': contract[change] = 1
                elif change == 'limits': contract['limits']['corrections'] = 2
                elif change == 'offline_only': contract[change] = False
                elif change in ('funding_authorized', 'publication_authorized'): contract[change] = True
                elif change == 'extra': contract['live'] = True
                elif change == 'prompt': contract['prompts']['generation'] += '!'
                elif change == 'prompt_hash': contract['prompts_sha256'] = 'a' * 64
                elif change == 'schema': contract['schemas']['generation']['additionalProperties'] = True
                elif change == 'schema_hash': contract['schemas_sha256'] = 'a' * 64
                elif change == 'threshold': contract['quality_threshold'] = 94
                elif change == 'result_limit': contract['maximum_result_bytes'] = 1000001
                elif change == 'context_limit': contract['maximum_context_bytes'] += 1
                else: contract['maximum_snapshot_bytes'] += 1
                with self.assertRaises(ContractError): validate_processing_contract(contract)

    def test_invalid_frozen_price_cannot_underestimate_the_offline_budget(self):
        for key in ('input_batch_usd_per_million', 'output_batch_usd_per_million',
                    'cached_input_batch_usd_per_million', 'cache_write_batch_usd_per_million'):
            with self.subTest(key=key):
                contract = copy.deepcopy(self.contract)
                contract['models'][contract['generation_model']][key] = -0.01
                with self.assertRaises(ContractError): validate_processing_contract(contract)

    def test_stage_model_reasoning_and_output_caps_use_frozen_settings(self):
        campaign, task = copy.deepcopy(self.campaign), copy.deepcopy(self.task)
        campaign['model'] = task['model'] = 'gpt-5-mini'
        campaign['review_model'] = task['review_model'] = 'gpt-4.1'
        contract = processing_contract(self.root, campaign, task)
        contract['models']['gpt-5-mini']['max_output_tokens'] = 500
        contract['models']['gpt-4.1']['max_output_tokens'] = 400
        cycle = self.new_cycle(contract)
        generation = cycle.request()
        self.assertEqual(generation['line']['body']['model'],
                         campaign['models']['gpt-5-mini']['api_model'])
        self.assertEqual(generation['line']['body']['reasoning_effort'], 'low')
        self.assertEqual(generation['line']['body']['max_completion_tokens'], 500)
        cycle.receive(self.generation(generation))
        review = cycle.request()
        self.assertEqual(review['line']['body']['model'], campaign['models']['gpt-4.1']['api_model'])
        self.assertNotIn('reasoning_effort', review['line']['body'])
        self.assertEqual(review['line']['body']['max_completion_tokens'], 400)
        self.assertEqual(review['estimated_usd'], reserve_cost(
            contract['models']['gpt-4.1'], review['input_bound'], 400))

    def test_prompts_models_language_and_glossary_are_frozen_copies(self):
        frozen = copy.deepcopy(self.contract)
        self.campaign['prompts']['translation'] = 'changed live prompt'
        self.campaign['models'][self.task['model']]['api_model'] = 'changed-live-model'
        self.campaign['language_settings']['deu']['guidance'] = 'changed guidance'
        self.campaign['glossaries']['deu'] = {'changed': 'glossary'}
        (self.root / 'prompts/scripture-components-generation-v1.txt').write_text('changed on disk')
        self.assertEqual(self.contract, frozen)
        request = self.cycle.request()
        self.assertEqual(request['line']['body']['messages'][0]['content'], frozen['prompts']['generation'])
        self.assertEqual(request['line']['body']['model'],
                         frozen['models'][frozen['generation_model']]['api_model'])

    def test_context_limit_fails_without_truncation_or_consuming_an_attempt(self):
        contract = copy.deepcopy(self.contract)
        contract['models'][contract['generation_model']]['context_tokens'] = 10
        cycle = self.new_cycle(contract)
        before = cycle.snapshot()
        with self.assertRaisesRegex(ContractError, 'never truncate'): cycle.request()
        self.assertEqual(cycle.snapshot(), before)

    def test_oversized_context_fails_at_construction_without_shortening_input(self):
        contract = copy.deepcopy(self.contract)
        contract['glossary'] = {'large': 'x' * contract['maximum_context_bytes']}
        before = copy.deepcopy(contract)
        with self.assertRaisesRegex(ContractError, 'selection_size_limit'): self.new_cycle(contract)
        self.assertEqual(contract, before)

    def test_oversized_result_and_unarchivable_response_hold_without_retry(self):
        contract = copy.deepcopy(self.contract); contract['maximum_result_bytes'] = 500
        cycle = self.new_cycle(contract)
        request = cycle.request()
        final = self.assert_held(cycle, self.generation(request))
        self.assertTrue(final['replayable'])
        self.assertEqual(OfflineComponentCycle.restore(final).snapshot(), final)
        cycle = self.new_cycle(contract)
        request = cycle.request()
        row = self.generation(request)
        row['padding'] = 'x' * 70000
        final = self.assert_held(cycle, row)
        self.assertFalse(final['replayable'])
        with self.assertRaises(ContractError): OfflineComponentCycle.restore(final)


if __name__ == '__main__':
    unittest.main()
