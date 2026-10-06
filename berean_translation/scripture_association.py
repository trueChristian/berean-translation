"""Version-2 explicit source quotation/citation association.

Shapes, not Bible-like vocabulary or matching substrings, establish scope.
The finite English aliases below are attested in the two pinned 2019 sources.
No provider lookup, source mutation, or semantic approval happens here.
"""
from __future__ import annotations

import re
from collections import Counter

from .common import json_hash
from .reference_notation import reference_mentions
from .scripture_books import CORE_BOOK_NAMES
from .scripture_scope import ScopeFragment, ScopeProofError

VERSION = '2'
SOURCE_ALIASES = (('Exo', 'Exodus'), ('Pro', 'Proverbs'), ('Jam', 'James'),
                  ('1 Kin', '1 Kings'), ('Act', 'Acts'))
_LANGUAGES = {'de': 'deu', 'he': 'heb', 'it': 'ita', 'sv': 'swe',
              'af': 'afr', 'nb': 'nob'}
_DASHES = '—–-‐‑−'
_TERMINAL = '.;,。；，'
MAX_ASSOCIATION_WORK = 2_000_000
# Independent delimiter patterns retain nested quotations of another style.
_PAIRS = (('“', '”'), ('"', '"'), ('‘', '’'), ('«', '»'), ('„', '“'),
          ('「', '」'), ('『', '』'))
_ALLUSION_BEFORE = re.compile(
    r'(?:\b(?:as\s+)?(?:we\s+)?read\s+(?:about\s+this\s+)?in|'
    r'\bas\s+(?:taught|we\s+find)\s+in|'
    r'\bconsider\s+[^.?!“”"‘’]{1,80}\s+in|'
    r'\bfrom\s+the\s+words\s+of\s+Paul\s+in)\s*$', re.I)


class AssociationError(ScopeProofError):
    def __init__(self, reason, detail):
        self.reason = reason
        super().__init__(detail)


def mentions(text, evidence=None):
    aliases = dict(SOURCE_ALIASES)
    if evidence is not None:
        for quote in evidence['quotes']:
            native = evidence['lookups'][quote['target_lookup']]['result']['data']['book_name']
            identity = CORE_BOOK_NAMES[quote['book'] - 1][0]
            if native in aliases and aliases[native] != identity:
                raise AssociationError('ambiguous_quote_reference', 'Conflicting frozen book name')
            aliases[native] = identity
    language = _LANGUAGES.get((evidence or {}).get('language_tag'), 'eng')
    native_aliases = tuple(sorted(aliases.items()))
    result = reference_mentions(text, language, native_aliases=native_aliases)
    repaired = []
    for left, right, key in result:
        # The shared legacy parser conservatively calls an attached trailing
        # dash a dangling range. Only a known, complete citation immediately
        # followed by a marked quote qualifies that dash as attribution here.
        if (key.startswith('!invalid') and text[right-1:right] in _DASHES
                and text[right:].lstrip()[:1] in {pair[0] for pair in _PAIRS}):
            inner = reference_mentions(text[left:right-1], language, native_aliases=native_aliases)
            if len(inner) == 1 and inner[0][0] == 0 and inner[0][1] == right-left-1 and not inner[0][2].startswith('!'):
                right, key = right - 1, inner[0][2]
        repaired.append((left, right, key))
    return repaired


def delimited_spans(text):
    spans = []
    for opening, closing in _PAIRS:
        pattern = re.escape(opening) + '([^' + re.escape(closing) + '\\n]+)' + re.escape(closing)
        for match in re.finditer(pattern, text):
            # Apostrophes within words are not speech delimiters.
            if opening == '‘' and ((match.start() and text[match.start()-1].isalnum())
                    or (match.end() < len(text) and text[match.end()].isalnum())):
                continue
            start, end = match.start(1), match.end(1)
            while start < end and text[start].isspace(): start += 1
            while start < end and text[end-1].isspace(): end -= 1
            if start < end:
                spans.append({'start': start, 'end': end, 'outer_start': match.start(),
                              'outer_end': match.end(), 'kind': 'delimited'})
    return sorted(spans, key=lambda s: (s['start'], -s['end']))


def delimiter_topology(text):
    """Delimiter count/nesting is fixed; translated glyph styles may differ."""
    spans = delimited_spans(text)
    parents = []
    for index, span in enumerate(spans):
        ancestors = [i for i, other in enumerate(spans[:index])
                     if other['start'] <= span['outer_start']
                     and span['outer_end'] <= other['end']]
        parents.append(ancestors[-1] if ancestors else None)
    return parents


def _suffix_ok(value, wrapper):
    value = value.strip()
    if wrapper:
        close = {'(': ')', '[': ']'}[wrapper]
        if value and value[-1:] in _TERMINAL: value = value[:-1].rstrip()
        if not value.endswith(close): return False
        value = value[:-1].rstrip()
    return not value or value in _TERMINAL


def _tail(text, left, right, end, *, allow_plain=False):
    """Return the quote end for one complete attribution tail, or None."""
    before = text[:left].rstrip()
    wrapper = before[-1:] if before[-1:] in ('(', '[') else ''
    if wrapper: before = before[:-1].rstrip()
    if before and before[-1:] in _DASHES: before = before[:-1].rstrip()
    elif not wrapper and not allow_plain: return None
    if not _suffix_ok(text[right:end], wrapper): return None
    return len(before)


def _allusion(text, left, right, marked=()):
    if marked and not any(start <= left and right <= end for start, end in marked):
        return False  # Prose outside a marked quote cannot discharge its scope.
    if any(start >= right and re.fullmatch(r'[\s.!?;:()\[\]—–-]*', text[right:start])
           for start, _ in marked):
        return False  # A prose cue may introduce a direct marked quotation.
    return bool(_ALLUSION_BEFORE.search(text[:left]) or
                (re.search(r'\bas\s*$', text[:left], re.I)
                 and re.match(r'\s+says\b', text[right:], re.I)))


def _sentence_bounds(text, marked, refs, *, marked_terminals=True):
    protected = marked + [(r['start'], r['end']) for r in refs]
    boundaries = [0] + [m.end() for m in re.finditer(r'[.!?]\s+', text)
                       if not any(a <= m.start() < b for a, b in protected)] + [len(text)]
    for left, right in marked if marked_terminals else ():
        tail = text[left:right].rstrip('”"’»」』 ').rstrip()
        after = text[right:].lstrip()
        if tail[-1:] in ('.', '!', '?') and after[:1].isupper():
            boundaries.append(right)
    return sorted(set(boundaries))


def _prose_allusion(text, ref, marked, refs, block, *, unresolved_delimiters=False):
    """A citation in a separate, wholly unmarked prose sentence owns itself.

    Sentence punctuation inside a quote, emphasis, or citation cannot sever
    the scope and turn its following attribution into unrelated prose.
    Bare/unknown reference identities and blockquotes never use this rule.
    """
    if (unresolved_delimiters or '/blockquote[' in block
            or not any(ref['identity'].startswith(row[0] + ' ') for row in CORE_BOOK_NAMES)):
        return False
    opening = text.rfind('(', 0, ref['start'])
    closing = text.find(')', ref['end'])
    if opening < 0 or closing < 0 or ')' in text[opening:ref['start']] or '(' in text[ref['end']:closing]:
        return False
    boundaries = _sentence_bounds(text, marked, refs)
    left = max(value for value in boundaries if value <= ref['start'])
    right = min(value for value in boundaries if value >= ref['end'])
    if any(left < b and a < right for a, b in marked):
        return False
    # A prose sentence can introduce a direct quotation in the following
    # sentence. Do not discharge that nearby citation as unrelated prose.
    if any(a >= right and not text[right:a].strip() for a, _ in marked):
        return False
    prose = text[left:ref['start']] + text[ref['end']:right]
    return len(re.findall(r'[A-Za-z]+', prose)) >= 3


def structure_hash(parsed):
    return json_hash([parsed.signature, parsed.signature_paths])


def _endpoint_paths(parsed, block, position, *, end=False):
    return [path for path, node in parsed.nodes.items()
            if path != block and node['block'] == block and node['end'] is not None
            and (node['start'] < position <= node['end'] if end else
                 node['start'] <= position < node['end'])]


def unclaimed_marked_spans(parsed, quotes):
    """Keep otherwise ordinary marked text visible to the English backstop.

    This supplies no guessed citation or Scripture authority. A subsequent
    exact English match can only hold the evidence for attention.
    """
    remaining = []
    seen = set()
    for block, text in parsed.blocks.items():
        spans = [(s['start'], s['end']) for s in delimited_spans(text)]
        spans += [(n['start'], n['end']) for n in parsed.nodes.values()
                  if n['tag'] == 'em' and n['block'] == block and n['end'] is not None]
        for start, end in spans:
            while start < end and text[start].isspace(): start += 1
            while start < end and text[end-1].isspace(): end -= 1
            if start >= end or (block, start, end) in seen:
                continue
            seen.add((block, start, end))
            if any(q['block'] == block and start < q['source_end'] and q['source_start'] < end for q in quotes):
                continue
            remaining.append({'block': block, 'start': start, 'end': end, 'text': text[start:end]})
    return remaining


def _after_bridge(text, outer_end, ref):
    bridge = text[outer_end:ref['start']].strip()
    if bridge[:1] in ('.', ',', ';', ':') and bridge[1:].strip() in ('', '(', '['):
        bridge = bridge[1:].strip()
    if bridge and bridge[:1] in _DASHES:
        bridge = bridge[1:].lstrip()
    if bridge in ('', '(', '['):
        wrapper = bridge if bridge in ('(', '[') else ''
        tail = re.match(r'\s*[)\]]?\s*[.,。，]?', text[ref['end']:])
        if not _suffix_ok(text[ref['end']:ref['end'] + tail.end()], wrapper):
            raise AssociationError('ambiguous_quote_reference', 'Malformed adjacent citation')
        return 'punctuation'
    if re.fullmatch(r',?\s*(?:says|saith|said|reads|states|declares)\s*', bridge, re.I):
        return 'attribution'
    return None


def _before_bridge(text, ref, outer_start):
    bridge = text[ref['end']:outer_start].strip()
    if not bridge or bridge in (':', ',', *_DASHES):
        return 'punctuation'
    if re.fullmatch(r',?\s*(?:says|saith|reads|saying|tells us that|the Bible says)\s*[:,]?', bridge, re.I):
        return 'attribution'
    return None


def _frozen_external(evidence, block, shape, ref, side):
    if evidence is None:
        return False
    for quote in evidence['quotes']:
        a = quote['association']
        if (quote['block'] == block and a['kind'] == shape['kind']
                and a['path'] == shape['path'] and a['external']
                and a['citation_side'] == side and a['external_mode'] == 'attribution'
                and a['reference_ordinal'] == ref['ordinal']
                and a['delimiter_ordinal'] == shape.get('ordinal')):
            # The English attribution is translated prose, not Scripture.
            # Frozen reference occurrence, delimiter ordinal, and DOM endpoint
            # proofs establish the target association without English cues.
            return True
    return False


def _external_shape(text, block, shape, refs, evidence):
    following = [r for r in refs if r['start'] >= shape['outer_end']]
    if following:
        ref = following[0]
        mode = ('attribution' if _frozen_external(evidence, block, shape, ref, 'after')
                else _after_bridge(text, shape['outer_end'], ref))
        if mode:
            if any(re.fullmatch(r'[\s;,()\[\]—–-]*', text[ref['end']:r['start']])
                   for r in following[1:]):
                raise AssociationError('ambiguous_quote_reference', block)
            return {**shape, 'refs': [ref], 'external': True, 'external_mode': mode}
    preceding = [r for r in refs if r['end'] <= shape['outer_start']]
    if preceding:
        ref = preceding[-1]
        mode = ('attribution' if _frozen_external(evidence, block, shape, ref, 'before')
                else _before_bridge(text, ref, shape['outer_start']))
        if mode:
            return {**shape, 'refs': [ref], 'external': True, 'external_mode': mode}
    return None


def associate(html, article_id, *, evidence=None):
    """Inventory every citation and explicitly bounded quotation without I/O.

    Uncited/unmarked prose remains for independent semantic review. Cited
    emphasis and citation-bearing speech with no definite shape/allusion fail
    closed. Multiple references in independent scopes are supported.
    """
    parsed = ScopeFragment(html, article_id)
    # Freeze a conservative combinatorial bound as well as parser bytes/nodes.
    # All inventories remain complete; an oversized shape graph is a hold.
    work = len(parsed.nodes) * len(parsed.blocks)
    if work > MAX_ASSOCIATION_WORK:
        raise AssociationError('source_association_limit', 'Source association exceeds its versioned work bound')
    references, quotes, ordinary = [], [], []
    for block, text in parsed.blocks.items():
        refs = [{'block': block, 'start': left, 'end': right, 'printed_text': text[left:right],
                 'identity': key, 'classification': 'reference_only', 'ordinal': ordinal}
                for ordinal, (left, right, key) in enumerate(mentions(text, evidence))]
        references.extend(refs)
        if evidence is not None:
            # Classification belongs to the frozen citation occurrence, not
            # its identity alone: an allusion may cite the same verse as a
            # nearby genuine quotation. Translated cues need no English regex.
            frozen = [r for r in evidence['references'] if r['block'] == block]
            if any(q['block'] == block for q in evidence['quotes']):
                if [r['identity'] for r in refs] != [r['identity'] for r in frozen]:
                    raise AssociationError('citation_identity_changed', 'Printed citation identities/order/counts changed')
                refs = [r for r, before in zip(refs, frozen) if before['classification'] == 'scripture_quotation']
            else:
                refs = []
        delimiters = delimited_spans(text)
        all_marked = [(s['outer_start'], s['outer_end']) for s in delimiters]
        all_marked += [(n['start'], n['end']) for n in parsed.nodes.values()
                       if n['tag'] == 'em' and n['block'] == block and n['end'] is not None]
        em_count = sum(n['tag'] == 'em' and n['block'] == block for n in parsed.nodes.values())
        work += (em_count * (len(parsed.nodes) + len(refs) + len(delimiters))
                 + len(delimiters) * (len(refs) + len(delimiters) + em_count)
                 + (em_count + len(delimiters)) ** 2)
        if work > MAX_ASSOCIATION_WORK:
            raise AssociationError('source_association_limit', 'Source association exceeds its versioned work bound')
        for ordinal, span in enumerate(delimiters):
            span['ordinal'] = ordinal
        shapes = []
        allusions = set()
        # The smallest containing emphasis owns a citation. Outer prose/em
        # cannot swallow an independently cited nested quotation.
        for path, node in parsed.nodes.items():
            if node['tag'] != 'em' or node['block'] != block or node['end'] is None:
                continue
            contained = [r for r in refs if node['start'] <= r['start'] and r['end'] <= node['end']]
            nested = [n for p, n in parsed.nodes.items() if p.startswith(path + '/')
                      and n['tag'] == 'em' and n['block'] == block and n['end'] is not None]
            contained = [r for r in contained if not any(n['start'] <= r['start']
                         and r['end'] <= n['end'] for n in nested)]
            if not contained:
                # Separate emphasis and following/preceding citation are one
                # finite marked scope, not a paragraph-wide text search.
                left, right = node['start'], node['end']
                while left < right and text[left].isspace(): left += 1
                while left < right and text[right-1].isspace(): right -= 1
                if left < right and not any(s['outer_start'] <= left < s['outer_end']
                        or s['outer_start'] < right <= s['outer_end'] for s in delimiters):
                    external = _external_shape(text, block, {'start': left, 'end': right,
                        'outer_start': left, 'outer_end': right, 'kind': 'em', 'path': path}, refs, evidence)
                    if external is not None:
                        shapes.append(external)
                continue
            if (len(contained) == 1
                    and not text[node['start']:contained[0]['start']].strip(' \t\r\n([—–-')
                    and not text[contained[0]['end']:node['end']].strip(' \t\r\n)].;,。；，')):
                continue  # Styling just the citation does not mark a quote.
            if any(not text[node['start']:s['outer_start']].strip()
                   and not text[s['outer_end']:node['end']].strip()
                   and node['start'] <= s['outer_start'] < s['outer_end'] <= node['end']
                   for s in delimiters):
                continue
            shapes.append({'start': node['start'], 'end': node['end'], 'kind': 'em',
                           'path': path, 'refs': contained})
        for span in sorted(delimiters, key=lambda s: s['end'] - s['start']):
            contained = [r for r in refs if span['start'] <= r['start'] and r['end'] <= span['end']]
            contained = [r for r in contained if not any(r in s['refs']
                         and span['start'] <= s['start'] and s['end'] <= span['end'] for s in shapes)]
            contained = [r for r in contained if not any(s is not span and span['start'] <= s['outer_start']
                         and s['outer_end'] <= span['end'] and s['start'] <= r['start']
                         and r['end'] <= s['end'] for s in delimiters)]
            if contained:
                if all(_allusion(text, r['start'], r['end'], all_marked) for r in contained):
                    if any(span['start'] <= left and right <= span['end']
                           and not any(s['start'] <= left and right <= s['end']
                                       or left <= s['start'] and s['end'] <= right for s in shapes)
                           for left, right in all_marked):
                        raise AssociationError('unresolved_quote_reference', f'{block}: nested marked quotation lacks its own scope')
                    allusions.update((r['start'], r['end']) for r in contained)
                    ordinary.append({'block': block, **span, 'classification': 'cited_allusion'})
                else:
                    shapes.append({**span, 'path': block, 'refs': contained})
            else:
                external = _external_shape(text, block, {**span, 'path': block}, refs, evidence)
                if external is not None:
                    shapes.append(external)
                    continue
                ordinary.append({'block': block, **span, 'classification': 'ordinary_quotation'})
        # Unmarked text is accepted only as an entire paragraph immediately
        # followed by its attribution, never via a verse substring in prose.
        if not shapes and len(refs) == 1 and not delimiters:
            ref = refs[0]
            plain_tail = '/blockquote[' in block
            end = _tail(text, ref['start'], ref['end'], len(text), allow_plain=plain_tail)
            if end is not None and text[:end].strip():
                shapes.append({'start': 0, 'end': len(text), 'kind': 'paragraph',
                               'path': block, 'refs': [ref], 'plain_tail': plain_tail})
            elif re.search(r'\b(?:written|saith|scripture says|Bible says)\b', text, re.I):
                raise AssociationError('unmarked_quote_scope', block)
        used = set()
        used_bounds = {}
        for shape in sorted(shapes, key=lambda s: s['start']):
            if len(shape['refs']) != 1:
                raise AssociationError('ambiguous_quote_reference', block)
            ref = shape['refs'][0]
            location = (ref['start'], ref['end'])
            # An em nested entirely within delimiters is the same explicit
            # scope, not a second independent quotation.
            if shape.get('external'):
                start, end = shape['start'], shape['end']
            else:
                start = shape['start']
                end = _tail(text, ref['start'], ref['end'], shape['end'], allow_plain=shape.get('plain_tail', False))
                if end is None:
                    raise AssociationError('ambiguous_quote_reference', f'{block}: citation lacks a complete explicit scope')
                while start < end and text[start].isspace(): start += 1
                # Delimiters around the quotation inside emphasis remain
                # part of the frozen shape, not Scripture words.
                inner = [s for s in delimiters if s['outer_start'] == start and s['outer_end'] == end]
                if len(inner) == 1: start, end = inner[0]['start'], inner[0]['end']
            if start >= end:
                raise AssociationError('ambiguous_quote_reference', block)
            if location in used:
                if used_bounds[location] == (start, end):
                    continue
                raise AssociationError('ambiguous_quote_reference', f'{block}: distinct marked scopes share one citation')
            if any(start < q['source_end'] and q['source_start'] < end for q in quotes if q['block'] == block):
                raise AssociationError('ambiguous_quote_reference', f'{block}: overlapping Scripture scopes')
            used.add(location)
            used_bounds[location] = (start, end)
            ref['classification'] = 'scripture_quotation'
            quotes.append({'block': block, 'source_start': start, 'source_end': end,
                           'source_quote': text[start:end], 'reference': ref['identity'],
                           'printed_reference': ref['printed_text'], 'delimited': shape['kind'] == 'delimited',
                           'association': {'kind': shape['kind'], 'path': shape['path'],
                                           'delimiter_ordinal': shape.get('ordinal'),
                                           'citation_side': ('before' if ref['end'] <= start else
                                                             'after' if shape.get('external') else 'inside'),
                                           'quote_start_paths': _endpoint_paths(parsed, block, start),
                                           'quote_end_paths': _endpoint_paths(parsed, block, end, end=True),
                                           'reference_start_paths': _endpoint_paths(parsed, block, ref['start']),
                                           'reference_end_paths': _endpoint_paths(parsed, block, ref['end'], end=True),
                                           'reference_start': ref['start'], 'reference_end': ref['end'],
                                           'reference_ordinal': ref['ordinal'],
                                           'external_mode': shape.get('external_mode'),
                                           'plain_tail': bool(shape.get('plain_tail')),
                                           'external': bool(shape.get('external'))}})
        # Coverage is per block, including blocks which already yielded some
        # quotations. A successful first scope must not hide a second lost
        # quotation. Every marked/cited combination needs a scope or an
        # explicit allusion owner; unsupported aliases and bridges hold.
        marked = [(s['outer_start'], s['outer_end']) for s in delimiters]
        marked += [(n['start'], n['end']) for n in parsed.nodes.values()
                   if n['tag'] == 'em' and n['block'] == block and n['end'] is not None
                   and not any(n['start'] <= r['start'] and r['end'] <= n['end']
                               and not text[n['start']:r['start']].strip(' \t\r\n([—–-')
                               and not text[r['end']:n['end']].strip(' \t\r\n)].;,。；，') for r in refs)]
        block_quotes = [q for q in quotes if q['block'] == block]
        allusive_spans = [s for s in ordinary if s['block'] == block and s['classification'] == 'cited_allusion']
        unresolved_delimiters = any(not any(s['start'] <= q['source_start'] and q['source_end'] <= s['end']
                                           or q['source_start'] <= s['start'] and s['end'] <= q['source_end'] for q in block_quotes)
                                   and not any(a['outer_start'] <= s['outer_start'] and s['outer_end'] <= a['outer_end'] for a in allusive_spans)
                                   for s in delimiters)
        allusions.update((r['start'], r['end']) for r in refs if
                         _allusion(text, r['start'], r['end'], all_marked) or
                         _prose_allusion(text, r, marked, refs, block, unresolved_delimiters=unresolved_delimiters))
        for ref in refs:
            location = (ref['start'], ref['end'])
            ref['association_owner'] = ('scripture_scope' if location in used else
                'allusion' if location in allusions else 'unmarked_reference_only')
        unowned = [(r['start'], r['end']) for r in refs if (r['start'], r['end']) not in used | allusions]
        if unowned and (delimiters or em_count or '/blockquote[' in block):
            raise AssociationError('unresolved_quote_reference', f'{block}: marked material has unowned citations')
        if evidence is None and refs:
            boundaries = _sentence_bounds(text, marked, refs)
            for start, end in marked:
                # A nested quotation/container already has its explicit owner.
                if any(start <= q['source_start'] and q['source_end'] <= end
                       or q['source_start'] <= start and end <= q['source_end'] for q in block_quotes):
                    continue
                if any(s['outer_start'] <= start and end <= s['outer_end'] for s in allusive_spans):
                    continue
                following_quotes = [q for q in block_quotes if q['source_start'] >= end]
                if following_quotes:
                    next_quote = min(following_quotes, key=lambda q: q['source_start'])
                    bridge = text[end:next_quote['source_start']]
                    if not re.search(r'[.!?]', bridge) and re.search(r'\b(?:then|answer(?:ed|s)?|followed|continued|added|goes on)\b', bridge, re.I):
                        raise AssociationError('unresolved_quote_reference', f'{block}: continued marked quotation lacks its own scope')
                prefix = text[max(0, start-160):start]
                if re.search(r'\b(?:(?:he|it|Jesus|Christ|God|Lord|Paul|Peter|Moses|Scripture|Bible)\s+'
                             r'(?:said|says|saith|taught|teaches|answered|speaks)|'
                             r'(?:translated|rendered|translation|version))\b[^.!?]*$', prefix, re.I):
                    raise AssociationError('unresolved_quote_reference', f'{block}: attributed marked quotation lacks its own scope')
                left = max(value for value in boundaries if value <= start)
                right = min(value for value in boundaries if value >= end)
                if any(left <= r['start'] and r['end'] <= right for r in refs):
                    raise AssociationError('unresolved_quote_reference', f'{block}: marked quotation has no independent citation/allusion owner')
    return parsed, references, quotes, ordinary


class AssociationScope:
    """Prove candidate scope against the frozen v2 association inventory."""
    def __init__(self, evidence, source, candidate):
        self.evidence = evidence
        if source is not None and json_hash(source) != evidence['source_sha256']:
            raise ScopeProofError('Source differs from the frozen English snapshot')
        self.target, refs, quotes, _ = associate(candidate['html'], evidence['article_id'], evidence=evidence)
        if structure_hash(self.target) != evidence['source_structure_sha256']:
            raise ScopeProofError('Quotation structural HTML differs from frozen English')
        frozen = evidence['quotes']
        blocks = {q['block'] for q in frozen}
        if any(delimiter_topology(self.target.blocks.get(block, '')) !=
               evidence['source_delimiter_topology'].get(block) for block in blocks):
            raise ScopeProofError('Quotation delimiter topology differs from frozen English')
        if (Counter((r['block'], r['identity']) for r in refs if r['block'] in blocks)
                != Counter((r['block'], r['identity']) for r in evidence['references'] if r['block'] in blocks)):
            raise AssociationError('citation_identity_changed', 'Printed citation identities/counts changed')
        if len(quotes) != len(frozen):
            raise ScopeProofError('Candidate quotation associations differ from frozen English')
        self.proofs = {}
        for before, after in zip(frozen, quotes):
            keys = ('block', 'reference', 'delimited')
            shape_keys = ('kind', 'path', 'external', 'delimiter_ordinal', 'citation_side',
                          'quote_start_paths', 'quote_end_paths',
                          'reference_start_paths', 'reference_end_paths',
                          'reference_ordinal', 'external_mode', 'plain_tail')
            if (any(before[k] != after[k] for k in keys)
                    or any(before['association'][k] != after['association'][k] for k in shape_keys)):
                raise ScopeProofError('Quotation moved outside its frozen citation/structure association')
            self.proofs[before['id']] = (after['source_start'], after['source_end'], after['source_quote'])

    def prove(self, quote, expected):
        start, end, actual = self.proofs[quote['id']]
        if actual != expected:
            raise ScopeProofError('Selection must cover its complete frozen quotation scope')
        # Complete canonical words may repeat only in the corresponding
        # independently frozen scopes, never as an added ordinary occurrence.
        if quote['alignment']['kind'] == 'complete':
            claimed = {(q['block'], a, b) for q in self.evidence['quotes']
                       for a, b, value in (self.proofs[q['id']],) if value == expected}
            actual_locations = set()
            for path, text in self.target.blocks.items():
                position = text.find(expected)
                while position >= 0:
                    actual_locations.add((path, position, position + len(expected)))
                    position = text.find(expected, position + 1)
            if actual_locations != claimed:
                raise ScopeProofError('Complete quotation has an extra unclaimed article occurrence')
        return start, end
