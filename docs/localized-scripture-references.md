# Localized Scripture reference notation

The October 2, 2026 Luna Spring 2022 run exposed a deterministic parser gap:
German chapter/verse commas and Hebrew letter numerals were reported as missing
references even when their numerical values matched the printed English. A full
read-only audit covered 436 source/candidate HTML block pairs, including 100
reference-bearing blocks. Seven German/Hebrew candidates had equivalent numerical
references; two Russian candidates genuinely changed or omitted them. This is
reference evidence, not a semantic approval of those translations.

## Narrow recognition rules

Only the trusted task or publication language (`deu` or `heb`) enables these
grammars. The model's text cannot select its own validation language. Other
languages retain the existing decimal-colon and source-backed clock handling.

- Recognize qualified German citations such as `Matthäus 24,6–7`, plus narrowly
  delimited parenthesized citation continuations, without converting arbitrary
  decimals such as `3,9 %` or prose number lists
- Decode canonical, explicitly marked Hebrew numerals in recognized citations,
  including mixed letter/decimal forms; ordinary Hebrew words and initials are
  not numbers
- Recognize the authenticated unmarked form `— משלי טז:יח.` only with its
  strict delimiters: a citation dash, known book, whitespace, two canonical
  multi-letter Hebrew numerals separated by an adjacent colon, and a period
  followed by whitespace or end of text. It denotes Proverbs 16:18. Bare,
  single-letter, mixed marked/unmarked, range, list and subverse forms do not
  gain recognition; the ordinary unmarked-numeral grammar remains disabled
- Accept the observed book-qualified space forms `יוחנן י״א 4` and `מתי ט׳ 2–8`
  only through that explicit grammar. Repeated chapter notation such as
  `מתי ה׳ 5:14` must agree; conflicting chapter markers stay blocked
- Preserve each citation's book identity where recognized, chapter, verse list
  or range, multiplicity and containing HTML block. Normalize adjacent list
  entries without flattening all references into an article-wide verse set
- Treat `Matthew 1:18, 19` and `Matthäus 1,18–19` as the same cited verses.
  `Matthew 1:18` is different, and `1:18,20` does not cover verse 19
- Distinguish a known-book chapter introduction followed by clearly nonnumeric
  prose (`Psalm 119: Thou hast ...`) from a dangling chapter/verse citation.
  This creates no chapter-only citation key; the subsequent `Psalm 119:21`
  remains separately protected. Numeric, ambiguous numeral and dangling tails
  remain invalid, and unqualified bare-colon prose is not exempted
- Distinguish German chapter-only prose such as `Römer 13, den „höheren
  Gewalten untertan“ ...` from a chapter/verse comma only in a source-qualified
  block containing `in [known book] [chapter] to [plain prose]`. This requires
  whitespace and clear prose words or a balanced prose quotation. Every complete
  chapter-only book/chapter occurrence is then compared in that same block,
  including the source's neighboring 1 Peter 2. Wrong books, chapters, omissions
  and duplicates fail. Other historical chapter-only syntax does not gain
  numeric recognition; unqualified comma, numeral and dangling tails still fail
- Keep an invalid occurrence when either new form adds an unsupported numeric,
  Roman or named book ordinal. For example, an extra `4 Römer 14, ...` in a
  source-qualified chapter block or `— הרביעית משלי טז:יח.` cannot disappear
  beside a valid occurrence. Complete supported numbered-book aliases retain
  their identities; unrelated historical unsupported syntax is unchanged
- Recognize the observed attached Hebrew `ב` ("in"), optionally preceded by `ו`,
  before a mapped book, and the complete `הראשונה אל הקורינתים` alias for
  1 Corinthians. Arbitrary word prefixes and conflicting ordinals stay blocked
- Recognize one explicit verse label directly after a complete known-book
  citation: English `Matthew 19:3-12: V.4 In ...` and Hebrew
  `מתי 19:3–12: פס׳ 4: בראשית… כששאלו ...`. In Hebrew-paired text, the label
  number is protected separately and tied to that citation; changing or omitting
  it fails. Nested, malformed or unqualified labels do not get this recognition

Malformed or unsupported notation must not conceal a source reference. Known
book swaps, changed chapters, added/missing verses, duplicate occurrences and
references moved to another block remain failures. Printed English numbering
remains authoritative: changing Psalm 20:7/8 to an edition's Psalm 19:8/9 is not
punctuation normalization. Independent semantic review still checks wording,
theology, attribution and any citation details outside the deterministic grammar.

The format rules are supported by [Unicode CLDR's Hebrew numbering rules](https://github.com/unicode-org/cldr/blob/main/common/rbnf/root.xml)
and [University of Passau's Bible citation guidance](https://www.geku.uni-passau.de/fileadmin/dokumente/fakultaeten/geku/lehrstuehle/huebenthal/Dateien/02_Studium_und_Lehre/Tippsheet_Zitieren_von_Bibelstellen.pdf).
These sources establish notation only; no substitute Bible edition is inserted
into article quotations.

## Historical state and reuse

This is a pure validation-recognition change. It does not change frozen campaign
models, prompts, token limits, source snapshots, budgets or prepared batch bytes.
Existing terminal holds, attempts and rejection evidence are not rewritten or
automatically released. A later explicitly authorized, bounded exact-candidate
review can reuse the saved candidate; the independent reviewer must still pass
it before publication. Repair workflows keep their existing bounded stages,
funding and once-per-source restrictions.

New and in-flight candidates use the corrected parser when they reach ordinary
validation. Historical public versions are checked consistently by review sync,
repository validation, projection and export. There is no special export bypass.

## Evidence and tests

`tests/fixtures/localized_reference_cases.json` contains minimal exact excerpts
from already-public source/candidate records at pinned commit
`e806e88960cd4e28437986027fbfe91edbe1d95f`, with artifact paths, SHA-256s and Git
blob identities. Synthetic counterexamples are explicitly labeled.

`tests/fixtures/reference_introduction_cases.json` adds five complete text blocks
from three public held candidates at `d572a7cc319cc3bd07c19e60d89066cb77831914`,
with immutable paths, hashes and source links. These reproduce the October 3
audit's chapter-introduction, attached-preposition, explicit verse-label and
full-epistle-name false holds. All three saved candidates pass the corrected
deterministic gates; this does not supply their still-required semantic review.

Tests cover notation, invalid forms, book and verse changes, clocks, multiplicity,
language scoping, HTML placement and immutable terminal holds. Mocked lifecycle
tests verify that a newly recognized candidate still needs an independent review,
and that a saved historical candidate can only be published through a separately
authorized bounded review. No live provider call, paid retry or held-state rewrite
is part of this change.

The expanded finite book-name table and quote/list boundary rules are documented
in [Exact Scripture book identities](scripture-book-aliases.md).

`tests/fixtures/remaining_reference_cases.json` records the complete immutable
final corrections at `5154bba7cfec68fc64dc04b056840d200418a655` for German ALL
NATURE SINGS and four Summer 2020 articles, plus the authentic Korean negative.
The five equivalent candidates preserve all 54 chapter/verse occurrences plus
the two source-qualified chapter-only mentions. Ten pinned
blocks reproduce eight literal alias gaps, the German prose comma, the unmarked
Hebrew citation and the Korean range change. The Korean correction still fails:
printed John 4:16 became 4:15–16. The quotation's wording does not authorize a
change to its printed reference. Hash checks, complete-candidate mutations and
unchanged-history checks protect the evidence; no saved task is reopened.
