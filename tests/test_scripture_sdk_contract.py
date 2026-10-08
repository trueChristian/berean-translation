"""Verify the installed runtime MCP interface without opening a connection."""
import importlib.metadata
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch
from berean_translation.scripture_provider import API_VERSION, ENDPOINT, SDK_VERSION

try:
    import mcp
except ImportError:
    mcp = None


@unittest.skipIf(mcp is None, 'Install requirements-dev.txt to verify the runtime MCP SDK')
class ScriptureSDKContractTests(unittest.TestCase):
    def test_pinned_sdk_supports_the_used_transport_and_client_arguments(self):
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        self.assertEqual(importlib.metadata.version('mcp'), SDK_VERSION)
        self.assertEqual(importlib.metadata.version('httpx'), '0.28.1')
        self.assertIn('cache', inspect.signature(Client).parameters)
        self.assertIn('read_timeout_seconds', inspect.signature(Client).parameters)
        self.assertIn('url', inspect.signature(streamable_http_client).parameters)
        self.assertIn('arguments', inspect.signature(Client.call_tool).parameters)
        self.assertEqual(API_VERSION, 'v2')
        with patch('socket.socket.connect', side_effect=AssertionError('No network')):
            transport = streamable_http_client(ENDPOINT)
            Client(transport, cache=None, read_timeout_seconds=45)

    def test_historical_sdk_dependencies_are_absent_from_active_collection(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('-r requirements-scripture.txt', (root/'requirements-dev.txt').read_text())
        self.assertNotIn('-r requirements-scripture.txt', (root/'.github/workflows/ai-worker.yml').read_text())
        self.assertIn('mcp==' + SDK_VERSION, (root/'requirements-scripture.txt').read_text())
