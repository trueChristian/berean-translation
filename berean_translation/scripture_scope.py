"""Version-2 structural quotation proofs over immutable, decoded HTML.

The two supported undelimited shapes are deliberately finite: a trailing em
subtree in a paragraph, or a whole paragraph inside a blockquote.  A quote must
start at that structural boundary and leave only its one printed citation.
"""
from __future__ import annotations

import re

from .common import ContractError, json_hash
from .html import Fragment, TEXT_BLOCKS, VOID
from .reference_notation import reference_mentions
from .scripture_citations import quotation_reference_mentions


# These limits are part of normalization version 2. They bound parser work,
# rather than assigning semantic authority to model-supplied numeric offsets.
MAX_SCOPE_HTML_BYTES = 2_000_000
MAX_SCOPE_NODES = 50_000
MAX_SCOPE_DEPTH = 128
_TAIL_SPACE = ' \t\r\n\f\v\u00a0\u202f'
_ATTRIBUTION_DASHES = frozenset('—–-‐‑−')
_TERMINAL = frozenset('.;。；')


class ScopeProofError(ContractError):
    pass


class ScopeFragment(Fragment):
    """Use the strict production parser; additionally retain inline text bounds."""
    def __init__(self, text, article_id):
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_SCOPE_HTML_BYTES:
            raise ScopeProofError('Structural quotation HTML exceeds its versioned parser bound')
        self.nodes = {}
        self.lengths = {}
        super().__init__(text, article_id)
        self.blocks = {path: ''.join(parts) for path, parts in self.text_by_block.items()}

    def handle_starttag(self, tag, attrs):
        if len(self.nodes) >= MAX_SCOPE_NODES or len(self.stack) >= MAX_SCOPE_DEPTH:
            raise ScopeProofError('Structural quotation DOM exceeds its versioned parser bound')
        parent = self.path_stack[-1] if self.path_stack else ''
        super().handle_starttag(tag, attrs)
        path = self.signature_paths[-1]
        block = next((p for t,p in zip(reversed(self.stack), reversed(self.path_stack))
                      if t in TEXT_BLOCKS), None)
        start = self.lengths.get(block, 0)
        self.nodes[path] = {'tag': tag, 'parent': parent, 'block': block,
                            'start': start, 'end': start if tag in VOID else None}

    def handle_endtag(self, tag):
        path = self.path_stack[-1] if self.path_stack else None
        super().handle_endtag(tag)
        node = self.nodes[path]
        node['end'] = self.lengths.get(node['block'], 0)

    def handle_data(self, data):
        super().handle_data(data)
        if self.stack:
            block = next(p for t,p in zip(reversed(self.stack), reversed(self.path_stack))
                         if t in TEXT_BLOCKS)
            self.lengths[block] = self.lengths.get(block, 0) + len(data)


def citation_tail_matches(text, identity, *, evidence=None):
    """Consume the entire tail, allowing only a single verified citation."""
    mentions = (quotation_reference_mentions(evidence, text) if evidence is not None
                else reference_mentions(text, 'eng'))
    if len(mentions) != 1:
        return False
    start,end,key = mentions[0]
    if key != identity or start >= end:
        return False
    # Peel a finite grammar linearly. Overlapping optional whitespace regexes
    # would make malformed, long tails needlessly expensive to reject.
    prefix = text[:start].strip(_TAIL_SPACE)
    if prefix.startswith('——'):
        prefix = prefix[2:].lstrip(_TAIL_SPACE)
    elif prefix and prefix[0] in _ATTRIBUTION_DASHES:
        prefix = prefix[1:].lstrip(_TAIL_SPACE)
    if prefix not in ('','(','['):
        return False
    wrapper = prefix
    suffix = text[end:].strip(_TAIL_SPACE)
    if wrapper:
        close = {'(':')','[':']'}[wrapper]
        # One terminal punctuation mark may sit inside or outside one balanced
        # wrapper. No ellipsis, nested bracket, or arbitrary punctuation runs.
        outer_terminal = bool(suffix and suffix[-1] in _TERMINAL)
        if outer_terminal:
            suffix = suffix[:-1].rstrip(_TAIL_SPACE)
        if not suffix.endswith(close):
            return False
        suffix = suffix[:-1].rstrip(_TAIL_SPACE)
        return not suffix or (not outer_terminal and suffix in _TERMINAL)
    return not suffix or suffix in _TERMINAL


class StructuralScope:
    def __init__(self, evidence, source, candidate):
        if not isinstance(source, dict) or json_hash(source) != evidence['source_sha256']:
            raise ScopeProofError('Structural proof requires the unchanged frozen English snapshot')
        self.source = ScopeFragment(source['html'], evidence['article_id'])
        self.target = ScopeFragment(candidate['html'], evidence['article_id'])
        if (self.source.signature != self.target.signature
                or self.source.signature_paths != self.target.signature_paths):
            raise ScopeProofError('Quotation structural HTML differs from its frozen English snapshot')
        self.evidence = evidence
        self.proofs = {}

    def prove(self, quote, expected):
        """Return independently proven candidate bounds, never provider offsets."""
        if expected not in self.proofs:
            self.proofs[expected] = self._account_occurrences(expected)
        proof = self.proofs[expected].get(quote['id'])
        if proof is None:
            raise ScopeProofError('Quotation has no complete source-backed structural proof')
        return proof

    @staticmethod
    def _complete(quote):
        alignment = quote['alignment']
        return (alignment['kind'] == 'complete' and len(alignment['fragments']) == 1
                and not alignment['ellipsis'] and not alignment['leading_ellipsis']
                and not alignment['trailing_ellipsis'])

    def _source_bounds(self, quote):
        if not self._complete(quote):
            raise ScopeProofError('Undelimited proof requires a complete unambiguous quotation')
        source = self.source.blocks.get(quote['block'], '')
        start,end = quote['source_start'],quote['source_end']
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source)
                or source[start:end] != quote['source_quote']):
            raise ScopeProofError('Frozen quotation bounds disagree with the source snapshot')
        return source,start,end

    def _account_occurrences(self, expected):
        # Repeated source quotations are legitimate only when each occurrence
        # has its own frozen, independently proved scope. Never let a second
        # occurrence in prose borrow an already-used proof.
        quotes = [q for q in self.evidence['quotes'] if self._complete(q)
                  and ' '.join(v['text'].strip() for v in q['target_verses']) == expected]
        actual = set()
        for path,text in self.target.blocks.items():
            position = text.find(expected)
            while position >= 0:
                actual.add((path,position,position+len(expected)))
                if len(actual) > len(quotes):
                    raise ScopeProofError('Complete quotation has an extra unclaimed article occurrence')
                position = text.find(expected,position+1)
        proofs,covered,delimited = {},set(),{}
        for quote in quotes:
            self._source_bounds(quote)
            if quote.get('delimited'):
                delimited.setdefault(quote['block'],[]).append(quote)
                continue
            start,end = self._prove_structural(quote,expected)
            location = (quote['block'],start,end)
            if location in covered:
                raise ScopeProofError('Different quotations cannot reuse one structural occurrence')
            proofs[quote['id']] = (start,end)
            covered.add(location)
        # Import only at runtime to share the exact frozen delimiter grammar
        # without a module-initialization cycle or a second parser definition.
        from .scripture_evidence import _output_quote_spans
        for path,group in delimited.items():
            spans = [(start,end) for start,end,text in _output_quote_spans(self.target.blocks.get(path,''),path)
                     if text == expected]
            if len(spans) != len(group):
                raise ScopeProofError('Every frozen delimited quotation requires one exact occurrence')
            for quote,(start,end) in zip(sorted(group,key=lambda q:q['source_start']),spans):
                location = (path,start,end)
                if location in covered:
                    raise ScopeProofError('Different quotations cannot reuse one delimited occurrence')
                proofs[quote['id']] = (start,end)
                covered.add(location)
        if actual != covered or len(proofs) != len(quotes):
            raise ScopeProofError('Exact quotation occurrences and frozen scopes disagree')
        return proofs

    def _prove_structural(self, quote, expected):
        source,start,end = self._source_bounds(quote)
        block_path = quote['block']
        target = self.target.blocks.get(block_path, '')
        position = target.find(expected)
        if position < 0:
            raise ScopeProofError('Complete frozen quotation is absent from its original block')
        target_end = position + len(expected)

        # Whole blockquote paragraph, including quotations split over adjacent
        # strong/em subtrees. No prose may precede or follow the citation tail.
        if re.fullmatch(r'.*/blockquote\[[1-9][0-9]*\]/p\[[1-9][0-9]*\]', block_path):
            if source[:start].strip() or target[:position].strip():
                raise ScopeProofError('Blockquote quotation must begin at its paragraph boundary')
            scope_path = block_path
        else:
            # A source-backed trailing emphasis subtree, never a matching
            # arbitrary substring within ordinary prose or another em node.
            nodes = [(path,node) for path,node in self.source.nodes.items()
                     if node['tag'] == 'em' and node['parent'] == block_path
                     and node['block'] == block_path and node['start'] == start
                     and node['end'] is not None and node['end'] >= end
                     and not source[node['end']:].strip()]
            if (not re.search(r'/p\[[1-9][0-9]*\]$', block_path)
                    or not source[:start].strip() or len(nodes) != 1):
                raise ScopeProofError('Quotation lacks a unique frozen trailing emphasis boundary')
            path,node = nodes[0]
            scope_path = path
            candidate_node = self.target.nodes.get(path)
            if (not candidate_node or candidate_node['start'] != position
                    or candidate_node['end'] < target_end
                    or target[candidate_node['end']:].strip()):
                raise ScopeProofError('Quotation moved outside its frozen emphasis subtree')
            # Citation itself must remain in the same trailing subtree.
            if (not citation_tail_matches(source[end:node['end']], quote['reference'])
                    or not citation_tail_matches(target[target_end:candidate_node['end']],
                                                 quote['reference'], evidence=self.evidence)):
                raise ScopeProofError('Quotation subtree must end with exactly its verified citation')
        if any(node['tag'] in TEXT_BLOCKS for path,node in self.source.nodes.items()
               if path.startswith(scope_path+'/')):
            raise ScopeProofError('Quotation scope may contain only inline descendant elements')
        if (not citation_tail_matches(source[end:], quote['reference'])
                or not citation_tail_matches(target[target_end:], quote['reference'], evidence=self.evidence)):
            raise ScopeProofError('Quotation must end with exactly its verified citation and no prose')
        return position,target_end
