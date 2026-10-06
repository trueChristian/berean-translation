"""Frozen native book names and finite aliases, with original text offsets."""
from __future__ import annotations

from .common import ContractError
from .reference_notation import reference_mentions
from .scripture_books import CORE_BOOK_NAMES


_LANGUAGES = {'de': 'deu', 'he': 'heb', 'it': 'ita', 'sv': 'swe',
              'af': 'afr', 'nb': 'nob'}


def quotation_reference_mentions(evidence: dict, text: str) -> list[tuple[int, int, str]]:
    """Recognize only known identities and names attested by frozen lookups.

    ``evidence`` must be the application's verified frozen bundle, never a
    model-provided alias table. Matching in place keeps spans suitable for a
    complete citation-tail proof without rewriting any source/candidate text.
    """
    aliases = {}
    quotes = evidence.get('quotes')
    lookups = evidence.get('lookups')
    if not isinstance(quotes, list) or not isinstance(lookups, dict):
        raise ContractError('Invalid frozen Scripture citation evidence')
    for quote in quotes:
        book = quote.get('book')
        if type(book) is not int or not 1 <= book <= len(CORE_BOOK_NAMES):
            raise ContractError('Invalid frozen Scripture book identity')
        try:
            native = lookups[quote['target_lookup']]['result']['data']['book_name']
        except (KeyError, TypeError) as exc:
            raise ContractError('Missing frozen native Scripture book name') from exc
        if not isinstance(native, str) or not native.strip() or len(native) > 200:
            raise ContractError('Invalid frozen native Scripture book name')
        identity = CORE_BOOK_NAMES[book - 1][0]
        key = native.casefold()
        if key in aliases and aliases[key][1] != identity:
            raise ContractError('Ambiguous frozen native Scripture book name')
        aliases[key] = (native, identity)
    language = _LANGUAGES.get(evidence.get('language_tag'), 'eng')
    return reference_mentions(text, language, native_aliases=tuple(sorted(aliases.values())))
