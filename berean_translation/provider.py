"""Official OpenAI SDK adapter. Batch creation is NEVER transparently retried."""
from __future__ import annotations
import io
import os
from typing import Protocol
from .common import ContractError, loads


class BatchProvider(Protocol):
    def upload(self, payload: bytes, name: str) -> str: ...
    def create(self, input_file_id: str, key: str, campaign: str) -> dict: ...
    def retrieve(self, batch_id: str) -> dict: ...
    def find(self, key: str) -> list[dict]: ...
    def content(self, file_id: str) -> bytes: ...
    def cancel(self, batch_id: str) -> dict: ...


class OpenAIProvider:
    def __init__(self, api_key: str | None = None, client=None):
        if client is None:
            key = api_key or os.environ.get('OPENAI_API_KEY')
            if not key:
                raise ContractError('Add the OPENAI_API_KEY repository Actions secret')
            from openai import OpenAI
            client = OpenAI(api_key=key, timeout=90.0, max_retries=0,
                            base_url='https://api.openai.com/v1')
        self.client = client

    @staticmethod
    def data(value) -> dict:
        return value.model_dump(mode='json') if hasattr(value,'model_dump') else dict(value)

    def upload(self, payload: bytes, name: str) -> str:
        result = self.client.files.create(file=(name,payload,'application/jsonl'),purpose='batch')
        return result.id

    def create(self, input_file_id: str, key: str, campaign: str) -> dict:
        result = self.client.batches.create(input_file_id=input_file_id,
                     endpoint='/v1/chat/completions', completion_window='24h',
                     metadata={'application':'berean-translation','submission_key':key,'campaign':campaign})
        return self.data(result)

    def retrieve(self, batch_id: str) -> dict:
        return self.data(self.client.batches.retrieve(batch_id))

    def find(self, key: str) -> list[dict]:
        # SDK auto-pagination: absence is NOT permission to resubmit an uncertain create.
        matches = []
        for result in self.client.batches.list(limit=100):
            item = self.data(result)
            if (item.get('metadata') or {}).get('submission_key') == key:
                matches.append(item)
        return matches

    def content(self, file_id: str) -> bytes:
        response = self.client.files.content(file_id)
        return response.content

    def cancel(self, batch_id: str) -> dict:
        return self.data(self.client.batches.cancel(batch_id))
