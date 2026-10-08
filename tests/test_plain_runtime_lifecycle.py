"""End-to-end plain translation and upgrade lifecycle using an offline provider."""
from __future__ import annotations

import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation import cli
from berean_translation.common import canonical, digest, json_hash, loads, read_json
from berean_translation.config import Config
from berean_translation.validation import validate_repository
from support import A, drive, queue, setup


class PlainRuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.config, self.state, self.upstream, self.provider,
         self.git, self.engine) = setup(self.root, review_contract_version=2)
        self.upstream.articles = self.upstream.articles[:1]
        self.upstream.rebuild()
    def candidate(self, payload, *, improved=False):
        source = payload['source']
        word = 'Glaube' if improved else 'Vertrauen'
        text = source['html'].replace('Faith and <em>grace</em>. John 3:16–18.',
                                     f'{word} und <em>Gnade</em>. Johannes 3,16–18.')
        text = text.replace('alt="A tree"', 'alt="Ein Baum"').replace('A tree.', 'Ein Baum.')
        return {'html': text, 'title': word, 'subtitle': source['subtitle'], 'section': 'Lehre'}

    def responder(self, *, score=95, improved=False, passed=True):
        def respond(line):
            if ':review' in line['custom_id']:
                return {'score': score, 'passed': passed, 'findings': [], 'findings_complete': True}
            return self.candidate(loads(line['body']['messages'][1]['content']), improved=improved)
        return respond

    def run_offline(self, responder, ticks=8):
        for _ in range(ticks):
            self.engine.tick(discover_source=False)
            self.provider.complete_all(responder)

    def publish_initial(self):
        queue(self.state, 'initial', languages='deu')
        drive(self.engine, self.provider, self.responder(), ticks=5)
        publication = self.state.record('deu', A)['published']
        self.assertEqual(publication['quality_score'], 95)
        return copy.deepcopy(publication)

    def latest_task(self):
        record = self.state.record('deu', A)
        return self.state.read(f'state/tasks/{record["latest_task"]}/task.json')

    def set_policy(self, *, legacy):
        runtime = read_json(self.root / 'config/runtime.json')
        runtime['prompt_version'] = '1.0.3' if legacy else '2.0.0'
        if legacy:
            runtime.pop('review_contract_version', None)
        else:
            runtime['review_contract_version'] = 2
        (self.root / 'config/runtime.json').write_bytes(canonical(runtime))
        self.config = Config(self.root)
        self.engine.config = self.config

    @staticmethod
    def frozen_contract(campaign):
        return {key: copy.deepcopy(value) for key, value in campaign.items()
                if key in ('request_sha256', 'budget_usd', 'models', 'model', 'review_model',
                           'prompts', 'prompt_version', 'quality_threshold', 'review_contract_version',
                           'max_output_tokens', 'review_output_tokens', 'scripture_quotes')}

    def test_plain_95_publication_skips_only_scripture_gate_and_keeps_four_field_candidate(self):
        publication = self.publish_initial()
        task = self.latest_task()
        campaign = self.state.read('state/campaigns/initial.json')
        self.assertNotIn('scripture_quotes', campaign)
        self.assertNotIn('accepted_baseline', task)
        candidate = self.state.candidate(task)
        self.assertEqual(set(candidate), {'html', 'title', 'subtitle', 'section'})
        self.assertIn('Johannes 3,16–18', candidate['html'])
        raw = self.state.read(f'state/tasks/{task["id"]}/results/translate.json')['result']
        self.assertEqual(raw, candidate)
        self.assertFalse(self.state.path(f'state/tasks/{task["id"]}/scripture-selections.json').exists())
        self.assertEqual(validate_repository(self.config)['published'], 1)
        for payload in self.provider.files.values():
            if not payload or not isinstance(payload, bytes):
                continue
            for row in payload.splitlines():
                data = loads(row)
                if 'body' in data:
                    request = loads(data['body']['messages'][1]['content'])
                    self.assertNotIn('scripture_evidence', request)
                    self.assertNotIn('scripture_selection_audit', request)

    def test_minor_only_false_verdict_can_publish_at_95(self):
        queue(self.state, 'false-verdict', languages='deu')

        def respond(line):
            result = self.responder(passed=False)(line)
            if ':review' in line['custom_id']:
                result['findings'] = [{'severity': 'minor', 'location': 'title',
                                       'source_quote': 'Faith', 'translation_quote': 'Vertrauen',
                                       'suggested_fix': 'Glaube'}]
            return result

        drive(self.engine, self.provider, respond, ticks=5)
        self.assertEqual(self.latest_task()['status'], 'complete')
        self.assertEqual(self.state.record('deu', A)['published']['quality_score'], 95)

    def test_final_upgrade_score_97_keeps_previous_publication_bytes_and_hashes(self):
        previous = self.publish_initial()
        original_bytes = self.state.path(previous['html_path']).read_bytes()
        queue(self.state, 'upgrade-97', operation='review', languages='deu', issues='all')
        self.run_offline(self.responder(score=97, improved=True))
        task = self.latest_task()
        self.assertEqual(task['status'], 'not_ready')
        self.assertEqual(task['review_attempts'], 2)
        self.assertIn('accepted_baseline', task)
        self.assertEqual(self.state.record('deu', A)['published'], previous)
        self.assertEqual(self.state.path(previous['html_path']).read_bytes(), original_bytes)
        self.assertFalse(self.state.path('state/publication-history').exists())
        self.assertEqual(validate_repository(self.config)['published'], 1)

    def test_corrected_upgrade_98_replaces_and_archives_exact_previous_publication(self):
        previous = self.publish_initial()
        original_html = self.state.path(previous['html_path']).read_text()
        original_metadata = self.state.read(previous['metadata_path'])
        queue(self.state, 'upgrade-98', operation='review', languages='deu', issues='all')

        def upgrade(line):
            if line['custom_id'].endswith(':review1'):
                return {'score': 96, 'passed': False, 'findings': [], 'findings_complete': True}
            return self.responder(score=98, improved=True)(line)

        self.run_offline(upgrade)
        task = self.latest_task()
        current = self.state.record('deu', A)['published']
        self.assertEqual(task['status'], 'complete')
        self.assertEqual(current['quality_score'], 98)
        self.assertNotEqual(current['task'], previous['task'])
        self.assertNotEqual(current['html_sha256'], previous['html_sha256'])
        self.assertEqual(self.state.read(current['metadata_path'])['title'], 'Glaube')
        paths = list(self.state.path(f'state/publication-history/deu/{A}').glob('*.json'))
        self.assertEqual(len(paths), 1)
        archived = read_json(paths[0])
        self.assertEqual(archived['publication'], previous)
        self.assertEqual(archived['html'], original_html)
        self.assertEqual(archived['metadata'], original_metadata)
        self.assertEqual(digest(archived['html']), previous['html_sha256'])
        self.assertEqual(json_hash(archived['metadata']), previous['metadata_sha256'])
        self.assertEqual(paths[0].stem, json_hash(archived))
        self.assertEqual(validate_repository(self.config)['published'], 1)

    def fail_upgrade_response(self, kind):
        previous = self.publish_initial()
        original_bytes = self.state.path(previous['html_path']).read_bytes()
        queue(self.state, 'bad-upgrade', operation='review', languages='deu', issues='all')
        self.engine.tick(discover_source=False)
        self.provider.complete_all(self.responder(score=100))
        completed = [batch for batch in self.provider.batches.values()
                     if batch.get('output_file_id') and any(
                         loads(line)['custom_id'].startswith(self.latest_task()['id'])
                         for line in self.provider.files[batch['output_file_id']].splitlines())]
        self.assertEqual(len(completed), 1)
        batch = completed[0]
        rows = [loads(line) for line in self.provider.files[batch['output_file_id']].splitlines()]
        message = rows[0]['response']['body']['choices'][0]['message']
        if kind == 'refusal':
            message.update(refusal='Simulated refusal', content=None)
        else:
            message['content'] = '{invalid JSON'
        self.provider.files[batch['output_file_id']] = b'\n'.join(canonical(row) for row in rows)
        self.engine.tick(discover_source=False)
        self.assertEqual(self.latest_task()['status'], 'not_ready')
        self.assertEqual(self.state.record('deu', A)['published'], previous)
        self.assertEqual(self.state.path(previous['html_path']).read_bytes(), original_bytes)
        self.assertEqual(validate_repository(self.config)['published'], 1)

    def test_malformed_upgrade_result_never_replaces_accepted_publication(self):
        self.fail_upgrade_response('malformed')

    def test_provider_refusal_never_replaces_accepted_publication(self):
        self.fail_upgrade_response('refusal')

    def test_human_edit_during_upgrade_protects_body_from_late_98_result(self):
        previous = self.publish_initial()
        queue(self.state, 'human-during-upgrade', operation='review', languages='deu', issues='all')
        self.engine.tick(discover_source=False)
        candidate, _, _ = self.state.publication_candidate(previous)
        human_body = candidate['html'].replace('Vertrauen und', 'Human authoritative text und')
        self.state.path(previous['html_path']).write_text(human_body)
        self.provider.complete_all(self.responder(score=98, improved=True))
        self.engine.tick(discover_source=False)
        record = self.state.record('deu', A)
        self.assertTrue(record['published']['human_reviewed'])
        self.assertEqual(self.state.publication_candidate(record['published'])[0]['html'], human_body)
        self.assertEqual(self.latest_task()['status'], 'proposal')
        calls = self.provider.create_calls
        queue(self.state, 'human-excluded', operation='review', languages='deu', issues='all')
        self.engine.tick(discover_source=False)
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.state.read('state/campaigns/human-excluded.json')['tasks'], [])

    def test_cli_no_discover_restores_seeded_cache_without_english_network_scan(self):
        self.engine.discover()
        self.assertTrue(self.state.path('state/source-snapshots.json').exists())
        with patch.dict(os.environ, {}, clear=True), \
                patch('berean_translation.cli.GitStore', return_value=self.git), \
                patch('berean_translation.source.SourceClient.discover',
                      side_effect=AssertionError('Collector must use the seeded source cache')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(['--root', str(self.root), 'tick', '--no-discover'])
        self.assertEqual(result, 0)
        self.assertIn('"api_key_configured": false', output.getvalue())
        self.assertEqual(self.state.read('state/last-collection.json')['operation'], 'collect')
        self.assertEqual(self.provider.create_calls, 0)

    def test_already_paid_legacy_translation_uses_plain_result_then_saved_candidate_recovery(self):
        self.set_policy(legacy=True)
        queue(self.state, 'legacy-translate', languages='deu')
        self.engine.tick()
        task = self.latest_task()
        self.assertEqual(task['stage'], 'translate')
        self.assertEqual(task['status'], 'in_batch')
        original_campaign = self.frozen_contract(self.state.read('state/campaigns/legacy-translate.json'))
        remote_batch = next(iter(self.provider.batches.values()))
        original_payload = self.provider.files[remote_batch['input_file_id']]
        self.provider.complete_all(self.responder())
        self.set_policy(legacy=False)

        # An already submitted result is collected under plain evaluation;
        # immutable legacy request bytes remain its provider provenance.
        self.engine.tick(discover_source=False)
        retired = self.state.read(f'state/tasks/{task["id"]}/task.json')
        self.assertEqual(retired['status'], 'not_ready')
        self.assertEqual(retired['failure_kind'], 'policy_retired')
        self.assertEqual(retired['translation_attempts'], 1)
        self.assertEqual(retired['review_attempts'], 0)
        candidate = self.state.candidate(retired)
        self.assertEqual(set(candidate), {'html', 'title', 'subtitle', 'section'})
        self.assertIn('Johannes 3,16–18', candidate['html'])
        raw = self.state.read(f'state/tasks/{task["id"]}/results/translate.json')['result']
        self.assertEqual(raw, candidate)
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(self.provider.files[remote_batch['input_file_id']], original_payload)
        self.assertEqual(self.frozen_contract(self.state.read('state/campaigns/legacy-translate.json')),
                         original_campaign)

        queue(self.state, 'plain-recover-saved', operation='review', languages='deu', issues='all')
        self.run_offline(self.responder())
        recovered = self.latest_task()
        self.assertEqual(recovered['status'], 'complete')
        self.assertEqual(recovered['translation_attempts'], 0)
        self.assertEqual(recovered['review_attempts'], 1)
        self.assertEqual(self.state.candidate(recovered), candidate)
        self.assertEqual(self.state.record('deu', A)['published']['quality_score'], 95)
        self.assertEqual(self.provider.create_calls, 2)
        self.assertEqual(self.state.read(f'state/tasks/{task["id"]}/task.json'), retired)

    def test_already_paid_legacy_review_can_publish_at_95_without_rewriting_its_contract(self):
        self.set_policy(legacy=True)
        queue(self.state, 'legacy-review', languages='deu')
        self.engine.tick()

        def legacy_translation(line):
            candidate = self.candidate(loads(line['body']['messages'][1]['content']))
            candidate['html'] = candidate['html'].replace('Johannes 3,16–18', 'Johannes 3:16–18')
            return candidate

        self.provider.complete_all(legacy_translation)
        self.engine.tick()
        task = self.latest_task()
        self.assertEqual(task['stage'], 'review1')
        self.assertEqual(task['status'], 'in_batch')
        original_campaign = self.frozen_contract(self.state.read('state/campaigns/legacy-review.json'))
        remote_batch = list(self.provider.batches.values())[-1]
        original_payload = self.provider.files[remote_batch['input_file_id']]
        self.provider.complete_all(lambda line: {'score': 95, 'passed': True, 'findings': []})
        self.set_policy(legacy=False)
        self.engine.tick(discover_source=False)
        completed = self.latest_task()
        publication = self.state.record('deu', A)['published']
        self.assertEqual(completed['status'], 'complete')
        self.assertEqual(publication['quality_score'], 95)
        self.assertEqual(publication['task'], task['id'])
        self.assertEqual(self.provider.files[remote_batch['input_file_id']], original_payload)
        self.assertEqual(self.frozen_contract(self.state.read('state/campaigns/legacy-review.json')),
                         original_campaign)
        self.assertEqual(validate_repository(self.config)['published'], 1)


if __name__ == '__main__':
    unittest.main()
