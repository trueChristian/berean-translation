"""Versioned review schemas and complete validation before workflow decisions."""
from __future__ import annotations

import copy
import html
import re
import unicodedata
from .common import ContractError

CURRENT_VERSION = 2
MAX_FINDINGS = 30


def review_threshold(campaign, task=None):
    """Use the campaign's frozen threshold, with stricter replacement review."""
    threshold = campaign.get('quality_threshold', 95)
    if (task or {}).get('accepted_baseline') is not None:
        threshold = campaign.get('upgrade_quality_threshold', 98)
    if type(threshold) is not int or not 0 <= threshold <= 100:
        raise ContractError('Review threshold must be an integer from 0 through 100')
    return threshold


def _normalized_text(value):
    """Compare evidence as visible Unicode words, ignoring quotation marks."""
    value = html.unescape(re.sub(r'<[^>]*>', ' ', value))
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold()))


def _no_op_text(value):
    """Ignore display wrappers while retaining meaning-bearing punctuation."""
    value = html.unescape(re.sub(r'<[^>]*>', ' ', value))
    value = ' '.join(unicodedata.normalize('NFKC', value).split())
    return value.strip('"\'“”‘’«»„‟‹›「」『』')


def _article_text(value):
    if not isinstance(value, dict):
        return ''
    metadata = value.get('article', value)
    if not isinstance(metadata, dict):
        return ''
    return _normalized_text(' '.join([str(value.get('html') or ''),
                                     *(str(metadata.get(key) or '')
                                       for key in ('title', 'subtitle', 'section'))]))


def actionable_finding(finding, *, source=None, candidate=None):
    """Discard only demonstrably self-contradictory, anchored major findings.

    Unknown evidence and actual proposed changes remain blocking. This does not
    translate, judge theology, or relax the independent structural validation.
    """
    if finding['severity'] not in ('major', 'critical'):
        return False
    source_quote = _normalized_text(finding['source_quote'])
    target_quote = _normalized_text(finding['translation_quote'])
    source_text, target_text = _article_text(source), _article_text(candidate)
    if (not source_quote or not target_quote or source_quote not in source_text
            or target_quote not in target_text):
        return True
    suggested = _no_op_text(finding['suggested_fix'])
    if suggested == _no_op_text(finding['translation_quote']):
        return False
    return True


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
