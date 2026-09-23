"""Versioned language, model, and runtime configuration."""
from __future__ import annotations
import re
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

    def prompt(self, name: str) -> str:
        if name not in ('translation', 'review'):
            raise ContractError('Invalid prompt name')
        return (self.root / 'prompts' / (name + '.txt')).read_text(encoding='utf-8')
