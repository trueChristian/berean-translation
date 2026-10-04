"""Versioned review schemas and complete validation before workflow decisions."""
from __future__ import annotations

import copy
from .common import ContractError

CURRENT_VERSION = 2
MAX_FINDINGS = 30

# Never change this schema: missing campaign version means this exact legacy
# request contract, including its historically unbounded findings array.
LEGACY_REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'score': {'type': 'integer'}, 'passed': {'type': 'boolean'},
        'findings': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'severity': {'type': 'string', 'enum': ['minor', 'major', 'critical']},
                **{key: {'type': 'string'} for key in
                   ('location', 'source_quote', 'translation_quote', 'suggested_fix')}},
            'required': ['severity', 'location', 'source_quote', 'translation_quote', 'suggested_fix']}}},
    'required': ['score', 'passed', 'findings']}


def validate_version(value):
    if value is not None and (type(value) is not int or value != CURRENT_VERSION):
        raise ContractError('Unsupported frozen review contract version')


def frozen_version(settings):
    if 'review_contract_version' not in settings:
        return None
    value = settings['review_contract_version']
    if value is None:
        raise ContractError('Unsupported frozen review contract version')
    validate_version(value)
    return value


def frozen_fields(config):
    version = frozen_version(config.runtime)
    return {} if version is None else {'review_contract_version': version}


def review_schema(contract_version=None):
    validate_version(contract_version)
    schema = copy.deepcopy(LEGACY_REVIEW_SCHEMA)
    if contract_version == CURRENT_VERSION:
        schema['properties']['score'].update(minimum=0, maximum=100)
        schema['properties']['findings']['maxItems'] = MAX_FINDINGS
        schema['properties']['findings_complete'] = {'type': 'boolean'}
        schema['required'].append('findings_complete')
    return schema


def validate_review(review, contract_version=None, *, enforce_limit=True):
    """Validate every item, even in a negative report; never truncate evidence.

    Disabling the item limit is for bounded read-only legacy diagnostics only.
    Workflow acceptance always retains the limit.
    """
    validate_version(contract_version)
    keys = {'score', 'passed', 'findings'}
    if contract_version == CURRENT_VERSION:
        keys.add('findings_complete')
    if (not isinstance(review, dict) or set(review) != keys
            or type(review['score']) is not int or not 0 <= review['score'] <= 100):
        raise ContractError('Invalid review score or schema')
    if (type(review['passed']) is not bool or not isinstance(review['findings'], list)
            or (enforce_limit and len(review['findings']) > MAX_FINDINGS)):
        raise ContractError('Invalid review verdict or findings')
    if contract_version == CURRENT_VERSION and type(review['findings_complete']) is not bool:
        raise ContractError('Invalid review completeness marker')
    for item in review['findings']:
        fields = {'severity', 'location', 'source_quote', 'translation_quote', 'suggested_fix'}
        if (not isinstance(item, dict) or set(item) != fields
                or not all(isinstance(value, str) for value in item.values())):
            raise ContractError('Invalid review finding')
        if item['severity'] not in ('minor', 'major', 'critical'):
            raise ContractError('Unknown finding severity')
