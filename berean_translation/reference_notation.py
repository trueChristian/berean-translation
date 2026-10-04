"""Conservative, language-qualified Scripture notation equivalence.

This module does not find clocks or compare HTML blocks. Callers must compare
mentions within the same structural block and apply only their source-backed
clock exclusions. It never rewrites the input: returned spans use its original
character offsets. Unknown/malformed citation-shaped numbers are represented by
language-qualified invalid keys, so they cannot silently disappear or qualify as
an equivalent translation.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from .scripture_books import CORE_BOOK_NAMES

# The English identities are also accepted in localized text: language quality
# belongs to semantic review, while this gate protects citation identity.
_ENGLISH_BOOKS = tuple(row[0] for row in CORE_BOOK_NAMES)
_GERMAN_BOOKS = {
    '1. Mose': 'Genesis', '2. Mose': 'Exodus', '3. Mose': 'Leviticus',
    '4. Mose': 'Numbers', '5. Mose': 'Deuteronomy',
    'Deuteronomium': 'Deuteronomy', 'Matthäus': 'Matthew',
    'Psalm': 'Psalm', 'Psalmen': 'Psalm', 'Lukas': 'Luke',
    'Offenbarung': 'Revelation', 'Judas': 'Jude', 'Sprüche': 'Proverbs',
    'Jesaja': 'Isaiah', 'Maleachi': 'Malachi', 'Römer': 'Romans',
    '1. Korinther': '1 Corinthians', '2. Korinther': '2 Corinthians',
    'Jeremia': 'Jeremiah', 'Hosea': 'Hosea', 'Johannes': 'John',
    '1. Johannes': '1 John', '2. Johannes': '2 John', '3. Johannes': '3 John',
    'Markus': 'Mark', 'Hiob': 'Job', '1. Timotheus': '1 Timothy',
    '2. Timotheus': '2 Timothy', '1. Thessalonicher': '1 Thessalonians',
    '2. Thessalonicher': '2 Thessalonians', 'Philipper': 'Philippians',
    'Epheser': 'Ephesians', 'Apg.': 'Acts',
    # Exact abbreviations in the immutable ALL NATURE SINGS correction.
    'Joh': 'John', 'Jes': 'Isaiah', 'Offb': 'Revelation', 'Kol': 'Colossians',
}
_HEBREW_BOOKS = {
    'בראשית': 'Genesis', 'שמות': 'Exodus', 'מתי': 'Matthew',
    'תהילים': 'Psalm', 'לוקס': 'Luke', 'ההתגלות': 'Revelation',
    'יהודה': 'Jude', 'יוחנן': 'John', 'ירמיהו': 'Jeremiah',
    'איוב': 'Job', 'טימותיאוס א׳': '1 Timothy',
    'טימותיאוס ב׳': '2 Timothy', 'תסלוניקים א׳': '1 Thessalonians',
    'תסלוניקים ב׳': '2 Thessalonians', 'פיליפים': 'Philippians',
    'קורינתים א׳': '1 Corinthians', 'קורינתים ב׳': '2 Corinthians',
    # Exact spellings already present in source-paired published translations.
    'א׳ קורינתיים': '1 Corinthians', 'א׳ טימותאוס': '1 Timothy',
    'ב׳ טימותאוס': '2 Timothy', '2 טימותיוס': '2 Timothy',
    '1 קורינתים': '1 Corinthians',
    '2 טימותיאוס': '2 Timothy',
    '1 פטרוס': '1 Peter', '2 פטרוס': '2 Peter',
    'א׳ פטרוס': '1 Peter', 'ב׳ פטרוס': '2 Peter',
    'אפסים': 'Ephesians', 'קולוסים': 'Colossians', 'רומים': 'Romans', 'עברים': 'Hebrews',
    'הראשונה לקורינתים': '1 Corinthians', 'השנייה לקורינתים': '2 Corinthians',
    'הראשונה לפטרוס': '1 Peter', 'אל האפסים': 'Ephesians',
    'הראשונה אל הקורינתים': '1 Corinthians',
    '1 יוחנן': '1 John', '2 יוחנן': '2 John', '3 יוחנן': '3 John',
    'א׳ יוחנן': '1 John', 'ב׳ יוחנן': '2 John', 'ג׳ יוחנן': '3 John',
    # Exact additional forms in the immutable Winter 2021 correction fixtures.
    'האיגרת הראשונה ליוחנן': '1 John', 'יוחנן הראשונה': '1 John',
    'טימותיאוס הראשונה': '1 Timothy', 'טימותיאוס השנייה': '2 Timothy',
    'התגלות': 'Revelation',
    # Exact additional forms in the immutable Summer 2020 corrections.
    'הראשונה ליוחנן': '1 John', 'פטרוס הראשונה': '1 Peter',
    'גלטים': 'Galatians', 'טיטוס': 'Titus',
}
# The complete finite baseline is authoritative; observed, source-paired
# spelling variants above remain exact aliases rather than fuzzy matching.
_GERMAN_BOOKS = {**{row[1]: row[0] for row in CORE_BOOK_NAMES}, **_GERMAN_BOOKS}
_HEBREW_BOOKS = {**{row[2]: row[0] for row in CORE_BOOK_NAMES}, **_HEBREW_BOOKS}
_HEBREW_BOOKS.update({row[2] + '׳': row[0] for row in CORE_BOOK_NAMES
                      if row[2].endswith((' א', ' ב'))})
_DASHES = '-‐‑–—−־'
_QUOTES = str.maketrans({"'": '׳', '‘': '׳', '’': '׳',
                        '"': '״', '“': '״', '”': '״'})
_HEBREW_LETTERS = 'אבגדהוזחטיכלמנסעפצקרשת'
_HEBREW_VALUES = dict(zip(_HEBREW_LETTERS,
    (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30, 40, 50, 60, 70, 80, 90,
     100, 200, 300, 400)))
_DECIMAL_TOKEN = r'[0-9]+[A-Za-z\u05d0-\u05ea]*'
_HEBREW_TOKEN = r'''[\u05d0-\u05ea׳״'"‘’“”]+'''
_DECIMAL_ATOM = re.compile(_DECIMAL_TOKEN)
_HEBREW_ATOM = re.compile(f'(?:{_DECIMAL_TOKEN}|{_HEBREW_TOKEN})')
_BARE_COLON = re.compile(r'(?<![0-9])[0-9]+\s*:')
_HEBREW_COLON = re.compile(rf'(?<![\w׳״])({_HEBREW_TOKEN})\s*:')
# Never match a Gospel/book suffix within an unsupported numbered identity.
_PRECEDING_ORDINAL = re.compile(
    r"(?<![\w:,\-‐‑–—−־])(?:[0-9]+(?:st|nd|rd|th)?\.?|[IVX]+\.?|[אבגדהוזחט](?:[׳'’‘])?)\s*$", re.I)
_PRECEDING_NAMED_ORDINAL = re.compile(
    r'(?<!\w)(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|'
    r'(?:erst|zweit|dritt|viert|fünft|sechst|siebt|acht|neunt|zehnt)e[rsnm]?|'
    r'הראשו(?:ן|נה)|השני(?:ה|יה|ת)?|השלישי(?:ת)?|הרביעי(?:ת)?|'
    r'החמישי(?:ת)?|השישי(?:ת)?|השביעי(?:ת)?|השמיני(?:ת)?|התשיעי(?:ת)?|העשירי(?:ת)?)'
    r'(?:\s+אל)?\s*$', re.I)
_PRECEDING_HEBREW_EPISTLE_TITLE = re.compile(r'(?<!\w)\w*איגרת\s+$')
_BARE_CLOCK_VALUE = re.compile(r'(?:[01]?[0-9]|2[0-3])\s*:\s*[0-5][0-9]')
_AMOUNT_WORDS = (r'(?:Euro|EUR|US-Dollar|Dollar|USD|CHF|Franken|GBP|Pfund|JPY|Yen|'
                 r'Prozent|percent|Kilogramm|Kilometer|Liter|kg|km|cm|mm|g|'
                 r'Millionen?|Milliarden?)')
_UNMAPPED_NUMBERED_CITATION = re.compile(
    r'[1-3]\.?\s*[^\W\d_]+(?:\s+[^\W\d_]+)*\s*'
    rf'(?P<chapter>[0-9]+|{_HEBREW_TOKEN})\s*:')
_AMOUNT_AFTER = re.compile(r'\s*' + _AMOUNT_WORDS + r'(?!\w)', re.I)
_AMOUNT_BEFORE = re.compile(r'(?<!\w)(?:' + _AMOUNT_WORDS +
                            r'|Preis|Betrag|Kosten|Anteil|Zinssatz)\s*$', re.I)



def _decimal_digits(text: str) -> str:
    # Every replacement is exactly one code point, retaining span offsets.
    return ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)


def _hebrew_spelling(number: int) -> str:
    letters = ''
    for value, letter in ((400, 'ת'), (300, 'ש'), (200, 'ר'), (100, 'ק'),
                          (90, 'צ'), (80, 'פ'), (70, 'ע'), (60, 'ס'),
                          (50, 'נ'), (40, 'מ'), (30, 'ל'), (20, 'כ')):
        while number >= value:
            letters += letter
            number -= value
    # The conventional exceptions apply after the hundreds as well.
    if number in (15, 16):
        letters += 'טו' if number == 15 else 'טז'
    else:
        if number >= 10:
            letters += 'י'
            number -= 10
        if number:
            letters += _HEBREW_LETTERS[number - 1]
    return letters + '׳' if len(letters) == 1 else letters[:-1] + '״' + letters[-1]


def _hebrew_number(token: str) -> int | None:
    token = token.translate(_QUOTES)
    letters = token.replace('׳', '').replace('״', '')
    if not letters or len(letters) > 6 or any(c not in _HEBREW_VALUES for c in letters):
        return None
    value = sum(_HEBREW_VALUES[c] for c in letters)
    if 1 <= value <= 999 and token == _hebrew_spelling(value):
        return value
    return None


def _number(token: str, language: str, *, suffix: bool = False) -> tuple[int, str] | None:
    decimal = re.fullmatch(r'([0-9]+)([aAbBאב]?)', token)
    if decimal:
        digits, part = decimal.groups()
        # Bound parsing work; no range is ever expanded into its verse set.
        if len(digits) > 3 or not 1 <= int(digits) <= 999 or (part and not suffix):
            return None
        if part in 'אב' and part and language != 'heb':
            return None
        return int(digits), {'א': 'a', 'ב': 'b'}.get(part, part.lower())
    if language != 'heb':
        return None
    value = _hebrew_number(token)
    if value is not None:
        return value, ''
    if suffix and token.endswith(('א', 'ב')):
        value = _hebrew_number(token[:-1])
        if value is not None:
            return value, 'a' if token[-1] == 'א' else 'b'
    return None


def _alias_pattern(alias: str) -> str:
    if alias == 'Is.':
        return r'(?-i:Is\.?)'  # The English verb "is" is never a book alias.
    return re.escape(alias).replace(r'\.', r'\.?').replace(r'\ ', r'\s*').replace('׳', "[׳'’‘]")


@lru_cache(maxsize=12)
def _book_pattern(language: str, target_language: str | None = None) -> tuple[re.Pattern, dict[str, str]]:
    paired = target_language if language == 'eng' else language
    supported = ({'deu': _GERMAN_BOOKS, 'heb': _HEBREW_BOOKS}.get(paired))
    identities_supported = set(supported.values()) if supported is not None else set(_ENGLISH_BOOKS)
    aliases = {book: book for book in _ENGLISH_BOOKS if book in identities_supported}
    for book, _, _, osis in CORE_BOOK_NAMES:
        if book not in identities_supported:
            continue
        aliases[(osis[0] + ' ' + osis[1:] if osis[0] in '123' else osis) + '.'] = book
        if book[0] in '123':
            roman = {'1': 'I', '2': 'II', '3': 'III'}[book[0]]
            aliases[roman + '. ' + book[2:]] = book
            aliases[roman + '. ' + osis[1:] + '.'] = book
    aliases.update({alias: identity for alias, identity in
                    {'Psalms': 'Psalm', 'Song of Songs': 'Song of Solomon',
                     'Ps.': 'Psalm', 'Rev.': 'Revelation', 'Gen.': 'Genesis',
                     'Jer.': 'Jeremiah', 'Ecc.': 'Ecclesiastes', 'Is.': 'Isaiah',
                     'Hebrew': 'Hebrews'}.items()
                    if identity in identities_supported})
    if language == 'deu':
        aliases.update(_GERMAN_BOOKS)
    elif language == 'heb':
        aliases.update(_HEBREW_BOOKS)
    # Longest first avoids treating "1 John" as "John" and ordinal suffixes
    # as chapters. Recognize only the observed attached conjunction vav and
    # prepositions bet ("in") and mem ("from"), optionally combined with vav,
    # not arbitrary word prefixes.
    groups, identities = [], {}
    for index, alias in enumerate(sorted(aliases, key=len, reverse=True)):
        group = f'b{index}'
        groups.append(f'(?P<{group}>{_alias_pattern(alias)})')
        identities[group] = aliases[alias]
    prefix = '(?:ו?[במ]|ו)?' if language == 'heb' else ''
    return re.compile(r'(?<!\w)' + prefix + '(?:' + '|'.join(groups) +
                      r')(?![^\W\d_])', re.I), identities


def _space(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _atom(text: str, position: int, language: str) -> re.Match | None:
    return (_HEBREW_ATOM if language == 'heb' else _DECIMAL_ATOM).match(text, position)


def _numeric_looking(token: str) -> bool:
    return token[:1].isdigit() or any(c in token for c in '׳״\'"‘’“”')


def _unsupported_numeric_tail(text: str, end: int, language: str, *, qualified=False) -> int | None:
    """Hold unknown arithmetic/range/list connectors with numeric endpoints.

    Unicode categories catch math symbols and dash variants without an endless
    per-glyph allowlist. Compatibility normalization is used only to classify
    punctuation such as fullwidth slash; text and offsets are never rewritten.
    """
    position = _space(text, end)
    start = position
    while position < len(text):
        character = text[position]
        normalized = unicodedata.normalize('NFKC', character)
        if (unicodedata.category(character) in ('Sm', 'Pd')
                or normalized in ('/', '\\', ';', ':', '&', '·', '|', '^', '~', '.', '*')):
            position += 1
        else:
            break
    if position == start:
        return None
    connectors = text[start:position]
    if connectors == ':' and (_verse_annotation(text, start, language)
            or qualified and _quoted_prose_after_colon(text, start)):
        return None
    endpoint_start = _space(text, position)
    # A full stop plus whitespace belongs to ordinary sentence punctuation.
    if unicodedata.normalize('NFKC', connectors) == '.' and (
            start != end or endpoint_start != position):
        return None
    endpoint = _atom(text, endpoint_start, language)
    if endpoint is None or not _numeric_looking(endpoint.group()):
        return None
    if unicodedata.normalize('NFKC', connectors) == ';':
        after_endpoint = _space(text, endpoint.end())
        if text[after_endpoint:after_endpoint + 1] == ':':
            return None  # The next bare chapter:verse is independently protected.
        if _book_pattern(language)[0].match(text, endpoint_start):
            return None  # The next explicit numbered-book citation is independent.
        unknown_book = _UNMAPPED_NUMBERED_CITATION.match(text, endpoint_start)
        if unknown_book and _numeric_looking(unknown_book['chapter']):
            # Preserve legacy numeric protection for unmapped book names too.
            return None
    return endpoint.end()


def _verse_expression(text: str, start: int, language: str, *, qualified=False) -> tuple[int, str | None] | None:
    """Parse ordered verse pieces, coalescing only non-overlapping adjacency."""
    first = _atom(text, start, language)
    if first is None:
        return None
    intervals = []
    invalid = False
    current = first
    end = start
    while current:
        left = _number(current.group(), language, suffix=True)
        right = left
        end = current.end()
        next_pos = _space(text, end)
        if next_pos < len(text) and text[next_pos] in _DASHES:
            after_dash = _space(text, next_pos + 1)
            endpoint = _atom(text, after_dash, language)
            if endpoint and _numeric_looking(endpoint.group()):
                right = _number(endpoint.group(), language, suffix=True)
                end = endpoint.end()
                next_pos = _space(text, end)
            elif (after_dash == len(text)
                  or text[after_dash:after_dash + 1] in (')', ']', ';', ',', *_DASHES)
                  or next_pos == end):
                # An explicitly dangling range must not become its first verse.
                invalid = True
                end = endpoint.end() if endpoint else next_pos + 1
                next_pos = _space(text, end)
        if left is None or right is None or right < left:
            invalid = True
        else:
            intervals.append((left, right))
        # A colon followed by a number is not a supported verse endpoint.
        # Preserve the whole malformed/cross-chapter expression as invalid.
        if next_pos < len(text) and text[next_pos] == ':':
            after_colon = _space(text, next_pos + 1)
            following = _atom(text, after_colon, language)
            if (following and _numeric_looking(following.group())
                    and not _verse_annotation(text, next_pos, language)
                    and not (qualified and _quoted_prose_after_colon(text, next_pos))):
                invalid = True
                end = following.end()
                next_pos = _space(text, end)
        if text[end:end + 1] == '.' and text[end + 1:end + 2].isdigit():
            # Unsupported dotted/decimal tails cannot vanish after a prefix.
            tail = re.match(r'(?:\.[0-9]+)+', text[end:])
            end += tail.end()
            invalid = True
            next_pos = _space(text, end)
        if (qualified and text[next_pos:next_pos + 1] == ':'
                and text[next_pos + 1:].lstrip()[:1] in _QUOTE_PAIRS
                and not _quoted_prose_after_colon(text, next_pos)):
            invalid = True
            end = next_pos + 1
            next_pos = _space(text, end)
        tail_end = _unsupported_numeric_tail(text, end, language, qualified=qualified)
        if tail_end is not None:
            # Do not silently truncate unsupported /19, +19, etc. to verse 18.
            end = tail_end
            invalid = True
            next_pos = _space(text, end)
        if next_pos >= len(text) or text[next_pos] != ',':
            break
        following = _atom(text, _space(text, next_pos + 1), language)
        if not following or not _numeric_looking(following.group()):
            if text[_space(text, next_pos + 1):].startswith((',', ':')):
                invalid = True
                end = _space(text, next_pos + 1) + 1
            break
        # "3:16, 4:2" comprises separate chapter references, not verses 16,4.
        after_following = _space(text, following.end())
        if (text[after_following:after_following + 1] == ':'
                and not (qualified and _quoted_prose_after_colon(text, after_following))):
            next_verse = _atom(text, _space(text, after_following + 1), language)
            if next_verse and _numeric_looking(next_verse.group()):
                break  # A separately protected chapter:verse follows.
            # A malformed/ambiguous final list member must not disappear just
            # because it cannot be recognized as an independent reference.
            invalid = True
            end = after_following + 1
            break
        current = following
    if invalid:
        return end, None
    merged = []
    for left, right in intervals:
        if (merged and not merged[-1][1][1] and not left[1] and not right[1]
                and left[0] == merged[-1][1][0] + 1):
            merged[-1] = (merged[-1][0], right)
        else:
            merged.append((left, right))
    def display(value):
        return f'{value[0]}{value[1]}'
    return end, ','.join(display(left) if left == right else f'{display(left)}-{display(right)}'
                         for left, right in merged)


def _plain_prose(tail: str) -> bool:
    """Require two plain words, excluding numeric and ambiguous numeral tails."""
    if tail[:1] in ('"', '“', '„', '«', '‘', '「', '『', '״'):
        tail = tail[1:].lstrip()
    words = re.match(r'([^\W\d_]{2,})(?:\s+|[,.!?…]+\s+)'
                     r'([^\W\d_]{2,})(?=\W|$)', tail)
    if not words:
        return False
    first = words[1]
    if re.fullmatch('[IVXLCDM]+', first, re.I):
        return False  # Unsupported Roman-numeral verses are not prose.
    if all(c in _HEBREW_VALUES for c in first):
        values = [_HEBREW_VALUES[c] for c in first]
        if all(left >= right for left, right in zip(values, values[1:])):
            # Descending additive letters might be an unmarked numeral,
            # including malformed or oversized values. Do not reinterpret
            # them as prose or construct an unbounded numeric spelling.
            return False
    return True


def _prose_after_colon(text: str, colon: int) -> bool:
    return (text[colon:colon + 1] == ':'
            and text[colon + 1:colon + 2].isspace()
            and _plain_prose(text[colon + 1:].lstrip()))


_QUOTE_PAIRS = {'"': '"', '“': '”', '„': '“', '«': '»', '»': '«',
                '‘': '’', '「': '」', '『': '』', '״': '״'}


def _quoted_prose(tail: str) -> bool:
    """A balanced quotation with plain words, never a quoted numeric endpoint."""
    closer = _QUOTE_PAIRS.get(tail[:1])
    if closer is None:
        return False
    end = tail.find(closer, 1)
    if end < 0:
        return False
    words = re.match(r'([^\W\d_]+)(?:\s+|[,.!?…]+\s+)([^\W\d_]+)(?=\W|$)', tail[1:end])
    if not words:
        return False
    first = words[1]
    if re.fullmatch('[IVXLCDM]+', first, re.I) and first not in ('I',):
        return False
    if all(character in _HEBREW_VALUES for character in first):
        value = sum(_HEBREW_VALUES[character] for character in first)
        if value > 999 or _hebrew_spelling(value).replace('׳', '').replace('״', '') == first:
            return False  # Canonical unmarked Hebrew numerals remain ambiguous.
    return True


def _quoted_prose_after_colon(text: str, colon: int) -> bool:
    return (text[colon:colon + 1] == ':' and text[colon + 1:colon + 2].isspace()
            and _quoted_prose(text[colon + 1:].lstrip()))


def _german_prose_after_comma(text: str, comma: int) -> bool:
    """A chapter-only mention can be followed by a prose comma.

    Require whitespace and plain words, or a plain word followed by a balanced
    prose quotation (the observed ``Römer 13, den „höheren Gewalten ...“``).
    Numeric, Roman-numeral and dangling tails still go through citation parsing.
    """
    if text[comma:comma + 1] != ',' or not text[comma + 1:comma + 2].isspace():
        return False
    tail = text[comma + 1:].lstrip()
    if tail[:1] in _QUOTE_PAIRS:
        return _quoted_prose(tail)
    if _plain_prose(tail):
        return True
    first = re.match(r'([^\W\d_]{2,})\s+', tail)
    return bool(first and not re.fullmatch('[IVXLCDM]+', first[1], re.I)
                and not all(character in _HEBREW_VALUES for character in first[1])
                and _quoted_prose(tail[first.end():]))


def _unmarked_hebrew_citation(text: str, book_start: int, book_end: int,
                              book: str) -> tuple[int, str] | None:
    """Recognize the exact delimited form seen in ``— משלי טז:יח.``.

    A known book, preceding citation dash, separating whitespace, adjacent
    chapter:verse tokens and sentence-ending period are all required. Only
    canonical multi-letter numerals qualify. This does not enable unmarked
    words in the general number parser, bare citations, ranges or verse lists.
    Same-block source comparison still protects the decoded identity and count.
    """
    if text[:book_start].rstrip()[-1:] not in tuple(_DASHES):
        return None
    start = _space(text, book_end)
    if start == book_end:
        return None
    pair = re.match(r'([\u05d0-\u05ea]{2,}):([\u05d0-\u05ea]{2,})', text[start:])
    if pair is None:
        return None
    # Reinsert the conventional mark only for checking canonical spelling;
    # never rewrite the candidate or accept merely additive letter values.
    chapter, verse = (_hebrew_number(token[:-1] + '״' + token[-1])
                      for token in pair.groups())
    if chapter is None and verse is None:
        return None  # Ordinary Hebrew words are not numeric evidence.
    end = start + pair.end()
    if (chapter is None or verse is None or text[end:end + 1] != '.'
            or text[end + 1:end + 2] and not text[end + 1].isspace()):
        return end, f'!invalid[heb] {book} {text[start:end]}'
    return end, f'{book} {chapter}:{verse}'


def _verse_annotation(text: str, colon: int, language: str) -> tuple[int, int] | None:
    """One explicit verse label after a complete citation, never recursion.

    Return the prefix end and verse number. The caller separately protects that
    number and must qualify the label with its preceding known-book citation.
    """
    if text[colon:colon + 1] != ':' or not text[colon + 1:colon + 2].isspace():
        return None
    start = _space(text, colon + 1)
    if language == 'heb':
        label = re.match(r"פס[׳'’‘]\s+([1-9][0-9]{0,2})(:)", text[start:])
        if label and _prose_after_colon(text, start + label.start(2)):
            return _space(text, start + label.end()), int(label[1])
    elif language == 'eng':
        label = re.match(r'V\.\s*([1-9][0-9]{0,2})\s+', text[start:])
        if label and _plain_prose(text[start + label.end():]):
            return start + label.end(), int(label[1])
    return None


def _parse_at(text: str, start: int, language: str, book: str | None = None,
              *, comma: bool = False, chapter_introductions: bool = False) -> tuple[int, str | None] | None:
    chapter = _atom(text, start, language)
    if chapter is None:
        return None
    after = _space(text, chapter.end())
    chapter_value = _number(chapter.group(), language)
    repeated = None
    redundant = False
    ambiguous_book = False
    if after < len(text) and (text[after] == ':' or (comma and text[after] == ',')):
        verse_start = _space(text, after + 1)
        if book and chapter_value is not None and (_prose_after_colon(text, after)
                or _quoted_prose_after_colon(text, after)
                or chapter_introductions and language == 'deu'
                and _german_prose_after_comma(text, after)):
            # Record only the introductory prefix as consumed. In particular,
            # a later real citation in the quoted prose must still be parsed.
            return verse_start, None
    elif (language == 'heb' and book and after > chapter.end()
          and any(c in chapter.group() for c in '׳״\'"‘’“”')):
        # A small John numeral without a colon may be an epistle ordinal.
        ambiguous_book = book == 'John' and chapter_value in ((1, ''), (2, ''), (3, ''))
        # This narrowly qualified colonless form needs an explicit book,
        # a quoted Hebrew chapter, and decimal verse(s).
        decimal = _DECIMAL_ATOM.match(text, after)
        if not decimal:
            return None
        after_decimal = _space(text, decimal.end())
        if text[after_decimal:after_decimal + 1] == ':':
            # A redundant decimal chapter is allowed only when identical.
            redundant = True
            repeated = _number(decimal.group(), language)
            verse_start = _space(text, after_decimal + 1)
        else:
            verse_start = after
    else:
        return None
    first_verse = _atom(text, verse_start, language)
    if (language == 'heb' and not _numeric_looking(chapter.group())
            and first_verse and not _numeric_looking(first_verse.group())):
        # Book names also occur in prose ("John said: yes"). Unquoted
        # Hebrew words on both sides of a colon are not numeric evidence.
        # A source citation replaced with such ambiguous text remains missing.
        return None
    verses = _verse_expression(text, verse_start, language, qualified=book is not None)
    if verses is None:
        # Retain an obvious, qualified dangling citation as an invalid key.
        if book and _numeric_looking(chapter.group()):
            return verse_start, f'!invalid[{language}] {book} {text[start:verse_start]}'
        return None
    end, expression = verses
    prefix = f'{book} ' if book else ''
    # Unqualified clock-shaped material remains exactly protected, including
    # zero minutes. This is not a clock exemption or permission to change it.
    raw_value = text[start:end]
    if book is None and _BARE_CLOCK_VALUE.fullmatch(raw_value):
        return end, re.sub(r'\s+', '', raw_value)
    if (chapter_value is None or expression is None or ambiguous_book
            or (redundant and repeated != chapter_value)):
        return end, f'!invalid[{language}] {prefix}{text[start:end]}'
    if book and _quoted_prose_after_colon(text, _space(text, end)):
        # Consume only the quote-introduction delimiter, so the last list
        # member cannot also become a phantom bare chapter:verse reference.
        end = _space(text, _space(text, end) + 1)
    return end, f'{prefix}{chapter_value[0]}:{expression}'


def _preceding_ordinal_start(text: str, position: int) -> int | None:
    """Keep the full unsupported ordinal prefix, including stacked ordinals."""
    start = position
    while True:
        ordinals = [match for pattern in (_PRECEDING_ORDINAL, _PRECEDING_NAMED_ORDINAL)
                    if (match := pattern.search(text[:start]))]
        if not ordinals:
            return start if start != position else None
        start = min(match.start() for match in ordinals)


def _qualified_books(text: str, language: str, target_language: str | None = None,
                     *, retain_unsupported_ordinals: bool = False):
    book_pattern, identities = _book_pattern(language, target_language)
    for match in book_pattern.finditer(text):
        ordinal_start = _preceding_ordinal_start(text, match.start())
        if ordinal_start is not None and not retain_unsupported_ordinals:
            continue
        book = identities[match.lastgroup]
        if (language == 'heb' and book == '1 John'
                and _PRECEDING_HEBREW_EPISTLE_TITLE.search(text[:match.start()])):
            # A newly supported shorter name cannot salvage a larger title
            # whose unsupported attached prefix failed the normal boundary.
            continue
        yield match, book, ordinal_start


def chapter_reference_mentions(text: str, language: str, references,
                               *, target_language: str | None = None) -> list[tuple[int, int, str]]:
    """Find chapter-only identities for an explicitly source-qualified block.

    Callers must not add these to ordinary reference counters unless the English
    block has the authenticated ``in [book] [chapter] to [prose]`` introduction.
    Within that block, protect every complete chapter-only mention on both sides
    so another mention cannot replace or conceal an omitted/duplicated chapter.
    """
    normalized = _decimal_digits(text)
    result = []
    for match, book, ordinal_start in _qualified_books(normalized, language, target_language,
                                                       retain_unsupported_ordinals=language == 'deu'):
        start = _space(normalized, match.end())
        chapter = _DECIMAL_ATOM.match(normalized, start)
        if chapter is None:
            continue
        if any(left <= start < right for left, right, _ in references):
            continue
        end = chapter.end()
        if ordinal_start is not None:
            result.append((ordinal_start, end,
                           f'!invalid[deu] chapter-only ordinal {normalized[ordinal_start:end]}'))
            continue
        value = _number(chapter.group(), language)
        if value is None:
            continue
        after = _space(normalized, end)
        tail = normalized[after:]
        if (not tail or tail[:1] in (';', ')', ']', '!', '?')
                or tail.startswith('.') and (len(tail) == 1 or tail[1].isspace())
                or after > end and _plain_prose(tail)
                or tail.startswith(',') and (
                    _german_prose_after_comma(normalized, after) if language == 'deu'
                    else normalized[after + 1:after + 2].isspace()
                    and _plain_prose(normalized[after + 1:].lstrip()))):
            result.append((match.start(), end, f'chapter-only {book} {value[0]}'))
        elif language == 'deu':
            result.append((match.start(), end, f'!invalid[deu] chapter-only {book} {chapter.group()}'))
    return result


def has_source_chapter_introduction(text: str, chapters) -> bool:
    """Only the source's narrow introduction enables chapter-only comparison."""
    return any(re.search(r'(?<!\w)in\s+$', text[:start], re.I)
               and re.match(r'\s+to\s+', text[end:], re.I)
               and _plain_prose(text[end:].lstrip()) for start, end, _ in chapters)


def reference_mentions(text: str, language: str, *, target_language: str | None = None,
                       chapter_introductions: bool = False) -> list[tuple[int, int, str]]:
    """Return conservative (start, end, key) citation mentions.

    ``language`` is a trusted configured three-letter language code. Pass the
    corresponding ``target_language`` for English source text to qualify only
    book identities supported by that localized alias table. German
    commas are recognized only after a mapped book or inside a complete
    citation-shaped pair of parentheses. Hebrew letter numerals are decoded
    only in citation syntax, never by replacing words in prose. Bare decimal
    colon references remain protected even without a known book. Occurrences
    remain separate; callers must not collapse this list into a set.
    """
    normalized = _decimal_digits(text)
    mentions = []
    introductions = []
    for match, book, ordinal_start in _qualified_books(normalized, language, target_language,
                                                       retain_unsupported_ordinals=language == 'heb'):
        if ordinal_start is not None:
            parsed = _unmarked_hebrew_citation(normalized, ordinal_start, match.end(), book)
            if parsed is not None:
                end, _ = parsed
                mentions.append((ordinal_start, end,
                                 f'!invalid[heb] ordinal {normalized[ordinal_start:end]}'))
            continue  # Other historical unsupported-ordinal syntax is unchanged.
        parsed = (_unmarked_hebrew_citation(normalized, match.start(), match.end(), book)
                  if language == 'heb' else None)
        if parsed is None:
            parsed = _parse_at(normalized, _space(normalized, match.end()), language,
                               book, comma=language == 'deu',
                               chapter_introductions=chapter_introductions)
        if parsed:
            end, key = parsed
            if match.group(match.lastgroup).rstrip('.') == 'Is':
                # This short source abbreviation is ambiguous with ordinary
                # prose. Only a complete parenthesized citation qualifies it.
                if (normalized[:match.start()].rstrip()[-1:] != '('
                        or normalized[_space(normalized, end):_space(normalized, end) + 1] != ')'):
                    continue
            if key is None:
                introductions.append((match.start(), end))
            else:
                mentions.append((match.start(), end, key))
                if not key.startswith('!invalid') and (language == 'heb' or
                        (language == 'eng' and target_language == 'heb')):
                    annotation = _verse_annotation(normalized, _space(normalized, end), language)
                    if annotation:
                        annotation_end, verse = annotation
                        mentions.append((end, annotation_end, f'{key} verse-label {verse}'))
                        introductions.append((match.start(), annotation_end))

    def covered(start, end):
        return (any(left <= start and end <= right for left, right, _ in mentions)
                or any(left <= start and end <= right for left, right in introductions))

    if language in ('deu', 'heb') or (language == 'eng' and target_language in ('deu', 'heb')):
        # Source-backed standalone verse labels introduce a quotation, not an
        # invented chapter. Protect their numbers separately on both sides.
        label = (r"(?<!\w)(?:ו?[במ]|ו)?(?:פסוק|פס[׳'’‘])(?![^\W\d_])\s*" if language == 'heb'
                 else r'(?<!\w)(?:Vers|V\.)(?![^\W\d_])\s*' if language == 'deu'
                 else r'(?<!\w)(?:verse|v\.)(?![^\W\d_])\s*')
        # Retain numeric-shaped malformed outer labels too. A digits-only
        # lexer could ignore 22a/22.5/-1/21,22 and expose only an inner label.
        pattern = re.compile(label + r'([^\s,:]+(?:[,:][^\s,:]+)*'
                             r'(?:\s+(?:[-+−*/×÷=|^&.,:]+\s*)?[0-9][^\s,:]*)*)\s*([,:])', re.I)
        for match in pattern.finditer(normalized):
            delimiter = match.start(2)
            tail = normalized[delimiter + 1:].lstrip()
            if covered(match.start(), match.end()):
                continue
            value = match[1]
            valid_number = re.fullmatch(r'[1-9][0-9]{0,2}', value) is not None
            numeric_shaped = (any(character.isdecimal() for character in value)
                              or _numeric_looking(value)
                              or re.fullmatch(r'[IVXLCDM]+', value, re.I))
            nested_start = _space(normalized, delimiter + 1)
            while normalized[nested_start:nested_start + 1] in (':', ','):
                nested_start = _space(normalized, nested_start + 1)
            nested = pattern.match(normalized, nested_start)
            if not numeric_shaped and not nested:
                continue  # Ordinary prose such as "this verse says:".
            if (not valid_number or nested or normalized[delimiter + 1:].lstrip().startswith((':', ','))
                    or tail[:1] in _QUOTE_PAIRS and not _quoted_prose(tail)):
                mentions.append((match.start(), match.end(),
                                 f'!invalid[{language}] verse-label {value}'))
                continue
            if (not covered(match.start(), match.end())
                    and normalized[delimiter + 1:delimiter + 2].isspace()
                    and _quoted_prose(tail)):
                mentions.append((match.start(), match.end(), f'verse-label {int(match[1])}'))
    if language == 'deu':
        # Only full parentheses qualify a bookless comma expression. Source
        # comparison in the SAME block is still required for equivalence.
        for parenthesis in re.finditer(r'\(\s*([0-9][^()]*)\)', normalized):
            before, after = normalized[:parenthesis.start()], normalized[parenthesis.end():]
            previous, following = before.rstrip()[-1:], after.lstrip()[:1]
            symbols = (previous, following)
            if (any(c and (unicodedata.category(c) == 'Sc' or c in '%‰‱') for c in symbols)
                    or _AMOUNT_BEFORE.search(before) or _AMOUNT_AFTER.match(after)):
                continue
            start = parenthesis.start(1)
            parsed = _parse_at(normalized, start, language, comma=True)
            if parsed:
                end, key = parsed
                if key is not None and _space(normalized, end) == parenthesis.end() - 1 and not covered(start, end):
                    mentions.append((start, end, key))
    # Unknown or bare decimal colon forms remain protected, as before.
    for match in _BARE_COLON.finditer(normalized):
        if covered(match.start(), match.end()):
            continue
        parsed = _parse_at(normalized, match.start(), language)
        if parsed:
            end, key = parsed
            if key is not None:
                mentions.append((match.start(), end, key))
    if language == 'heb':
        for match in _HEBREW_COLON.finditer(normalized):
            if covered(match.start(), match.end()) or not _numeric_looking(match[1]):
                continue
            parsed = _parse_at(normalized, match.start(), language)
            if parsed:
                end, key = parsed
                if key is not None:
                    mentions.append((match.start(), end, key))
    return sorted(mentions)
