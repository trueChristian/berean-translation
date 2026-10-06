# Exact Scripture book identities and quoted reference lists

The deterministic gate compares citations inside the same HTML block. It preserves
book identity, chapter, ordered verse pieces, duplicate occurrences and subverse
suffixes. It does not translate Scripture quotations or substitute another Bible
edition. Independent semantic review remains mandatory.

## Finite alias data

`berean_translation/scripture_books.py` contains 66 configured core book identities
with one exact English, German and Hebrew name and an OSIS identifier. All 198
literals, 132 English OSIS forms (with/without a terminal dot), and 62 finite Roman
numbered-book forms were independently checked as complete citations with the
expected book identity against the website's already pinned
[bible-passage-reference-parser 4.0.0](https://www.npmjs.com/package/bible-passage-reference-parser/v/4.0.0).
The production Python parser imports only the finite facts, not that library's
regular expressions, spelling expansion or runtime dependency.

[Recorded package integrity and language-module hashes](third-party/scripture-alias-provenance.json)
identify the exact evidence. The upstream source is
[OpenBible's Bible Passage Reference Parser](https://github.com/openbibleinfo/Bible-Passage-Reference-Parser).
Its [MIT notice](third-party/OpenBible-MIT-LICENSE.txt) is retained. To reproduce
the evidence independently, use that exact package/version, parse each literal
followed by `1:1`, and require the expected OSIS book and complete input span;
merely finding any reference somewhere in the string is insufficient.

GetBible inventories establish configured book IDs and edition availability. They
do not supply all localized aliases: its German inventory uses English names, and
its modern-Hebrew inventory uses English New Testament names. Do not attribute
the localized-name table or every spelling variant to that API.

Existing exact source-paired variants remain supported. The new held corrections
supply additional evidence for `2 טימותיאוס`, `הראשונה לקורינתים`,
`השנייה לקורינתים`, and `הראשונה לפטרוס`. Published source/translation pairs
also supply `א׳ פטרוס`, `אפסים`, `קולוסים`, `רומים`, and `עברים`.
These spellings are not claimed to be recognized by the pinned library. The
[authentic regression fixture](../tests/fixtures/reference_alias_cases.json)
records immutable task/source/result paths, Git blob IDs and SHA-256 values.
The [Winter 2021 fixture](../tests/fixtures/winter_reference_alias_cases.json)
adds six exact source-paired forms: the printed English `Hebrew` for `Hebrews`,
and Hebrew `האיגרת הראשונה ליוחנן`, `יוחנן הראשונה`,
`טימותיאוס הראשונה`, `טימותיאוס השנייה`, and `התגלות`.
The first two Hebrew forms identify 1 John; the next two identify 1 Timothy and
2 Timothy respectively; the last identifies Revelation. These are literal
aliases, not a rule that reorders or drops ordinal words, and are not attributed
to the pinned library. The English source wording remains unchanged.
The [remaining runtime fixture](../tests/fixtures/remaining_reference_cases.json)
adds only eight further literal names: German `Joh`, `Jes`, `Offb`, and `Kol`
for John, Isaiah, Revelation, and Colossians; Hebrew `הראשונה ליוחנן`,
`פטרוס הראשונה`, `גלטים`, and `טיטוס` for 1 John, 1 Peter, Galatians, and Titus.
Their evidence is the pinned source/correction pairs, not a claim of library
recognition. The shorter 1 John name cannot salvage a longer epistle title with
an unsupported attached prefix. Gospel/epistle ordinals remain distinct.
The full publication replay additionally preserves these existing variants:

| Language / article | Exact source / target notation |
| --- | --- |
| German `8072bb08-e7aa-4602-a3c6-f82161b740ae` | Acts / Apg |
| German `a4d4e3d1-c4ae-4c84-b991-b4ff13690ab9` | I Peter / 1. Petrus |
| German `b5ea052a-167f-45c4-bf40-cd463c4546b6` | Ecc. / Prediger |
| German `fbb7faf4-04de-4858-b608-09cb6aedaacf` | I Kings / 1. Könige |
| Hebrew `3be24d0c-4c97-452e-b3c3-177e16d990ff` | 1 Peter / א׳ פטרוס; Ephesians / אפסים |
| Hebrew `8aa76d0a-4ff5-403e-a366-7d6b8fe26924` | Colossians / קולוסים |
| Hebrew `affa9e6e-7d44-4174-ae66-fd44cfaeeaa0` | Romans / רומים |
| Hebrew `fb02a7e7-4fdc-4363-bea0-aa8a68b36f8b` | Hebrews / עברים |

Book matching remains longest-first and language scoped. Known numeric, Roman,
Hebrew-letter and textual ordinal prefixes cannot be discarded to reinterpret an
unknown numbered book as a Gospel surname. Conventional geresh marks belong to
known Hebrew Samuel/Kings/Chronicles ordinals. Attached Hebrew bet/mem, optionally
with vav, qualify exact book aliases; arbitrary word prefixes do not.

The source's short `Is` abbreviation is ambiguous with ordinary English. Its
exact capitalized form is recognized only in a complete parenthesized citation,
such as `(Is 65:14)`. Ordinary `is 9:00` and unqualified clocks retain their prior
numeric protection. There is no general short-word or approximate-name matching.

## Quotation delimiters and verse labels

A clear quoted-prose introduction after a complete known-book reference keeps the
last member of a verse list. For example, `Ephesians 5:22,24` and
`Epheser 5:22,24: „…“` retain both verses. The introducer consumes only its delimiter;
real citations later inside the quotation remain separately protected. Hebrew
`״` can open a prose quotation as well as appear inside numeric notation. Numeric,
malformed or ambiguous quoted endpoints cannot be silently discarded.

Explicit English `Verse`, German `Vers` and Hebrew `פסוק` labels introducing quoted
prose have separate numbered keys. This permits `Verse 21, “…”` and
`פסוק 21: ״…״` without inventing a chapter reference, while rejecting changed,
missing or duplicated labels. These keys cannot replace a real chapter/verse
citation. Malformed or nested labels remain invalid; there is no blanket exemption
for a bare number followed by a colon.

## Verified runtime failures and limits

Four saved final corrections at translation revision
`b3efe9466ad6964001b25e08542d68a8a1e0d98a` retain all 71 genuine chapter/verse
occurrences plus prose verse labels 21/33:

- Hebrew ALL NATURE SINGS: `1c122aa1ca732afe5706117e045bca06`
- German A Christ-Centered Courtship: `02c2ad0c8d9f1ca54bc1985a6e0d2fba`
- Hebrew Dealing with Cynicism: `1704a36496c1b1fb90c8e7c27f01bc51`
- Hebrew A Christ-Centered Courtship: `e7dbc43e1a96f372d73e73c668eb250f`

Tests read their immutable `results/correct.json` artifacts and source snapshots,
verify provenance, and replay complete candidates. All four were held before any
semantic review. Passing this deterministic gate does not approve their language
quality, reopen terminal tasks, authorize another attempt or publish them.

Four further Winter 2021 final corrections at revision
`9fb829988df25d92d5051fc13085e7e268d12322` preserve 59 citation occurrences:
Hebrew This Thing Called Love, German and Hebrew Rest, and Hebrew The Dew of
Youth. Ten occurrences were blocked by the six literal aliases above. Regression
tests replay the entire immutable correction results and preserve wrong-book,
ordinal, chapter, verse, range, missing-occurrence and duplicate-occurrence
rejections. Their saved tasks remain held with zero semantic reviews; an
authorized future attempt still needs independent review before publication.

Regression checks retain wrong-book/ordinal/chapter/verse rejection, list and range
boundaries, duplicate occurrences, numeric tails, clock/amount guards and exact
HTML/metadata protections. Every existing publication and the full current English
archive must validate before this change is considered complete. Budgets, prompts,
attempt limits, saved history and publication state are unchanged.

The remaining fixture at `5154bba7cfec68fc64dc04b056840d200418a655` reproduces
five complete equivalent final corrections (54 chapter/verse occurrences plus
two source-qualified chapter-only mentions): German ALL NATURE
SINGS and In the World, but not of it; Hebrew By Their Fruits, Little Ramona’s
Pride, and The Strait Gate to Life. The Summer tasks retain two translation
attempts and zero reviews; German ALL NATURE SINGS retains its one prior review
and one correction attempt. None receives a new review or publication merely
because reference notation now validates. The authentic Korean The Strait Gate
to Life remains a negative because John 4:16 was expanded to 4:15–16.

## Pointed Hebrew book names and clock diagnostics

The October 5, 2026 follow-up recognizes Hebrew pointing and cantillation within
the existing finite book-name aliases, including the fetched `קֹהֶלֶת` spelling.
It preserves the original decoded-text offsets and every book/ordinal/numeric
identity. Punctuation is not pointing; unsupported word prefixes and hidden
combining/format characters cannot expose a known suffix or conceal an ordinal.
No additional book-letter spelling is inferred.

A changed clock without an explicit source AM/PM period still fails. Clear time
cues now produce a clock-specific diagnostic: `dinner at 5:30` must retain 5:30,
rather than inferring 17:30. Explicit equivalent conversions such as 5:30 pm to
17:30 keep their existing source-backed behavior. New frozen translation/review
instructions explain this distinction; prior requests and held records retain
their original bytes and outcomes.
