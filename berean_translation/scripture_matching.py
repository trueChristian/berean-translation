"""Component-only source association v3: bounded exact-token backstop.

The reference v2 matcher remains in scripture_evidence. This implementation
changes its work model, never its matches: KMP reports every occurrence,
including overlaps, and every ellipsis fragment must still be globally unique
and ordered without overlap. No dialogue, short phrase or chapter is omitted.

Work units are deterministic conservative charges, not elapsed time: input
character scans/normalization, token and coordinate materialization, KMP loop
steps, and token equality (length check plus all characters when lengths agree).
Each charge is reserved BEFORE its operation. Cumulative allocation units bound
characters and scalar/container slots, including temporary patterns and output;
they are not a claim about Python RSS. Both limits fail closed. The v3 constants
and audit schema form part of the versioned algorithm and must not be retuned
for an existing frozen contract.
"""
from __future__ import annotations

from .scripture_evidence import WORDS, ELLIPSIS, ScriptureAttention

MEMORY_LIMIT = 500_000


class Budget:
    def __init__(self, maximum, *, memory_limit=MEMORY_LIMIT):
        self.maximum, self.memory_limit = maximum, memory_limit
        self.work = self.memory = 0
        self.charges = {}

    def charge(self, category, amount):
        if amount > self.maximum - self.work:
            raise ScriptureAttention('source_association_limit',
                'Complete v3 source backstop exceeds its versioned work bound')
        self.work += amount
        self.charges[category] = self.charges.get(category, 0) + amount

    def allocate(self, amount):
        if amount > self.memory_limit - self.memory:
            raise ScriptureAttention('source_association_limit',
                'Complete v3 source backstop exceeds its versioned memory bound')
        self.memory += amount

    def equal(self, left, right):
        # Unequal lengths need no character comparison. No hashes or
        # probabilistic fingerprints stand in for exact normalized equality.
        self.charge('token_comparisons', 1 + (len(left) if len(left) == len(right) else 0))
        return left == right

    def tokens(self, text, *, verse=None):
        # WORDS is the exact v2 expression; its ASCII tokens cannot expand on
        # casefold. Reserve scanning, group slicing, apostrophe replacement,
        # casefold and original-coordinate construction before doing any of it.
        self.charge('normalization', 6 * len(text) + 1)
        self.allocate(2 * len(text) + 8)
        tokens, coordinates = [], []
        for match in WORDS.finditer(text):
            self.charge('materialization', 8)
            self.allocate(32)
            tokens.append(match.group().replace('’', "'").casefold())
            coordinates.append({'verse': verse, 'start': match.start(), 'end': match.end()})
        return tokens, coordinates

    def audit(self):
        return {'version': '3', 'algorithm': 'exact_token_kmp_v1',
                'work_limit': self.maximum, 'work_used': self.work,
                'charges': dict(self.charges), 'memory_limit': self.memory_limit,
                'allocated_units': self.memory}


class Pattern:
    """A normalized pattern and its exact KMP failure table, constructed once."""
    def __init__(self, tokens, budget):
        self.tokens = tokens
        budget.charge('materialization', len(tokens) + 1)
        budget.allocate(len(tokens) + 8)
        self.prefix = [0] * len(tokens)
        matched = 0
        for index in range(1, len(tokens)):
            budget.charge('prefix_steps', 1)
            while True:
                if budget.equal(tokens[index], tokens[matched]):
                    matched += 1
                    break
                if not matched:
                    break
                budget.charge('prefix_steps', 1)
                matched = self.prefix[matched - 1]
            self.prefix[index] = matched

    def positions(self, corpus, budget):
        if not self.tokens or len(self.tokens) > len(corpus):
            return
        matched = 0
        for index, token in enumerate(corpus):
            budget.charge('search_steps', 1)
            while True:
                if budget.equal(token, self.tokens[matched]):
                    matched += 1
                    break
                if not matched:
                    break
                budget.charge('search_steps', 1)
                matched = self.prefix[matched - 1]
            if matched == len(self.tokens):
                budget.charge('matches', 1)
                yield index - matched + 1
                matched = self.prefix[matched - 1]


class Chapter:
    def __init__(self, verses, budget):
        self.tokens, self.coordinates, self.verses = [], [], []
        for verse in verses:
            budget.charge('materialization', 1)
            budget.allocate(8)
            tokens, coordinates = budget.tokens(verse['text'], verse=verse['verse'])
            budget.charge('materialization', 2 * len(tokens))
            budget.allocate(2 * len(tokens))
            self.tokens.extend(tokens)
            self.coordinates.extend(coordinates)
            self.verses.append((verse['verse'], tokens))

    def selected(self, numbers, budget):
        tokens = []
        for number in numbers:
            for verse, words in self.verses:
                budget.charge('selection_steps', 1)
                if number == verse:
                    budget.charge('materialization', len(words))
                    budget.allocate(len(words))
                    tokens.extend(words)
                    break
            else:
                raise ScriptureAttention('missing_verse', 'The selected edition does not contain every cited verse')
        return Pattern(tokens, budget)


class Quote:
    def __init__(self, text, budget):
        budget.charge('normalization', 4 * len(text) + 1)
        budget.allocate(4 * len(text) + 8)
        self.split = ELLIPSIS.split(text)
        self.fragments = []
        edge_tokens = []
        for index, fragment in enumerate(self.split):
            budget.charge('materialization', 1)
            budget.allocate(8)
            if index % 2:
                continue
            tokens, _ = budget.tokens(fragment)
            edge_tokens.append(bool(tokens))
            if tokens:
                budget.charge('normalization', len(fragment))
                self.fragments.append((fragment.strip(), Pattern(tokens, budget)))
        self.leading = not edge_tokens[0]
        self.trailing = not edge_tokens[-1]

    def alignment(self, chapter, budget):
        if not self.fragments:
            return None
        alignments, previous = [], -1
        for source_text, pattern in self.fragments:
            # A second occurrence already proves non-uniqueness, including
            # overlaps; no later occurrence can restore a unique alignment.
            positions = pattern.positions(chapter.tokens, budget)
            start = next(positions, None)
            if start is None or next(positions, None) is not None or start <= previous:
                return None
            words = pattern.tokens
            end = start + len(words)
            if len(words) < 3:
                entire_verse = False
                for _, verse_tokens in chapter.verses:
                    budget.charge('selection_steps', 1)
                    if len(words) == len(verse_tokens) and all(
                            budget.equal(a, b) for a, b in zip(words, verse_tokens)):
                        entire_verse = True
                        break
                if not entire_verse:
                    return None
            previous = end - 1
            budget.charge('materialization', 8)
            budget.allocate(32)
            alignments.append({'source_text': source_text, 'word_start': start, 'word_end': end,
                               'start': chapter.coordinates[start], 'end': chapter.coordinates[end - 1]})
        complete = (len(self.fragments) == 1 and len(self.split) == 1
                    and len(self.fragments[0][1].tokens) == len(chapter.tokens))
        budget.charge('materialization', len(self.split) + 8)
        budget.allocate(len(self.split) + 16)
        return {'kind': 'complete' if complete else 'partial', 'fragments': alignments,
                'ellipsis': [value for i, value in enumerate(self.split) if i % 2],
                'leading_ellipsis': self.leading, 'trailing_ellipsis': self.trailing}


def check_backstop(parsed, references, scopes, english_chapters, reference_address,
                   unclaimed, *, maximum):
    """Run every v2 cited-verse/unclaimed check under the unchanged work cap."""
    budget = Budget(maximum)
    chapters, blocks, checked = {}, {}, set()
    cited_checks = unclaimed_spans = span_chapter_checks = 0
    for identity, verses in english_chapters:
        budget.charge('materialization', len(identity) + 1)
        budget.allocate(len(identity) + 8)
        chapters[identity] = Chapter(verses, budget)
    for ref in references:
        budget.charge('selection_steps', len(ref['block']) + len(ref['identity']) + 1)
        key = (ref['block'], ref['identity'])
        if key in checked:
            continue
        budget.allocate(8)
        checked.add(key)
        try:
            book, number, numbers = reference_address(ref['identity'])
        except ScriptureAttention:
            continue
        cited_checks += 1
        wanted = chapters[f'kjv/{book}/{number}'].selected(numbers, budget)
        if ref['block'] not in blocks:
            blocks[ref['block']] = budget.tokens(parsed.blocks[ref['block']])
        normalized, coordinates = blocks[ref['block']]
        for index in wanted.positions(normalized, budget):
            begin, end = coordinates[index]['start'], coordinates[index + len(wanted.tokens) - 1]['end']
            budget.charge('scope_checks', len(scopes) * (len(ref['block']) + 4))
            if not any(q['block'] == ref['block'] and q['source_start'] <= begin
                       and end <= q['source_end'] for q in scopes):
                raise ScriptureAttention('unmarked_quote_scope',
                    f'{ref["identity"]}: a complete cited verse occurs outside every proved quotation scope')
    for span in unclaimed:
        unclaimed_spans += 1
        quote = Quote(span['text'], budget)
        for identity, chapter in chapters.items():
            budget.charge('selection_steps', 1)
            span_chapter_checks += 1
            if quote.alignment(chapter, budget) is not None:
                raise ScriptureAttention('unassociated_source_quote',
                    f'{span["block"]}: marked source text matches {identity} without its own proved citation: {span["text"][:160]}')
    return {**budget.audit(), 'chapters_prepared': len(chapters), 'blocks_prepared': len(blocks),
            'cited_checks': cited_checks, 'unclaimed_spans': unclaimed_spans,
            'span_chapter_checks': span_chapter_checks}
