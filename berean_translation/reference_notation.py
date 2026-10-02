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

# The English identities are also accepted in localized text: language quality
# belongs to semantic review, while this gate protects citation identity.
_ENGLISH_BOOKS = (
    'Genesis', 'Exodus', 'Leviticus', 'Numbers', 'Deuteronomy', 'Joshua',
    'Judges', 'Ruth', '1 Samuel', '2 Samuel', '1 Kings', '2 Kings',
    '1 Chronicles', '2 Chronicles', 'Ezra', 'Nehemiah', 'Esther', 'Job',
    'Psalm', 'Proverbs', 'Ecclesiastes', 'Song of Solomon', 'Isaiah',
    'Jeremiah', 'Lamentations', 'Ezekiel', 'Daniel', 'Hosea', 'Joel',
    'Amos', 'Obadiah', 'Jonah', 'Micah', 'Nahum', 'Habakkuk', 'Zephaniah',
    'Haggai', 'Zechariah', 'Malachi', 'Matthew', 'Mark', 'Luke', 'John',
    'Acts', 'Romans', '1 Corinthians', '2 Corinthians', 'Galatians',
    'Ephesians', 'Philippians', 'Colossians', '1 Thessalonians',
    '2 Thessalonians', '1 Timothy', '2 Timothy', 'Titus', 'Philemon',
    'Hebrews', 'James', '1 Peter', '2 Peter', '1 John', '2 John',
    '3 John', 'Jude', 'Revelation',
)
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
    '1 יוחנן': '1 John', '2 יוחנן': '2 John', '3 יוחנן': '3 John',
    'א׳ יוחנן': '1 John', 'ב׳ יוחנן': '2 John', 'ג׳ יוחנן': '3 John',
}
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
    r"(?<![\w:,\-‐‑–—−־])(?:[0-9]+\.?|[IVX]+\.?|[אבגדהוזחט](?:[׳'’‘])?)\s*$", re.I)
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
    return re.escape(alias).replace(r'\.', r'\.?').replace(r'\ ', r'\s*').replace('׳', "[׳'’‘]")


@lru_cache(maxsize=12)
def _book_pattern(language: str, target_language: str | None = None) -> tuple[re.Pattern, dict[str, str]]:
    paired = target_language if language == 'eng' else language
    supported = ({'deu': _GERMAN_BOOKS, 'heb': _HEBREW_BOOKS}.get(paired))
    identities_supported = set(supported.values()) if supported is not None else set(_ENGLISH_BOOKS)
    aliases = {book: book for book in _ENGLISH_BOOKS if book in identities_supported}
    aliases.update({alias: identity for alias, identity in
                    {'Psalms': 'Psalm', 'Song of Songs': 'Song of Solomon'}.items()
                    if identity in identities_supported})
    if language == 'deu':
        aliases.update(_GERMAN_BOOKS)
    elif language == 'heb':
        aliases.update(_HEBREW_BOOKS)
    # Longest first avoids treating "1 John" as "John" and ordinal suffixes
    # as chapters. Only the Hebrew conjunction vav can be attached to a book.
    groups, identities = [], {}
    for index, alias in enumerate(sorted(aliases, key=len, reverse=True)):
        group = f'b{index}'
        groups.append(f'(?P<{group}>{_alias_pattern(alias)})')
        identities[group] = aliases[alias]
    prefix = '(?:ו)?' if language == 'heb' else ''
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


def _unsupported_numeric_tail(text: str, end: int, language: str) -> int | None:
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


def _verse_expression(text: str, start: int, language: str) -> tuple[int, str | None] | None:
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
            if following and _numeric_looking(following.group()):
                invalid = True
                end = following.end()
                next_pos = _space(text, end)
        if text[end:end + 1] == '.' and text[end + 1:end + 2].isdigit():
            # Unsupported dotted/decimal tails cannot vanish after a prefix.
            tail = re.match(r'(?:\.[0-9]+)+', text[end:])
            end += tail.end()
            invalid = True
            next_pos = _space(text, end)
        tail_end = _unsupported_numeric_tail(text, end, language)
        if tail_end is not None:
            # Do not silently truncate unsupported /19, +19, etc. to verse 18.
            end = tail_end
            invalid = True
            next_pos = _space(text, end)
        if next_pos >= len(text) or text[next_pos] != ',':
            break
        following = _atom(text, _space(text, next_pos + 1), language)
        if not following or not _numeric_looking(following.group()):
            if text[_space(text, next_pos + 1):].startswith(','):
                invalid = True
                end = _space(text, next_pos + 1) + 1
            break
        # "3:16, 4:2" comprises separate chapter references, not verses 16,4.
        after_following = _space(text, following.end())
        if text[after_following:after_following + 1] == ':':
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


def _parse_at(text: str, start: int, language: str, book: str | None = None,
              *, comma: bool = False) -> tuple[int, str] | None:
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
    verses = _verse_expression(text, verse_start, language)
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
    return end, f'{prefix}{chapter_value[0]}:{expression}'


def reference_mentions(text: str, language: str, *, target_language: str | None = None) -> list[tuple[int, int, str]]:
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
    book_pattern, identities = _book_pattern(language, target_language)
    for match in book_pattern.finditer(normalized):
        if _PRECEDING_ORDINAL.search(normalized[:match.start()]):
            continue
        parsed = _parse_at(normalized, _space(normalized, match.end()), language,
                           identities[match.lastgroup], comma=language == 'deu')
        if parsed:
            end, key = parsed
            mentions.append((match.start(), end, key))

    def covered(start, end):
        return any(left <= start and end <= right for left, right, _ in mentions)

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
                if _space(normalized, end) == parenthesis.end() - 1 and not covered(start, end):
                    mentions.append((start, end, key))
    # Unknown or bare decimal colon forms remain protected, as before.
    for match in _BARE_COLON.finditer(normalized):
        if covered(match.start(), match.end()):
            continue
        parsed = _parse_at(normalized, match.start(), language)
        if parsed:
            end, key = parsed
            mentions.append((match.start(), end, key))
    if language == 'heb':
        for match in _HEBREW_COLON.finditer(normalized):
            if covered(match.start(), match.end()) or not _numeric_looking(match[1]):
                continue
            parsed = _parse_at(normalized, match.start(), language)
            if parsed:
                end, key = parsed
                mentions.append((match.start(), end, key))
    return sorted(mentions)
