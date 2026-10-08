"""Versioned canonical/authored quotation primitives, reconstructed after reset.

These pure functions provide structural review input, never publication approval.
Offsets are Python Unicode code points; no spelling/whitespace normalization.
Provider, article and candidate binding belongs to the component evidence layer.
"""
from __future__ import annotations

import re
import unicodedata

from .common import canonical, json_hash
from .scripture_association import mentions
from .scripture_evidence import ScriptureAttention, _reference

COMPONENT_VERSION = '1'
MAX_COMPONENT_BYTES = 32768
MAX_COMPONENTS = 128


def _hold(reason, detail):
    raise ScriptureAttention(reason, detail)


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        _hold('component_schema', 'Missing or unexpected component fields')


def _version(version):
    if version != COMPONENT_VERSION:
        _hold('component_version', 'Explicit supported component version required')


def decode_component_reference(printed_reference, *, version):
    """Preserve the printed citation, treating a single suffix as opaque."""
    _version(version)
    if not isinstance(printed_reference, str) or len(printed_reference) > 256:
        _hold('unsupported_reference', 'Reference must be a bounded string')
    match = re.fullmatch(r'(.+ [1-9][0-9]*:[1-9][0-9]*)([a-z])', printed_reference)
    printed_lookup = match[1] if match else printed_reference
    if not re.fullmatch(r'.+ [1-9][0-9]*:[1-9][0-9]*(?:-[1-9][0-9]*)?(?:,[1-9][0-9]*(?:-[1-9][0-9]*)?)*', printed_lookup):
        _hold('unsupported_reference', 'Malformed component lookup reference')
    references = mentions(printed_lookup)
    if (len(references) != 1 or references[0][:2] != (0, len(printed_lookup))
            or references[0][2].startswith('!')):
        _hold('unsupported_reference', 'One complete recognized printed reference required')
    lookup = references[0][2]
    book, chapter, verses = _reference(lookup)
    return {'printed_reference': printed_reference, 'lookup_reference': lookup,
            'book': book, 'chapter': chapter, 'verses': verses,
            'partial_marker': match[2] if match else None, 'component_version': version}


def _span(operation, verse, cursor):
    start, end = operation['start'], operation['end']
    if (type(start) is not int or type(end) is not int
            or start != cursor or not start < end <= len(verse)):
        _hold('component_coverage', 'Verse components must cover every code point once in order')
    if operation['text'] != verse[start:end]:
        _hold('canonical_component_changed', 'Component differs from exact frozen verse bytes')
    return end


def _word_join(verse, position):
    def word(c):
        category = unicodedata.category(c)
        return c.isalnum() or category.startswith('M') or category in ('Pd', 'Cf') or c in "'’"
    return 0 < position < len(verse) and word(verse[position-1]) and word(verse[position])


def validate_components(verse, quote, operations, *, version):
    """Check complete verse partition and quote; morphology remains unsupported.

Only boundary omissions are implemented. Subword insertions are representable,
but subword/joined-word replacements and interior omissions remain held. Ordinary
whole-word replacement semantics always require independent review.
"""
    _version(version)
    if (not isinstance(verse, str) or not verse.strip() or not isinstance(quote, str)
            or not quote.strip() or not isinstance(operations, list)
            or not 1 <= len(operations) <= MAX_COMPONENTS):
        _hold('component_schema', 'Nonempty verse, quotation and bounded operations required')
    try:
        size = len(canonical({'verse': verse, 'quote': quote, 'operations': operations}))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ScriptureAttention('component_schema', 'Components must be finite UTF-8 JSON') from exc
    if size > MAX_COMPONENT_BYTES:
        _hold('component_size_limit', 'Complete components exceed the bound; never truncate')
    cursor, rendered, shape, authored_ids = 0, [], [], set()
    canonical_words = False
    omissions = {'leading': False, 'trailing': False}
    for operation in operations:
        if not isinstance(operation, dict):
            _hold('component_schema', 'Each component must be an object')
        kind = operation.get('kind')
        if kind == 'canonical':
            _keys(operation, ('kind', 'start', 'end', 'text'))
            cursor = _span(operation, verse, cursor)
            has_words = any(c.isalnum() for c in operation['text'])
            rendered.append(operation['text'])
            shape.append({'kind': kind, 'has_words': has_words})
            canonical_words = canonical_words or has_words
        elif kind == 'omit':
            _keys(operation, ('kind', 'start', 'end', 'text', 'reason'))
            cursor = _span(operation, verse, cursor)
            if (operation['reason'] != 'excerpt_boundary'
                    or (operation['start'] != 0 and cursor != len(verse))
                    or _word_join(verse, operation['start']) or _word_join(verse, cursor)):
                _hold('morphology_or_interior_omission', 'Only whole-boundary excerpt omission is supported')
            if any(c.isalnum() for c in operation['text']):
                omissions['leading' if operation['start'] == 0 else 'trailing'] = True
        elif kind in ('insert', 'replace', 'punctuation'):
            fields = ('kind', 'id', 'at', 'authored_text') if kind != 'replace' else (
                'kind', 'id', 'start', 'end', 'text', 'authored_text')
            _keys(operation, fields)
            identity = operation['id']
            if (not isinstance(identity, str) or not re.fullmatch(r'a[1-9][0-9]*', identity)
                    or identity in authored_ids):
                _hold('authored_component_identity', 'Authored IDs must be unique and explicit')
            authored_ids.add(identity)
            if kind == 'replace':
                cursor = _span(operation, verse, cursor)
                if not any(c.isalnum() for c in operation['text']):
                    _hold('authored_component_text', 'Replacement must replace canonical lexical content')
                if (_word_join(verse, operation['start']) or _word_join(verse, cursor)
                        or any(unicodedata.category(c) in ('Pd', 'Cf') for c in operation['text'])):
                    _hold('morphology_or_subword_replacement', 'Subword replacements need a separately reviewed contract')
            elif type(operation['at']) is not int or operation['at'] != cursor:
                _hold('component_coverage', 'Authored component is not at the current verse boundary')
            authored = operation['authored_text']
            if not isinstance(authored, str) or not authored:
                _hold('authored_component_text', 'Authored text must be explicit and nonempty')
            if any(unicodedata.category(c).startswith('C') for c in authored):
                _hold('authored_component_text', 'Control and invisible formatting characters are unsupported')
            if kind == 'punctuation':
                if not any(not c.isspace() for c in authored) or any(
                        not c.isspace() and not unicodedata.category(c).startswith('P') for c in authored):
                    _hold('authored_component_text', 'Punctuation cannot carry generated words')
            elif (not re.fullmatch(r'[ \t]*\[[^\[\]{}\r\n]+\][ \t]*', authored)
                  or not any(c.isalnum() for c in authored.strip()[1:-1])):
                _hold('authored_component_text', 'Insertions and replacements require visible square brackets')
            rendered.append(authored)
            shape.append({'kind': kind, 'id': identity, 'text': authored})
        else:
            _hold('component_kind', 'Unknown component or unimplemented morphological adaptation')
    if cursor != len(verse) or not canonical_words:
        _hold('component_coverage', 'Complete verse partition and retained canonical text required')
    if ''.join(rendered) != quote:
        _hold('component_quote_mismatch', 'Components do not reconstruct the complete printed quotation')
    return {'component_version': version, 'verse_sha256': json_hash(verse),
            'quote_sha256': json_hash(quote), 'components_sha256': json_hash(operations),
            'shape': shape, 'omission_topology': omissions, 'independent_review_required': True}


def validate_component_pair(source, target, *, printed_reference, version):
    """Prepare review input, never attest translation correctness or provenance."""
    _version(version)
    for side in (source, target):
        _keys(side, ('verse', 'quote', 'operations'))
    reference = decode_component_reference(printed_reference, version=version)
    if len(reference['verses']) != 1:
        _hold('component_multi_verse', 'This component contract supports one verse only')
    proofs = [validate_components(side['verse'], side['quote'], side['operations'], version=version)
              for side in (source, target)]
    source_shape, target_shape = (proof['shape'] for proof in proofs)
    if proofs[0]['omission_topology'] != proofs[1]['omission_topology']:
        _hold('component_excerpt_changed', 'Lexical excerpt boundaries must correspond on both sides')
    if len(source_shape) != len(target_shape):
        _hold('authored_component_mapping', 'Source and target component inventories differ')
    for original, translated in zip(source_shape, target_shape):
        if (original['kind'] != translated['kind'] or original.get('id') != translated.get('id')
                or original.get('has_words') != translated.get('has_words')
                or (original['kind'] == 'punctuation' and original['text'] != translated['text'])):
            _hold('authored_component_mapping', 'Authored kind, identity, order or punctuation changed')
    return {'component_version': version, 'reference': reference,
            'source': proofs[0], 'target': proofs[1],
            'pair_sha256': json_hash({'source': source, 'target': target, 'reference': reference}),
            'independent_review_required': True, 'runtime_admission_supported': False}
