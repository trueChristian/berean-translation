"""Run the real manual Actions shell offline, then collect its captured request.

Only outbound HTTP is replaced in the shell subprocess. The workflow, CLI,
configuration validation, queue serialization and duplicate handling are real.
All provider lifecycle results below are fixtures, never live translations.
"""
from __future__ import annotations

import base64
import copy
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from berean_translation.collector import collect_window
from berean_translation.common import canonical, loads
from berean_translation.downstream import enqueue_hour, funding_ledger
from berean_translation.continuation import policy as continuation_policy
from berean_translation.validation import validate_repository
from support import REPO_ROOT, drive, queue, setup


REPOSITORY = 'trueChristian/berean-translation'
RUN_ID = '37045372894'
REQUEST_ID = 'gh-' + RUN_ID
WORKFLOW_REF = REPOSITORY + '/.github/workflows/ai-repair.yml@refs/heads/main'
FAILED_RUN_INPUTS = {
    'model': 'gpt-6-luna', 'review_model': 'gpt-6-luna',
    'max_articles': '3', 'budget_usd': '10', 'dry_run': 'false',
}

# sitecustomize is loaded before the CLI imports urllib.request.urlopen. Do not
# patch enqueue_github, github_request, validation, or the application modules.
# A socket audit hook makes an unexpected HTTP implementation fail closed too.
HTTP_STUB = r'''
import base64
import io
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

root = Path(os.environ['OFFLINE_QUEUE_CAPTURE'])
expected = os.environ['OFFLINE_QUEUE_URL']

def forbid_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        raise AssertionError('Real network access is forbidden in workflow tests')

sys.addaudithook(forbid_network)
if os.environ.get('OPENAI_API_KEY'):
    raise AssertionError('Workflow regression subprocess must not inherit an API key')
(root / 'stub-loaded').write_text('offline HTTP boundary installed')

def urlopen(request, *args, **kwargs):
    method = request.get_method() if hasattr(request, 'get_method') else 'GET'
    url = request.full_url if hasattr(request, 'full_url') else str(request)
    if (method, url) not in (('PUT', expected), ('GET', expected + '?ref=main')):
        raise AssertionError('Unexpected outbound request: ' + method + ' ' + url)
    if request.get_header('Authorization') != 'Bearer fixture-no-network':
        raise AssertionError('Only the test GitHub token may be used')
    body = json.loads(request.data) if request.data is not None else None
    with (root / 'http-calls.jsonl').open('a') as stream:
        stream.write(json.dumps({'method': method, 'url': url, 'body': body}) + '\n')
    stored = root / 'immutable-request.json'
    if method == 'PUT':
        if set(body) != {'message', 'branch', 'content'} or body['branch'] != 'main':
            raise AssertionError('Expected only a new immutable main-branch queue file')
        content = base64.b64decode(body['content'], validate=True)
        if stored.exists():
            raise HTTPError(url, 422, 'already exists', {}, None)
        stored.write_bytes(content)
        response = {'content': {'path': 'state/queue/gh-37045372894.json'},
                    'commit': {'sha': 'offline-queue-commit'}}
    else:
        if not stored.exists():
            raise HTTPError(url, 404, 'not found', {}, None)
        response = {'content': base64.b64encode(stored.read_bytes()).decode('ascii')}
    return io.BytesIO(json.dumps(response).encode('utf-8'))

urllib.request.urlopen = urlopen
'''


class ManualRepairWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'checkout'
        self.root.mkdir()
        (self.config, self.state, self.source, self.provider,
         self.git, self.engine) = setup(self.root)
        self.workflow = yaml.load(
            (REPO_ROOT / '.github/workflows/ai-repair.yml').read_text(),
            Loader=yaml.BaseLoader)
        steps = [step for step in self.workflow['jobs']['enqueue']['steps'] if 'run' in step]
        self.assertEqual(len(steps), 1, 'Exercise the workflow shell rather than a copied CLI command')
        self.step = steps[0]
        self.capture = self.directory / 'http-capture'
        self.capture.mkdir()
        self.stub = self.directory / 'test-pythonpath'
        self.stub.mkdir()
        (self.stub / 'sitecustomize.py').write_text(textwrap.dedent(HTTP_STUB))
        self.runtime_before = (self.root / 'config/runtime.json').read_bytes()
        self.repository_runtime_before = (REPO_ROOT / 'config/runtime.json').read_bytes()
        self.assert_hourly_disabled()
        # Keep all in-process fixture collector work offline as well.
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Real network access forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def assert_hourly_disabled(self):
        # This fixture deliberately pauses hourly work to verify independent
        # manual authorization. The repository policy is checked separately and
        # run_workflow still asserts that neither configuration file is changed.
        policy = self.config.runtime['automatic_downstream_recovery']
        self.assertIs(policy['enabled'], False)
        self.assertEqual(policy['total_budget_usd'], 0)

    def environment(self, inputs=None, context=None):
        # Whitelist process settings; do not inherit real tokens, proxy settings,
        # INPUT_* overrides, GitHub context or OPENAI_API_KEY from the test runner.
        env = {
            'PATH': os.environ.get('PATH', os.defpath),
            'PYTHONPATH': os.pathsep.join((str(self.stub), str(REPO_ROOT))),
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
            'OFFLINE_QUEUE_CAPTURE': str(self.capture),
            'OFFLINE_QUEUE_URL': 'https://api.github.com/repos/' + REPOSITORY
                + '/contents/state/queue/' + REQUEST_ID + '.json',
            'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': REPOSITORY,
            'GITHUB_RUN_ID': RUN_ID, 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_ACTOR': 'fixture-owner', 'GITHUB_WORKFLOW_REF': WORKFLOW_REF,
        }
        values = {name: spec['default'] for name, spec in
                  self.workflow['on']['workflow_dispatch']['inputs'].items()}
        values.update(inputs or {})
        github = {'token': 'fixture-no-network'}
        for field in ('repository', 'run_id', 'actor', 'ref', 'event_name', 'workflow_ref'):
            github[field] = env['GITHUB_' + field.upper()]

        def resolve(value):
            def replacement(match):
                source, key = match.groups()
                return str((values if source == 'inputs' else github)[key])
            resolved = re.sub(r'\$\{\{\s*(inputs|github)\.([a-z_]+)\s*\}\}', replacement, value)
            self.assertNotIn('${{', resolved, 'Unexpected expression needs explicit test support')
            return resolved

        for layer in (self.workflow, self.workflow['jobs']['enqueue'], self.step):
            env.update({key: resolve(value) for key, value in layer.get('env', {}).items()})
        for key, value in (context or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_workflow(self, inputs=None, context=None):
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', self.step['run']],
            cwd=self.root, env=self.environment(inputs, context),
            capture_output=True, text=True, timeout=30)
        self.assertTrue((self.capture / 'stub-loaded').exists(), result.stderr)
        self.assertEqual((self.root / 'config/runtime.json').read_bytes(), self.runtime_before)
        self.assertEqual((REPO_ROOT / 'config/runtime.json').read_bytes(), self.repository_runtime_before)
        self.assert_hourly_disabled()
        return result

    def captured(self):
        return loads((self.capture / 'immutable-request.json').read_bytes())

    def http_calls(self):
        path = self.capture / 'http-calls.jsonl'
        return [loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []

    def enqueue_failed_run(self):
        result = self.run_workflow(FAILED_RUN_INPUTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.captured()

    def seed_held_candidates(self):
        queue(self.state)

        def rejected(line):
            if ':review' in line['custom_id']:
                return {'score': 80, 'passed': False, 'findings': [{
                    'severity': 'major', 'location': 'p', 'source_quote': 'Faith',
                    'translation_quote': 'Wrong', 'suggested_fix': 'Preserve faith'}]}
            return self.provider.default_result(line)

        drive(self.engine, self.provider, rejected)
        self.assertEqual({task['status'] for task in self.state.tasks()}, {'not_ready'})
        self.assertFalse(self.state.projection(self.config)['articles'])
        self.original_tasks = copy.deepcopy(self.state.tasks())
        self.original_campaign = self.state.read('state/campaigns/request-1.json')

    def collect_captured(self, request, responder=None):
        self.state.write(f'state/queue/{request["id"]}.json', request)
        elapsed = 0

        def complete_after_wait(seconds):
            nonlocal elapsed
            elapsed += seconds
            self.provider.complete_all(responder)

        return collect_window(self.engine, wait_seconds=600, poll_seconds=60,
                              monotonic=lambda: elapsed, sleep=complete_after_wait)

    def test_failed_run_actual_actions_shell_enqueues_its_own_ten_dollar_envelope(self):
        request = self.enqueue_failed_run()
        self.assertEqual(request, {
            'id': REQUEST_ID, 'operation': 'repair', 'model': 'gpt-6-luna',
            'review_model': 'gpt-6-luna', 'budget_usd': '10', 'max_articles': 3,
            'dry_run': False, 'requested_by': 'fixture-owner',
            'continuation_policy': continuation_policy(),
            'manual_authorization': {
                'kind': 'github_workflow_dispatch', 'repository': REPOSITORY,
                'workflow_ref': WORKFLOW_REF, 'run_id': RUN_ID, 'actor': 'fixture-owner'},
        })
        self.assertEqual((self.capture / 'immutable-request.json').read_bytes(), canonical(request) + b'\n')
        calls = self.http_calls()
        self.assertEqual([call['method'] for call in calls], ['PUT'])
        self.assertEqual(loads(base64.b64decode(calls[0]['body']['content'])), request)
        self.assertNotIn('sha', calls[0]['body'], 'Queue requests must never update existing files')
        self.assertFalse(self.state.campaigns())
        self.assertFalse(self.state.tasks())
        self.assertEqual(self.provider.create_calls, 0)

    def test_same_run_rerun_is_idempotent_and_different_inputs_cannot_overwrite(self):
        original = self.enqueue_failed_run()
        serialized = (self.capture / 'immutable-request.json').read_bytes()
        result = self.run_workflow(FAILED_RUN_INPUTS, {'GITHUB_RUN_ATTEMPT': '2'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(loads(result.stdout)['already_queued'], True)
        for changes in ({'budget_usd': '9'}, {'model': 'gpt-6.1-sol'},
                        {'review_model': 'gpt-6.1-sol'}, {'max_articles': '4'}):
            with self.subTest(changes=changes):
                result = self.run_workflow({**FAILED_RUN_INPUTS, **changes})
                self.assertEqual(result.returncode, 1)
                self.assertIn('different inputs', result.stderr)
                self.assertEqual(self.captured(), original)
                self.assertEqual((self.capture / 'immutable-request.json').read_bytes(), serialized)
        self.assertEqual([call['method'] for call in self.http_calls()], ['PUT'] + ['PUT', 'GET'] * 5)

    def test_untrusted_actions_context_fails_before_any_transport(self):
        contexts = [
            {'GITHUB_EVENT_NAME': event} for event in ('push', 'schedule', 'pull_request', None)
        ] + [
            {'GITHUB_REF': ref} for ref in ('refs/heads/feature', 'refs/tags/main', None)
        ] + [
            {'GITHUB_ACTIONS': value} for value in ('false', '', None)
        ] + [
            {'GITHUB_WORKFLOW_REF': value} for value in (
                REPOSITORY + '/.github/workflows/ai-worker.yml@refs/heads/main',
                WORKFLOW_REF.replace('@refs/heads/main', '@refs/heads/feature'), None)
        ] + [
            {'GITHUB_RUN_ID': value} for value in ('', '0', '01', '-1', 'not-a-run', '1/2', None)
        ] + [
            {'GITHUB_REPOSITORY': value} for value in ('', 'another/repository', 'invalid', None)
        ] + [
            {'GITHUB_REPOSITORY': value,
             'GITHUB_WORKFLOW_REF': value + '/.github/workflows/ai-repair.yml@refs/heads/main'}
            for value in ('invalid', '../repository', 'owner/repository/extra')
        ] + [
            {'GITHUB_ACTOR': value} for value in ('', 'invalid/actor', None)
        ]
        for context in contexts:
            with self.subTest(context=context):
                result = self.run_workflow(FAILED_RUN_INPUTS, context)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('ERROR:', result.stderr)
                self.assertEqual(self.http_calls(), [])
                self.assertFalse((self.capture / 'immutable-request.json').exists())

    def test_offline_guard_allows_no_other_http_or_socket_transport(self):
        # Verify the boundary without attempting a real socket operation, even
        # if the audit-hook implementation accidentally regresses.
        probe = '''
import sitecustomize
import sys
import urllib.request
from urllib.request import Request
assert urllib.request.urlopen is sitecustomize.urlopen
for method, url in (
    ('POST', sitecustomize.expected),
    ('GET', 'https://api.openai.com/v1/batches'),
    ('PUT', sitecustomize.expected.replace('berean-translation', 'another-repository')),
):
    try:
        urllib.request.urlopen(Request(url, method=method))
    except AssertionError:
        pass
    else:
        raise AssertionError('Unexpected HTTP destination was not blocked')
for event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
    try:
        sys.audit(event, None)
    except AssertionError:
        pass
    else:
        raise AssertionError('Unexpected socket operation was not blocked')
'''
        result = subprocess.run(['python3', '-c', textwrap.dedent(probe)], cwd=self.root,
                                env=self.environment(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.http_calls(), [])
        self.assertFalse((self.capture / 'immutable-request.json').exists())

    def test_input_flags_cannot_manufacture_manual_authorization(self):
        result = self.run_workflow(FAILED_RUN_INPUTS, {
            'GITHUB_ACTIONS': 'false', 'GITHUB_EVENT_NAME': 'schedule',
            'INPUT_MANUAL_AUTHORIZATION': 'true', 'INPUT_MANUAL_BUDGET_APPROVED': 'true',
            'INPUT_APPROVED_TOTAL_USD': '10', 'INPUT_ENABLE_HOURLY': 'true',
        })
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.http_calls(), [])

    def test_default_actions_preview_allocates_no_money_or_work(self):
        result = self.run_workflow()
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.captured()
        self.assertIs(request['dry_run'], True)
        self.seed_held_candidates()
        calls_before = self.provider.create_calls
        outcome = self.collect_captured(request)
        campaign = self.state.read(f'state/campaigns/{REQUEST_ID}.json')
        self.assertEqual(outcome['ticks'], 1)
        self.assertEqual(len(campaign['selection']), 2)
        self.assertEqual(campaign['skipped'], [])
        self.assertLessEqual(campaign['planned_cycle_ceiling_usd'], campaign['budget_usd'])
        self.assertEqual(campaign['tasks'], [])
        self.assertEqual(campaign['downstream_allocation_usd'], 0)
        self.assertEqual(campaign['reserved_usd'], 0)
        self.assertEqual(campaign['reported_usage_usd'], 0)
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 0)
        self.assertEqual(funding_ledger(self.state)['shared_policy_usd'], 0)
        self.assertEqual(self.state.tasks(), self.original_tasks)
        self.assertEqual(self.provider.create_calls, calls_before)
        self.assert_hourly_disabled()

    def test_enqueued_payload_collects_one_repair_and_review_then_publishes(self):
        request = self.enqueue_failed_run()
        self.seed_held_candidates()
        calls_before = self.provider.create_calls
        outcome = self.collect_captured(request)
        self.assertEqual(outcome['ticks'], 3)
        self.assertEqual(outcome['stop_reason'], 'no_submitted_batches')
        self.assertEqual(self.provider.create_calls - calls_before, 2)
        campaign = self.state.read(f'state/campaigns/{REQUEST_ID}.json')
        self.assertEqual(campaign['downstream_request'], request)
        self.assertEqual(campaign['funding_scope'], 'manual_workflow')
        self.assertEqual(campaign['downstream_allocation_usd'], 10)
        self.assertEqual(campaign['approved_total_usd'], 10)
        self.assertLessEqual(campaign['reserved_usd'], 10)
        self.assertLessEqual(campaign['reported_usage_usd'], 10)
        self.assertEqual(campaign['status'], 'finished')
        self.assertEqual(len(campaign['tasks']), 2)
        self.assertLessEqual(len(campaign['tasks']), request['max_articles'])
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual(task['status'], 'complete')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
            submitted = []
            for batch in self.provider.batches.values():
                if batch['metadata']['campaign'] == REQUEST_ID:
                    submitted.extend(loads(line) for line in self.provider.files[batch['input_file_id']].splitlines()
                                     if loads(line)['custom_id'].startswith(identity + ':'))
            self.assertEqual([line['custom_id'].split(':')[1] for line in submitted], ['correct', 'review2'])
            repair, review = [loads(line['body']['messages'][1]['content']) for line in submitted]
            self.assertIn('correction_findings', repair)
            self.assertNotIn('correction_findings', review)
            self.assertNotIn('rejection_reason', review)
            self.assertTrue(all(line['body']['model'] == 'gpt-6-luna' for line in submitted))
        for original in self.original_tasks:
            self.assertEqual(self.state.read(f'state/tasks/{original["id"]}/task.json'), original)
        self.assertEqual(self.state.read('state/campaigns/request-1.json'), self.original_campaign)
        self.assertEqual(validate_repository(self.config)['ready'], 2)
        for article in self.state.projection(self.config)['articles']:
            self.assertFalse(article['human_reviewed'])
            self.assertTrue(article['ai_notice_required'])
            self.assertIn('data-translation-notice="ai"', self.state.path(article['html']).read_text())
        funding = funding_ledger(self.state)
        self.assertEqual(funding['manual_workflow_usd'], 10)
        self.assertEqual(funding['shared_policy_usd'], 0)
        queue_before = sorted(path.name for path in self.state.path('state/queue').glob('*.json'))
        enqueue_hour(self.engine)
        collect_window(self.engine)
        self.assertEqual(sorted(path.name for path in self.state.path('state/queue').glob('*.json')), queue_before)
        self.assertFalse(list(self.state.path('state/queue').glob('downstream-*.json')))
        self.assertEqual(self.provider.create_calls - calls_before, 2)
        self.assert_hourly_disabled()

    def test_failed_independent_review_has_no_second_correction_or_hourly_retry(self):
        request = self.enqueue_failed_run()
        self.seed_held_candidates()
        calls_before = self.provider.create_calls

        def rejected(line):
            if ':review' in line['custom_id']:
                return {'score': 80, 'passed': False, 'findings': []}
            return self.provider.default_result(line)

        self.collect_captured(request, rejected)
        campaign = self.state.read(f'state/campaigns/{REQUEST_ID}.json')
        self.assertEqual(campaign['status'], 'finished')
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            self.assertEqual(task['status'], 'not_ready')
            self.assertEqual((task['translation_attempts'], task['review_attempts']), (1, 1))
        self.assertEqual(validate_repository(self.config)['ready'], 0)
        self.assertEqual(self.provider.create_calls - calls_before, 2)
        collect_window(self.engine)
        self.assertEqual(self.provider.create_calls - calls_before, 2)
        self.assertFalse(list(self.state.path('state/queue').glob('downstream-*.json')))
        self.assertEqual(funding_ledger(self.state)['manual_workflow_usd'], 10)
        self.assertEqual(funding_ledger(self.state)['shared_policy_usd'], 0)
        self.assert_hourly_disabled()


    def test_old_workflow_rerun_keeps_legacy_request_bytes(self):
        # GitHub reruns the original workflow YAML. Its absent version variable
        # preserves the old queue contract even though checkout reads new main.
        context = {'TRANSLATION_CONTINUATION_VERSION': None, 'INPUT_MAX_CANDIDATE_BYTES': None}
        first = self.run_workflow(FAILED_RUN_INPUTS, context)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn('continuation_policy', self.captured())
        frozen = (self.capture / 'immutable-request.json').read_bytes()
        again = self.run_workflow(FAILED_RUN_INPUTS, {**context, 'GITHUB_RUN_ATTEMPT': '2'})
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(loads(again.stdout)['already_queued'])
        self.assertEqual((self.capture / 'immutable-request.json').read_bytes(), frozen)

    def test_candidate_cap_is_explicit_frozen_and_invalid_caps_fail_before_network(self):
        for value in ('0', '1000001', 'true', '-1', '1.5'):
            with self.subTest(value=value):
                result = self.run_workflow({'max_candidate_bytes': value})
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.http_calls(), [])
        result = self.run_workflow({'max_candidate_bytes': '240000'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.captured()['continuation_policy'], continuation_policy(240000))


if __name__ == '__main__':
    unittest.main()
