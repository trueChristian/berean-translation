"""Offline source-hook regressions; synthetic exports, mocked Git and HTTP only."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'trueChristian/berean-translation'
REVISION_KEY = 'translation_revision'
WORKFLOW = ROOT / '.github/workflows/ai-worker.yml'


def load_hook(name):
    spec = importlib.util.spec_from_file_location(
        '_remnant_' + name, ROOT / '.github/remnant' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


events = load_hook('validate_event')
# Load this repository's own helpers, without depending on the website checkout
# or leaving a generic module name on sys.path for other discovery tests.
with patch.dict(sys.modules, {'validate_event': events}):
    dispatch = load_hook('dispatch')


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.export = self.root / 'export'
        self.export.mkdir()
        self.state = self.root / 'marker/state.json'
        self.revision = 'a' * 40
        self.write('manifest.json', {REVISION_KEY: self.revision, 'omitted': []})
        self.write('index.json', {
            'source_revision': self.revision, 'translation_revision': self.revision,
            'articles': [{'id': 'synthetic', 'title': 'Example', 'human_reviewed': False}],
        })
        (self.export / 'article.html').write_text('<article>Example</article>', encoding='utf-8')
        (self.export / 'image.jpg').write_bytes(b'synthetic image bytes')
        (self.root / 'config').mkdir()
        self.registry = self.root / 'config/languages.json'
        self.registry.write_text(json.dumps({
            'afr': {'name': 'Afrikaans', 'native_name': 'Afrikaans',
                    'tag': 'af', 'dir': 'ltr', 'aliases': []},
        }), encoding='utf-8')
        self.request = Mock(return_value=(204, b''))
        # An accidental real HTTP call must fail instead of reaching the network.
        offline = patch('socket.create_connection', side_effect=AssertionError('Tests must remain offline'))
        offline.start()
        self.addCleanup(offline.stop)

    def write(self, relative, data):
        (self.export / relative).write_text(json.dumps(data), encoding='utf-8')

    def send(self, token='synthetic-token', repository=REPOSITORY):
        with patch.object(dispatch, 'durable_revision', return_value=self.revision):
            return dispatch.notify(repository, self.root, self.export, self.state, token, self.request)

    def test_dispatch_uses_one_fixed_destination_and_bounded_payload_then_deduplicates(self):
        self.assertTrue(self.send()['sent'])
        marker = self.state.read_bytes()
        self.assertFalse(self.send()['sent'])
        self.assertEqual(marker, self.state.read_bytes())
        self.request.assert_called_once()
        request = self.request.call_args.args[0]
        self.assertEqual(request.full_url,
                         'https://api.github.com/repos/trueChristian/remnant.truechristian.church/dispatches')
        self.assertEqual(request.method, 'POST')
        body = json.loads(request.data)
        self.assertEqual(set(body), {'event_type', 'client_payload'})
        self.assertEqual(body['event_type'], 'remnant-content-updated')
        self.assertEqual(body['client_payload']['repository'], REPOSITORY)
        self.assertEqual(body['client_payload']['revision'], self.revision)
        self.assertEqual(events.validate_payload(body['client_payload']), json.loads(marker))
        self.assertLess(len(request.data), 1024)
        self.assertNotIn('synthetic-token', request.full_url + request.data.decode() + marker.decode())
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-token')

    def test_revision_and_manifest_only_changes_do_not_notify(self):
        self.send()
        marker = self.state.read_bytes()
        before = dispatch.display_fingerprint(self.export, exported=True)
        self.revision = 'c' * 40
        self.write('manifest.json', {REVISION_KEY: self.revision, 'omitted': ['internal candidate']})
        index = json.loads((self.export / 'index.json').read_text())
        index.update(source_revision='d' * 40, translation_revision=self.revision)
        self.write('index.json', index)
        self.assertEqual(dispatch.display_fingerprint(self.export, exported=True), before)
        self.assertFalse(self.send()['sent'])
        self.assertEqual(self.state.read_bytes(), marker)
        self.request.assert_called_once()

    def test_public_html_metadata_and_asset_changes_each_notify(self):
        self.send()
        (self.export / 'article.html').write_text('<article>Changed</article>', encoding='utf-8')
        self.assertTrue(self.send()['sent'])
        index = json.loads((self.export / 'index.json').read_text())
        index['articles'][0]['title'] = 'Changed title'
        self.write('index.json', index)
        self.assertTrue(self.send()['sent'])
        (self.export / 'image.jpg').write_bytes(b'changed synthetic image')
        self.assertTrue(self.send()['sent'])
        self.assertEqual(self.request.call_count, 4)

    def test_notice_removal_and_review_metadata_are_public_changes(self):
        (self.export / 'article.html').write_text(
            '<article>Example</article><aside data-translation-notice="ai">Notice</aside>', encoding='utf-8')
        self.send()
        (self.export / 'article.html').write_text('<article>Example</article>', encoding='utf-8')
        self.assertTrue(self.send()['sent'])
        index = json.loads((self.export / 'index.json').read_text())
        index['articles'][0]['human_reviewed'] = True
        self.write('index.json', index)
        self.assertTrue(self.send()['sent'])

    def test_withdrawal_and_filename_changes_are_public_changes(self):
        self.send()
        (self.export / 'image.jpg').rename(self.export / 'new-image.jpg')
        self.assertTrue(self.send()['sent'])
        (self.export / 'article.html').unlink()
        self.write('index.json', {'articles': []})
        self.assertTrue(self.send()['sent'])

    def test_json_whitespace_and_key_order_do_not_change_fingerprint(self):
        before = dispatch.display_fingerprint(self.export, exported=True)
        path = self.export / 'index.json'
        path.write_text(json.dumps(json.loads(path.read_text()), sort_keys=True, indent=4), encoding='utf-8')
        self.assertEqual(dispatch.display_fingerprint(self.export, exported=True), before)

    def test_only_http_204_advances_state(self):
        for status in (200, 201, 202, 301, 400, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.request.return_value = (status, b'private response must not be logged')
                with self.assertRaisesRegex(RuntimeError, 'HTTP ' + str(status)) as error:
                    self.send()
                self.assertNotIn('private response', str(error.exception))
                self.assertFalse(self.state.exists())
        self.request.return_value = (204, b'')
        self.assertTrue(self.send()['sent'])
        marker = self.state.read_bytes()
        (self.export / 'article.html').write_text('<article>New</article>', encoding='utf-8')
        self.request.return_value = (503, b'')
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertEqual(self.state.read_bytes(), marker)

    def test_failure_preserves_marker_and_retry_recovers_without_leaking_details(self):
        self.send()
        marker = self.state.read_bytes()
        (self.export / 'article.html').write_text('<article>New</article>', encoding='utf-8')
        self.request.side_effect = TimeoutError('synthetic-token at private URL')
        with self.assertRaisesRegex(RuntimeError, 'no success marker') as error:
            self.send()
        self.assertNotIn('synthetic-token', str(error.exception))
        self.assertNotIn('private URL', str(error.exception))
        self.assertEqual(self.state.read_bytes(), marker)
        self.request.side_effect = None
        self.assertTrue(self.send()['sent'])
        self.assertFalse(self.send()['sent'])

    def test_missing_token_stale_export_and_unrecognized_source_never_send(self):
        with self.assertRaisesRegex(ValueError, 'Owner setup required'):
            self.send(token='')
        with self.assertRaisesRegex(ValueError, 'Unrecognized source'):
            self.send(repository='attacker/berean-translation')
        self.write('manifest.json', {REVISION_KEY: 'e' * 40})
        with self.assertRaisesRegex(ValueError, 'freshly validated'):
            self.send()
        self.request.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_unpublished_commit_never_sends(self):
        with patch.object(dispatch, 'durable_revision', side_effect=ValueError('not published')):
            with self.assertRaisesRegex(ValueError, 'not published'):
                dispatch.notify(REPOSITORY, self.root, self.export, self.state, 'synthetic-token', self.request)
        self.request.assert_not_called()

    def test_lost_or_corrupt_state_safely_resends(self):
        self.send()
        for value in ('incomplete', '[]', 'x' * 65537):
            self.state.write_text(value, encoding='utf-8')
            self.assertTrue(self.send()['sent'])
        self.state.unlink()
        self.assertTrue(self.send()['sent'])

    def test_empty_and_symlink_exports_are_rejected(self):
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, 'empty display'):
            dispatch.display_fingerprint(empty, exported=True)
        (self.export / 'link').symlink_to(self.root / 'secret')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.send()
        self.request.assert_not_called()

    def test_durable_revision_checks_clean_head_and_exact_remote_main(self):
        good = [self.revision, '', self.revision + '\trefs/heads/main']
        with patch.object(dispatch.subprocess, 'check_output', side_effect=good) as git:
            self.assertEqual(dispatch.durable_revision(self.root), self.revision)
            self.assertEqual([call.args[0][3:] for call in git.call_args_list], [
                ['rev-parse', 'HEAD'], ['status', '--porcelain', '--untracked-files=no'],
                ['ls-remote', '--exit-code', 'origin', 'refs/heads/main'],
            ])
        for values in ([self.revision, ' M content/x'], [self.revision, '', ''],
                       [self.revision, '', 'b' * 40 + '\trefs/heads/main'],
                       [self.revision, '', self.revision + '\trefs/heads/other'],
                       [self.revision, '', '\n'.join([good[-1], good[-1]])], ['main']):
            with self.subTest(values=values):
                with patch.object(dispatch.subprocess, 'check_output', side_effect=values):
                    with self.assertRaises(ValueError):
                        dispatch.durable_revision(self.root)

    def test_each_language_registry_display_field_changes_publication(self):
        self.send()
        registry = json.loads(self.registry.read_text())
        for field, value in [('name', 'Afrikaans display label'), ('native_name', 'Native label'),
                             ('tag', 'af-ZA'), ('dir', 'rtl'), ('aliases', ['af-ZA'])]:
            with self.subTest(field=field):
                registry['afr'][field] = value
                self.registry.write_text(json.dumps(registry), encoding='utf-8')
                self.assertTrue(self.send()['sent'])
        self.assertEqual(self.request.call_count, 6)

    def test_zero_article_language_addition_and_removal_notify(self):
        self.send()
        registry = json.loads(self.registry.read_text())
        registry['fra'] = {'name': 'French', 'native_name': 'Français',
                           'tag': 'fr', 'dir': 'ltr', 'aliases': []}
        self.registry.write_text(json.dumps(registry), encoding='utf-8')
        self.assertTrue(self.send()['sent'])
        del registry['fra']
        self.registry.write_text(json.dumps(registry), encoding='utf-8')
        self.assertTrue(self.send()['sent'])
        self.assertEqual(self.request.call_count, 3)

    def test_runtime_guidance_and_registry_json_formatting_are_excluded(self):
        self.send()
        registry = json.loads(self.registry.read_text())
        registry['afr'].update(guidance='Private runtime guidance', model='model-choice',
                                prompt='Internal prompt wording')
        self.registry.write_text(json.dumps(registry, sort_keys=True, indent=4), encoding='utf-8')
        self.assertFalse(self.send()['sent'])
        self.request.assert_called_once()

    def test_registry_missing_malformed_oversized_or_symlink_fails_closed(self):
        for value in ('[]', '{"afr": []}', 'x' * 1_000_001):
            with self.subTest(value=value[:40]):
                self.registry.write_text(value, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.send()
        self.registry.unlink()
        with self.assertRaisesRegex(ValueError, 'Fresh source language registry'):
            self.send()
        external = self.root / 'external-registry.json'
        external.write_text('{}', encoding='utf-8')
        self.registry.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'Fresh source language registry'):
            self.send()
        self.request.assert_not_called()


class EventAndNetworkTests(unittest.TestCase):
    def test_payload_is_exact_versioned_and_rejects_untrusted_sources_and_refs(self):
        payload = {'schema': 1, 'repository': REPOSITORY,
                   'revision': 'a' * 40, 'display_fingerprint': 'b' * 64}
        self.assertEqual(events.validate_payload(payload), payload)
        invalid = [None, [], {'schema': 1}, {**payload, 'checkout': 'attacker/ref'}]
        for key, value in [('schema', True), ('schema', 2), ('repository', []),
                           ('repository', 'attacker/berean-voice'), ('revision', 'main'),
                           ('revision', '../main'), ('revision', '$(touch /tmp/pwn)'),
                           ('revision', 'a' * 40 + '\n'), ('revision', 'A' * 40),
                           ('revision', 'a' * 41), ('revision', None),
                           ('display_fingerprint', 'a' * 65), ('display_fingerprint', None)]:
            invalid.append({**payload, key: value})
        for value in invalid:
            with self.subTest(payload=value), self.assertRaises(ValueError):
                events.validate_payload(value)

    def test_cli_rejects_untrusted_actions_before_notification(self):
        args = ['dispatch.py', 'notify', '--repository', REPOSITORY,
                '--export', 'unused-export', '--state', 'unused-state']
        trusted = {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main',
                   'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_EVENT_NAME': 'push'}
        for changes in ({'GITHUB_REF': 'refs/heads/feature'},
                        {'GITHUB_REPOSITORY': 'attacker/fork'},
                        {'GITHUB_EVENT_NAME': 'pull_request'},
                        {'GITHUB_EVENT_NAME': 'pull_request_target'}):
            with self.subTest(changes=changes), patch.object(sys, 'argv', args):
                with patch.dict(os.environ, {**trusted, **changes}, clear=True):
                    with patch.object(dispatch, 'notify') as notify, self.assertRaisesRegex(ValueError, 'trusted'):
                        dispatch.main()
                    notify.assert_not_called()
        with patch.object(sys, 'argv', args), patch.dict(os.environ, trusted, clear=True):
            with patch.object(dispatch, 'notify', return_value={'sent': True}) as notify, redirect_stdout(io.StringIO()):
                dispatch.main()
            notify.assert_called_once()

    def test_network_has_timeout_response_bound_and_no_redirects(self):
        response = Mock(status=204)
        response.read.return_value = b''
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        request = dispatch.urllib.request.Request(dispatch.DISPATCH_ENDPOINT)
        with patch.object(dispatch.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(dispatch.request_bytes(request), (204, b''))
            build.assert_called_once_with(dispatch.NoRedirect)
            opener.open.assert_called_once_with(request, timeout=30)
            response.read.assert_called_once_with(65537)
            response.read.return_value = b'x' * 65537
            with self.assertRaisesRegex(ValueError, 'bounded metadata'):
                dispatch.request_bytes(request)
        with self.assertRaisesRegex(ValueError, 'Redirects'):
            dispatch.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://attacker.invalid')


class WorkflowTests(unittest.TestCase):
    def test_direct_hook_follows_validation_and_fresh_export(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        self.assertLess(text.index('.venv/bin/python -m berean_translation tick --publish'),
                        text.index('Validate the resulting runtime records'))
        self.assertLess(text.index('.venv/bin/python -m berean_translation validate'),
                        text.index('Export a fresh website-facing publication'))
        self.assertLess(text.index('Export a fresh website-facing publication'), text.index('id: remnant_notify'))
        self.assertLess(text.index('id: remnant_notify'), text.index('Remember only a successfully accepted notification'))
        self.assertIn('.venv/bin/python -m berean_translation export --source-checkout .build/source --output .build/remnant-export --base /', text)
        self.assertIn('python3 .github/remnant/dispatch.py notify', text)
        self.assertIn('--repository ' + REPOSITORY, text)
        self.assertIn('--checkout . --export .build/remnant-export', text)
        self.assertIn('ref: main', text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", text)
        self.assertIn('github.event.workflow_run.head_repository.full_name == github.repository', text)
        self.assertNotIn('pull_request_target:', text)

    def test_each_hook_step_requires_main_and_explicit_opt_in_and_success_save(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        steps = re.split(r'(?m)^      - name: ', text)[1:]
        selected = [step for step in steps if step.startswith((
            'Restore the last accepted Remnant', 'Export a fresh website-facing',
            'Notify Remnant', 'Remember only a successfully accepted',
        ))]
        self.assertEqual(len(selected), 4)
        for step in selected:
            condition = re.search(r'(?m)^        if: (.+)$', step).group(1)
            self.assertIn("github.ref == 'refs/heads/main'", condition)
            self.assertIn("vars.REMNANT_NOTIFICATIONS_ENABLED == 'true'", condition)
            self.assertIn("(github.event_name != 'workflow_dispatch' || inputs.operation == 'collect')", condition)
            self.assertNotIn('always()', condition)
        notify = next(step for step in selected if step.startswith('Notify Remnant'))
        save = next(step for step in selected if step.startswith('Remember only'))
        self.assertIn('REMNANT_DISPATCH_TOKEN: ${{ secrets.REMNANT_DISPATCH_TOKEN }}', notify)
        self.assertNotIn('--token', notify)
        self.assertIn("steps.remnant_notify.outputs.sent == 'true'", save)
        self.assertEqual(text.count('secrets.REMNANT_DISPATCH_TOKEN'), 1)
        self.assertNotIn('continue-on-error:', '\n'.join(selected))
        for step in selected:
            for reference in re.findall(r'uses: (\S+)', step):
                self.assertRegex(reference, r'^[\w/-]+@[0-9a-f]{40}$')


if __name__ == '__main__':
    unittest.main()
