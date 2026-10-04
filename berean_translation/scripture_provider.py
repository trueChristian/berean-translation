"""Read-only, explicit-edition GetBible MCP prefetch. Never invokes a model.

The provider's SHA-1 is a scope version token checked before/after by GetBible,
not a local checksum of JSON bytes. Our SHA-256 separately binds exact evidence.
"""
from __future__ import annotations
import asyncio
import copy
import importlib.metadata
import re
from .common import ContractError, canonical, json_hash, now

ENDPOINT = 'https://mcp.getbible.net/'
API_VERSION = 'v2'
SDK_VERSION = '2.2.0'
MAX_RESPONSE_BYTES = 2_000_000


class ScriptureProviderError(ContractError):
    pass


class GetBibleMCP:
    """One bounded anonymous MCP session per lookup; no environment credentials.

    Constructor/imports have no network side effects. An injected callable is
    used by offline tests. Only the two explicitly allowed read-only tools exist.
    """
    def __init__(self, call_tool=None, *, timeout=45):
        self._call_tool = call_tool
        self.timeout = timeout

    def call(self, name: str, arguments: dict) -> dict:
        if name not in ('get_scripture', 'query_verses'):
            raise ScriptureProviderError('Unsupported Scripture lookup tool')
        if arguments.get('api_version') != API_VERSION or not arguments.get('translation'):
            raise ScriptureProviderError('Scripture lookup requires an explicit edition and API v2')
        try:
            result = (self._call_tool(name, copy.deepcopy(arguments)) if self._call_tool
                      else asyncio.run(self._remote(name, arguments)))
            if not isinstance(result, dict) or len(canonical(result)) > MAX_RESPONSE_BYTES:
                raise ScriptureProviderError('Scripture result is absent or exceeds its full-response limit')
            if result.get('isError') or result.get('is_error'):
                raise ScriptureProviderError('GetBible returned an MCP tool error; no fallback is permitted')
            data = result.get('structuredContent', result.get('structured_content'))
            if not isinstance(data, dict):
                raise ScriptureProviderError('GetBible returned no structured result')
            source = data.get('source', {})
            if (source.get('api_version') != API_VERSION or source.get('status_code') != 200
                    or not isinstance(source.get('url'), str)):
                raise ScriptureProviderError('GetBible source provenance is missing or inconsistent')
            return {'tool': name, 'arguments': copy.deepcopy(arguments), 'endpoint': ENDPOINT,
                    'client_version': SDK_VERSION, 'retrieved_at': now(),
                    'result': data, 'result_sha256': json_hash(data)}
        except ScriptureProviderError:
            raise
        except Exception as exc:
            # Never print server payloads, environment variables or credentials.
            raise ScriptureProviderError(f'GetBible lookup failed ({type(exc).__name__}); no fallback is permitted') from exc

    async def _remote(self, name, arguments):
        try:
            if importlib.metadata.version('mcp') != SDK_VERSION:
                raise ScriptureProviderError('Install the pinned Scripture MCP client dependencies')
        except importlib.metadata.PackageNotFoundError as exc:
            raise ScriptureProviderError('Install the pinned Scripture MCP client dependencies') from exc
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        async with asyncio.timeout(self.timeout):
            async with Client(streamable_http_client(ENDPOINT), cache=None,
                              read_timeout_seconds=self.timeout) as client:
                result = await client.call_tool(name, arguments)
                return result.model_dump(mode='json', by_alias=True)

    def chapter(self, edition: str, book: int, chapter: int) -> dict:
        arguments = {'translation': edition, 'book': book, 'chapter': chapter, 'api_version': API_VERSION}
        evidence = self.call('get_scripture', arguments)
        result = evidence['result']
        scope = result.get('scope', {})
        if scope != {'kind': 'chapter', **arguments}:
            raise ScriptureProviderError('GetBible returned a different chapter/edition scope')
        if (result.get('consistency_checked') is not True
                or not re.fullmatch(r'[0-9a-f]{40}', result.get('hash', ''))):
            raise ScriptureProviderError('GetBible chapter lacks its before/after SHA consistency check')
        data = result.get('data', {})
        if (not isinstance(data, dict) or data.get('book_nr') != book
                or data.get('chapter') != chapter or data.get('abbreviation') != edition):
            raise ScriptureProviderError('GetBible native chapter identity differs from the requested scope')
        verses = data.get('verses')
        if not isinstance(verses, list) or not verses:
            raise ScriptureProviderError('GetBible chapter contains no verses')
        seen = set()
        for verse in verses:
            if (not isinstance(verse, dict) or type(verse.get('verse')) is not int
                    or verse['verse'] < 1 or verse['verse'] in seen
                    or not isinstance(verse.get('text'), str) or not verse['text'].strip()):
                raise ScriptureProviderError('GetBible returned invalid or duplicated verses')
            seen.add(verse['verse'])
        return evidence

    def query(self, edition: str, references: str) -> dict:
        if not isinstance(references, str) or not 1 <= len(references) <= 512:
            raise ScriptureProviderError('Scripture reference must contain 1–512 characters')
        evidence = self.call('query_verses', {'translation': edition, 'references': references,
                                             'api_version': API_VERSION})
        result = evidence['result']
        if result.get('translation') != edition or result.get('references') != references:
            raise ScriptureProviderError('GetBible returned a different query identity')
        # Query results intentionally carry no fabricated chapter checksum.
        return evidence
