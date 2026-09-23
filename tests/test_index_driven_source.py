"""Editors update source content; only the consumer computes change fingerprints."""
from __future__ import annotations
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from berean_translation.common import ContractError, digest, loads
from berean_translation.source import SourceClient, translation_key
from support import A, B, setup, drive, queue


class IndexDrivenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)

    def test_no_core_manifest_or_navigation_is_read(self):
        self.upstream.contents.pop('manifest.json')
        fetched = []
        def fetch(url):
            fetched.append(url)
            self.assertNotIn('manifest.json', url)
            self.assertNotIn('navigation.json', url)
            return self.upstream.fetch(url)
        result = SourceClient(self.config, fetch=fetch).discover()
        self.assertEqual(set(result['articles']), {A, B})
        self.assertEqual(result['fingerprint_origin'], 'translation-runtime')

    def test_stale_or_invalid_core_generated_data_is_irrelevant(self):
        before = self.upstream.client.discover()
        self.upstream.contents['manifest.json'] = 'not even JSON'
        self.upstream.contents['navigation.json'] = '{stale}'
        self.assertEqual(self.upstream.client.discover(), before)

    def test_html_only_edit_detected_without_touching_index_or_manifest(self):
        queue(self.state)
        drive(self.engine, self.provider)
        original_index = self.upstream.contents['index.json']
        original_manifest = self.upstream.contents['manifest.json']
        self.upstream.revision = 'b' * 40
        path = f'content/articles/{A}.html'
        self.upstream.contents[path] = self.upstream.contents[path].replace('Faith', 'Trust')
        calls = self.provider.create_calls
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, calls)
        self.assertEqual(self.upstream.contents['index.json'], original_index)
        self.assertEqual(self.upstream.contents['manifest.json'], original_manifest)
        statuses = {item['id']: item['status'] for item in self.state.read('index.json')['articles']}
        self.assertEqual(statuses[A], 'stale')
        self.assertEqual(statuses[B], 'ready')

    def test_formatting_only_index_edit_does_not_retranslate(self):
        before = self.upstream.client.discover()['articles']
        self.upstream.revision = 'b' * 40
        self.upstream.contents['index.json'] += '\n\n'
        self.assertEqual(self.upstream.client.discover()['articles'], before)

    def test_new_commit_with_same_content_keeps_all_translation_keys(self):
        before = self.upstream.client.discover()
        self.upstream.revision = 'b' * 40
        after = self.upstream.client.discover()
        self.assertNotEqual(before['revision'], after['revision'])
        self.assertEqual(before['articles'], after['articles'])

    def test_title_edit_changes_only_that_article(self):
        before = self.upstream.client.discover()['articles']
        self.upstream.revision = 'b' * 40
        self.upstream.contents['index.json'] = self.upstream.contents['index.json'].replace('"title": "Faith"', '"title": "Trust"', 1)
        after = self.upstream.client.discover()['articles']
        self.assertNotEqual(before[A]['translation_key'], after[A]['translation_key'])
        self.assertEqual(before[B]['translation_key'], after[B]['translation_key'])

    def test_grouping_only_edit_keeps_translation(self):
        before = self.upstream.client.discover()['articles'][A]['translation_key']
        import json
        index = loads(self.upstream.contents['index.json'])
        index['articles'][0]['topics'] = ['55555555-5555-4555-8555-555555555555']
        self.upstream.contents['index.json'] = json.dumps(index)
        after = self.upstream.client.discover()['articles'][A]['translation_key']
        self.assertEqual(before, after)

    def test_missing_real_html_is_not_mistaken_for_an_empty_inventory(self):
        self.engine.discover()
        before = self.state.read('state/source.json')
        self.upstream.contents.pop(f'content/articles/{A}.html')
        with self.assertRaises((ContractError, KeyError)):
            self.engine.discover()
        self.assertEqual(self.state.read('state/source.json'), before)

    def test_invalid_structure_and_changed_image_reference_still_fail(self):
        path = f'content/articles/{A}.html'
        original = self.upstream.contents[path]
        for text in (original.replace('<p>', '<script>'), original.replace(f'{A}-1.jpg', f'{A}-2.jpg')):
            self.upstream.contents[path] = text
            with self.assertRaises(ContractError):
                self.engine.discover()

    def test_local_fingerprint_recipe_preserves_preexisting_compatibility(self):
        # Fixture manifests use the same recipe. Runtime must not fetch them.
        expected = copy.deepcopy(self.upstream.manifest['articles'])
        self.upstream.contents.pop('manifest.json')
        observed = self.upstream.client.discover()
        for identity in (A, B):
            self.assertEqual(observed['articles'][identity]['translation_key'], translation_key(expected[identity]))

    def test_snapshots_are_frozen_to_the_observed_run_not_later_source_edits(self):
        self.upstream.client.discover()
        before = self.upstream.client.snapshot(A)
        self.upstream.contents[f'content/articles/{A}.html'] = 'later edit'
        self.assertEqual(self.upstream.client.snapshot(A), before)
        before['html'] = 'mutated by caller'
        self.assertNotEqual(self.upstream.client.snapshot(A), before)
