"""Versioned language, model, and runtime configuration."""
from __future__ import annotations
import re
import math
from pathlib import Path
from .common import ContractError, csv_values, read_json


class Config:
    def __init__(self, root: Path):
        self.root = root
        self.runtime = read_json(root / 'config/runtime.json')
        self.languages = read_json(root / 'config/languages.json')
        self.models = read_json(root / 'config/models.json')
        if not all(isinstance(x, dict) for x in (self.runtime, self.languages, self.models)):
            raise ContractError('Missing repository configuration')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.runtime['source_repository']):
            raise ContractError('Invalid source repository')
        for code, language in self.languages.items():
            if not re.fullmatch(r'[a-z]{3}', code) or language['dir'] not in ('ltr', 'rtl'):
                raise ContractError(f'Invalid language configuration: {code}')
            if not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', language['tag']):
                raise ContractError(f'Invalid language tag: {code}')
            if set(re.findall(r'\{(\w+)\}', language['notice'])) != {'model'}:
                raise ContractError(f'Notice must contain only the model placeholder: {code}')
        self.language_aliases = {}
        for code, item in self.languages.items():
            for alias in [code, item['tag'], *item.get('aliases', [])]:
                key = alias.casefold()
                if key in self.language_aliases and self.language_aliases[key] != code:
                    raise ContractError(f'Ambiguous language alias: {alias}')
                self.language_aliases[key] = code
        for name, model in self.models.items():
            if not isinstance(model, dict):
                raise ContractError('Model configuration must be an object')
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*',name) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*',model.get('api_model','')):
                raise ContractError('Invalid model identity')
            cache_fields = {'cached_input_batch_usd_per_million', 'cache_write_batch_usd_per_million'}
            long_fields = {'long_context_threshold_tokens', 'long_context_input_multiplier',
                           'long_context_output_multiplier'}
            if (cache_fields & model.keys()) and not cache_fields <= model.keys():
                raise ContractError('Cache pricing requires both read and write rates')
            if (long_fields & model.keys()) and not long_fields <= model.keys():
                raise ContractError('Long-context pricing requires a threshold and both multipliers')
            for field in ('input_batch_usd_per_million','output_batch_usd_per_million',
                          *sorted(cache_fields & model.keys())):
                value = model.get(field)
                if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
                    raise ContractError('Model prices must be finite nonnegative numbers')
            if any(type(model.get(k)) is not int or model[k] <= 0 for k in ('context_tokens','max_output_tokens')):
                raise ContractError('Model token limits must be positive integers')
            if model['max_output_tokens'] > model['context_tokens']:
                raise ContractError('Output token limit exceeds model context')
            if long_fields <= model.keys():
                threshold = model['long_context_threshold_tokens']
                if type(threshold) is not int or not 0 < threshold < model['context_tokens']:
                    raise ContractError('Long-context threshold must be a positive input token count below context')
                for field in ('long_context_input_multiplier', 'long_context_output_multiplier'):
                    value = model[field]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 1:
                        raise ContractError('Long-context multipliers must be finite and at least one')
        for field in ('max_tasks_per_request','max_batch_requests','max_batch_bytes','max_output_tokens',
                      'review_output_tokens','reasoning_review_output_tokens','max_source_html_bytes','max_result_bytes',
                      'max_batches_per_tick','max_pending_campaigns_per_tick'):
            if type(self.runtime.get(field)) is not int or self.runtime[field] <= 0:
                raise ContractError('Runtime size/count limits must be positive integers')
        if self.runtime.get('automatic_new_translation') is not False:
            raise ContractError('Automatic new paid campaigns are outside the manual-authorization contract')
        refresh = self.runtime.get('automatic_source_refresh', {'enabled': False})
        if not isinstance(refresh, dict) or type(refresh.get('enabled')) is not bool:
            raise ContractError('Automatic source refresh must have a boolean enabled setting')
        if refresh['enabled'] or set(refresh) != {'enabled'}:
            if set(refresh) != {'enabled', 'model', 'review_model', 'budget_usd', 'max_campaigns_per_tick'}:
                raise ContractError('Invalid automatic source refresh policy fields')
            if any(refresh.get(field) != 'gpt-5-mini' for field in ('model', 'review_model')):
                raise ContractError('Automatic source refresh requires gpt-5-mini translation and review')
            self.model(refresh['model']); self.model(refresh['review_model'])
            budget = refresh.get('budget_usd')
            if type(budget) not in (int, float) or not math.isfinite(budget) or not 0 < budget <= min(10, self.runtime['max_campaign_usd']):
                raise ContractError('Automatic source refresh budget must be positive and at most $10 per campaign')
            limit = refresh.get('max_campaigns_per_tick')
            if type(limit) is not int or not 1 <= limit <= 5:
                raise ContractError('Automatic source refresh permits at most five campaigns per discovery tick')
        from .downstream import validate_policy
        validate_policy(self)
        if self.runtime['max_translation_attempts'] != 2:
            raise ContractError('Exactly two translation attempts are the hard limit')
        if self.runtime['quality_threshold'] != 95:
            raise ContractError('This contract uses a 95/100 acceptance threshold')

    def select_languages(self, value: str) -> list[str]:
        if value == 'all':
            return list(self.languages)
        try:
            selected = [self.language_aliases[x.casefold()] for x in csv_values(value)]
        except KeyError as exc:
            raise ContractError(f'Unknown language: {exc.args[0]}') from exc
        if len(set(selected)) != len(selected):
            raise ContractError('Two language aliases selected the same language')
        return selected

    def model(self, name: str) -> dict:
        if name not in self.models:
            raise ContractError(f'Unsupported model: {name}')
        return self.models[name]

    def review_output_limit(self, model_name: str) -> int:
        """Resolve a new campaign's total review cap, then freeze it at acceptance.

        Reasoning shares max_completion_tokens with the returned JSON. A larger
        finite cap provides headroom, not a guarantee of a complete response.
        Existing campaigns keep their recorded cap; request construction must
        never consult this live policy for already accepted work.
        """
        model = self.model(model_name)
        limit = self.runtime['review_output_tokens']
        if model.get('reasoning_effort'):
            limit = max(limit, self.runtime['reasoning_review_output_tokens'])
        return min(limit, model['max_output_tokens'])

    def prompt(self, name: str) -> str:
        if name not in ('translation', 'review'):
            raise ContractError('Invalid prompt name')
        return (self.root / 'prompts' / (name + '.txt')).read_text(encoding='utf-8')
