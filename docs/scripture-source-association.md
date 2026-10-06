# Explicit Scripture quotation association, version 2

The old extractor attached every delimited quotation in a paragraph to that
paragraph's sole Scripture reference. That made the ordinary word “seeds” in
*God in the Cloud* a purported quotation of Job 26:8, and Jacques's historical
dialogue a purported quotation of Matthew 5:39. It could also miss emphasized
partial Scripture quotations when multiple references appeared in one paragraph.

The local change introduces `scripture_quotes.source_association_version="2"`.
`policy()` freezes it for **new requests only**. A missing field or the explicit
value `"1"` retains the original extraction function. Unknown versions fail;
loading evidence requires agreement between its association version and the
campaign's frozen policy. Selection-audit adoption also compares the association
version. No old request, evidence bundle, source snapshot, selection audit,
admission decision, or publication is rewritten.

The overall Scripture contract remains version 1; selection normalization remains
its separately frozen version 2. Association version 2 does not authorize retries,
relax admission attention guards, provide a new budget, or bypass independent
review. Installing or publishing code alone cannot release the 27 persisted
attention decisions from `gh-37439892859`. Their old campaign policy still selects
the old extractor. A future request would need a newly frozen policy and ordinary
admission authority; the parent continuation decision is outside this repair.

## Supported proof

The extractor uses the strict bounded HTML parser and original decoded character
offsets. It does not search an entire paragraph for a Bible-like matching substring.
Each direct quotation must occupy one finite shape:

- An emphasis subtree ending with one attribution citation, or a separately
  emphasized quotation with its adjacent external citation
- One complete delimited quotation with its adjacent external citation, or one
  direct attribution citation inside its delimiters
- A delimited quotation immediately introduced by a citation and a supported
  punctuation/attribution bridge, including bare/dash juxtaposition, colon, comma, `says`, `saith`, `saying`,
  `reads:`, `tells us that`, or `the Bible says`
- A whole undelimited paragraph ending with one attribution citation, whose entire
  quotation must subsequently align with the cited English evidence. A whole
  blockquote paragraph additionally permits a plain trailing citation

Independent shapes can appear together in a paragraph. Nested genuine quotations
retain their own scope. Delimiters around ordinary dialogue do not confer Scripture
identity. Reference-bearing speech is an allusion only when its citation is
inside that speech scope and a finite English allusion construction supports it, such as “as we read in”, “as taught in”, “from
the words of Paul in”, or “as [citation] says”. A known parenthetical citation in
a separate, wholly unmarked prose sentence may also own its reference-only
occurrence. A preceding period alone does not establish unrelated prose: introductory
citations, continuation/answering cues, and unmatched marked quotations remain
attention cases. Styling the citation itself does not create a quotation. Neither a prose cue
outside a quotation nor arbitrary introductory wording can discharge a separate
marked quotation. An outer cited-allusion speech does not own an unresolved nested
marked span. Parenthetical prose ownership likewise refuses unresolved delimited
material, closing the same bypass through parentheses.
Unknown speech/citation combinations,
conflicting or duplicate adjacent references, malformed tails, authored bracket
insertions, and bounded-parser failures hold for attention. A failed explicit
association is never rescued by a smaller matching substring.

Coverage is enforced per block, including blocks that already yielded a genuine
quotation. Every citation accompanying marked material must belong to an explicit
Scripture scope or a justified allusion. Otherwise the entire block holds with
`unresolved_quote_reference`. This preserves the ordinary “seeds” because its Job
citation belongs to the genuine emphasis scope, and preserves historical dialogue
whose citations belong to allusions. It prevents silently losing a second genuine
quotation after successfully extracting the first. Both directions are checked:
unowned citations cannot disappear, and a second marked fragment cannot vanish
merely because the first fragment already owns the shared citation. Distinct
marked scopes sharing one citation hold; only identical bounds can be deduplicated.
An emphasis subtree cannot truncate a larger enclosing delimited quotation or
discard an authored insertion. Unknown Ro./Mt./Ep./Jo. forms
are held; no new book identities are guessed.

The five additional literal English aliases are Exo → Exodus, Pro → Proverbs,
Jam → James, 1 Kin → 1 Kings, and Act → Acts. Every one occurs in the pinned Cloud
source. They map only to finite canonical identities and preserve original text
and offsets. There is no generic abbreviation inference, and the shared legacy
reference parser is unchanged. Target names additionally require existing reviewed
aliases or attestation from the frozen approved-edition lookup.

The frozen bundle records exact quotation bounds, reference bounds, structural
paths, delimiter ordinal and topology, citation side, and quote/citation endpoint
ancestry. Candidate proof requires the same associations and HTML structure.
Reference-only occurrences retain their classification even when translated and
when they cite the same identity as a neighboring genuine quote. Prose attribution
bridges in translated HTML follow the frozen citation occurrence, quote/node
anchor, and side; they need not repeat the English word “says”. Candidate words
must still equal the exact approved target-edition selections. A complete verse
cannot acquire an extra unclaimed ordinary-prose occurrence. Complete quotations
can use the existing bounded metadata repair; partial and ellipsis quotations still
cannot receive automatic offset repair. Ellipsis fragment order, printed range,
source extent, and exact target words remain required.

The old complete-cited-verse detection is retained as an attention-only check.
Every complete cited verse/range occurrence must be fully contained by a proved
quotation scope. A merely overlapping partial scope or a second unmarked
occurrence does not qualify. An uncovered complete occurrence holds with
`unmarked_quote_scope`; a partial ordinary-prose substring remains ordinary.
This cannot create a quotation by accepting an arbitrary matching substring.

Before evidence can freeze, another fail-closed check compares every otherwise
unclaimed marked span against the already fetched cited KJV chapters using the
existing exact normalized-word/ordered-ellipsis alignment. A match produces
`unassociated_source_quote`. It never assigns a missing printed citation, invents
a target selection, or makes an arbitrary substring publishable. This English
backstop also catches genuine marked repeats separated by ordinary prose. Its
absence of a match is not semantic approval; independent review still checks
uncited text and paraphrases.

Parser byte/node/depth limits are inherited; version 2 additionally holds rather
than truncating when its conservative association-work estimate exceeds 2,000,000.
The two English coverage scans share a separate cumulative 2,000,000 work ceiling before
alignment calls. It charges token-comparison products and source/chapter
character and tokenization overhead. Exceeding it holds the whole source with
`source_association_limit`; it never skips a remaining span or truncates text.
The 500-span × 65-chapter adversarial control now schedules at most four
alignment calls rather than 32,500.

## Complete pinned-article replay

The reproducible inventory is
`tests/fixtures/source-association-archives.json`. It includes all 69 paragraphs,
source file and canonical hashes, exact spans, references, inferred uncited
candidates clearly separated from printed identities, and archive coverage.

*God in the Cloud*, UUID `3dd1acd1-f006-4519-b75c-577439a8b95c`, source fingerprint
`402193c80d2894154ccb0532fd8d7485388997a2a7d80b9fbe7c62d14a8efd80`:

| Paragraph | Explicit quotation associations | Other reference/candidate |
| --- | --- | --- |
| 1 | Exodus 16:10 | Printed Exo alias |
| 2 | None | Ordinary prose |
| 3 | Job 26:8 | “seeds” stays ordinary; following prose stays outside emphasis |
| 4 | Job 30:15; Lamentations 3:44; Job 36:32 | 1 Kings 18:44 is reference-only |
| 5 | Matthew 26:8; Proverbs 4:18; Job 5:14 | Partial and leading-ellipsis extents retained |
| 6 | James 5:17; Hebrews 12:21 | Uncited “mount that might be touched” matches Hebrews 12:18 and holds; it is not assigned to 12:21 |
| 7 | 1 Kings 19:11–13 | Leading/trailing omissions retained |
| 8 | None | “day in the sun” stays ordinary |
| 9 | Job 5:7 | Leading ellipsis retained |
| 10 | None | Ordinary prose |
| 11 | James 4:14 | 1 Peter 4:1 reference-only; uncited James repetition is a genuine unassociated-source hold |
| 12 | Revelation 18:7; James 4:14 | Separate partial/ellipsis scopes |
| 13 | Job 26:9 | Following prose stays outside emphasis |
| 14–16 | None | Ordinary prose |
| 17 | Hebrews 6:19; Romans 5:3–4 | Uncited Jacob wording remains for review, not assigned a fabricated Genesis citation |
| 18 | 1 Corinthians 13:13 | Leading ellipsis retained |
| 19 | Acts 1:9; James 4:14; Luke 21:27; Revelation 1:7 | Four independent scopes; Revelation's internal omission retained |

Result: 23 explicit quotation scopes and all 25 printed verse references are
inventoried. The false “seeds” association is gone. In the initial offline pass,
none of these 23 English alignments or target-edition selections could be
authenticated from local archives. That reproducible archive-only replay stops at
missing `kjv/2/16`, not at “seeds”. A subsequent, separately authorized read-only
GetBible verification at 17:48 UTC on October 6 confirmed all 23 English
alignments: seven complete and 16 partial/ellipsis. This verifies source wording;
it does not release any held admission or replace target evidence and independent
review. The later coverage backstop supersedes the earlier diagnostic result
that all 15 target preflights could pass: all 15 now hold at p6 with
`unassociated_source_quote`. Exhaustive checking of the five unclaimed marked
spans against all 21 authenticated, already-cited KJV chapters found exactly two
canonical matches:

- p6: “mount that might be touched” matches Hebrews 12:18, not the separately
  printed Hebrews 12:21 quotation
- p11: “ye know not what shall be on the morrow.” repeats James 4:14 without its
  own proved citation; the later 1 Peter 4:1 allusion cannot supply one

Neither receives an invented reference or a target replacement. The first hold
prevents admission; the exhaustive diagnostic records both underlying blockers.
The uncited Jacob wording remains for independent review. Genesis 47 was not
retrieved, and absence of a match in the 21 cited chapters is not proof that this
or other unclaimed wording is non-Scripture.

*The Inquisition of Jacques Dosie*, UUID
`069fc797-01e4-44d3-8b71-1c07f0821965`, source fingerprint
`ad31a29abcaa9fc4133d05837032668a19ddc92690fa0c75fc335b4adf6d6cb3`:

| Paragraph | Classification |
| --- | --- |
| 9 | Matthew 5:39, historical speech with explicit allusion cue |
| 13 | Matthew 5:11, historical speech with explicit allusion cue |
| 17 | Acts 24:5, historical speech with explicit allusion cue |
| 19 | 1 Peter 3:21, historical speech with explicit allusion cue |
| 21 | Romans 7:18, historical speech with explicit allusion cue |
| 24 | Mark 16:19, historical speech with explicit allusion cue |
| 35 | Chapter-only 2 Chronicles 18; diagnostic inventory only, outside verse extractor grammar |
| 50 | Romans 2:4, one explicit emphasized quotation with authored `[that]` insertion |
| All other paragraphs | No explicit Scripture quotation/reference scope |

Result: six verse allusions remain prose; p50 is the only direct Scripture
quotation. Fresh replay stops **before any provider call** with
`source_quote_annotation` because `[that]` cannot silently become exact canonical
Bible wording. The annotation is preserved in the immutable source. No policy for
translating/removing such insertions is introduced. This is a deeper genuine
blocker for all target editions, including the 12 resumed language pairs.

## Offline evidence limits and validation

The archive scan verified 105 frozen bundles, 119 lookup envelopes, and 49 distinct
lookup keys. All canonical bundle hashes and result hashes matched. Existing
expiries were November 4, 2026, 13:24–16:25 UTC. None contains any of the 29 needed
or diagnostic-candidate chapters. All 464 requested chapter/edition combinations
across KJV and the 15 approved targets are absent; this is absence rather than
expiry. Five other configured language editions are separately unavailable.

The initial local code/test pass made no GetBible, OpenAI, or other provider
request. Subsequent anonymous GetBible verification was separately authorized
and recorded outside runtime history; it must not be confused with these offline
fixtures. No paid model activity was authorized by this repair. Positive alignment
controls that need missing chapters explicitly use a `SyntheticChapter` test
double labeled `synthetic_adversarial_control_not_provider_provenance`; they prove
code behavior, not real provider text or translation eligibility. Existing edited
GetBible fixtures likewise remain ordinary offline fixtures. The archive inventory
copies only authentic metadata and identifies missing payloads rather than
fabricating provenance.

The focused regression file covers both complete sources and legacy failures,
multiple and nested genuine scopes, quoted biblical speakers, inside/outside and
preceding citations, false Bible-looking speech, malformed/unknown/duplicate
citations, wrong ranges, authored insertions, unavailable editions, source edits,
ordinary-prose claims, delimiter relocation/deletion, inline scope escape, shared
allusion identities, frozen-policy mismatches, and work limits. Existing Scripture
selection, structure, provider, and lifecycle tests remain in force. One older
subset-citation helper test explicitly pins version 1 because its intended assertion
is historical behavior; new-default controls require exact associations.

## Whole-archive regression coverage

The first local helper revision was rejected after a read-only scan of all 1,167
current English articles exposed genuine quotations that it had downgraded to
zero scopes. The correction adds the per-block ownership invariant above, plain
blockquote tails, separate emphasis/citation scopes, and finite preceding/following
attribution forms. `tests/fixtures/scripture-association-coverage.json` contains
27 verbatim source blocks pinned to English revision
`f454b2d575b6a491982a483cd69708bddb270669`: eight complete supported-source inventories
covering nine citations, and 19 attention cases, including unsupported aliases,
shared/split quotation extents, introductions, repeats, alternative renderings,
and split emphasis containing an authored insertion. This fixture
contains source text and hashes, not invented provider evidence.

New tests also cover partial losses within otherwise successful blocks, German and
Hebrew translated attribution bridges, malformed blockquote tails, and citation-only
emphasis. The earlier passing test run did not establish archive-wide correctness;
whole-archive coverage and independent review are required alongside the focused
unit tests before considering the new policy suitable for global adoption.

The final full-archive source-only scan covered all 1,167 articles with the stable
helper hash `958ed84345049a8914ddd4e0af56310b7f96680227b6a537005ca62d4a1b3f00`:
674 complete inventories containing 797 quotation scopes, and 493 attention holds
(317 unresolved ownership/extent, 173 ambiguous associations, three unmarked
scopes). The successful inventories recorded 71 justified allusion references
and 145 unmarked reference-only entries in addition to the 797 quoted references.
No article with old marked-quote candidates silently becomes a zero-scope success,
down from 74 in the rejected helper. The policy is deliberately conservative:
*John Paton’s Home Life* now holds its separately quoted “closet”/Matthew 6:6
ownership instead of using the permissive parenthetical route. Source words are
preserved, never forcibly replaced. This extra attention is a disclosed limit of
the finite policy rather than evidence that its ordinary prose is canonical Bible text. These are extraction counts, not translation eligibility or
evidence-fetch success counts; the English backstop may impose further genuine
holds after chapters are available.

The latest focused run passes all 42 source-association tests, including the
large adversarial work-budget control. The repository-wide suite and independent exact-tree review
remain separate validation steps; earlier passing tests did not excuse the
archive-wide regressions subsequently discovered and corrected.

The final cached 27-pair replay used association helper
`958ed84345049a8914ddd4e0af56310b7f96680227b6a537005ca62d4a1b3f00`
and evidence helper
`2274e773507daad61ca5a6f60a92cc46989ca19bfd1224fdc9ac6ed486fa2eca`.
It made zero network calls, retained both sources' explicit extents, and preserved
the ledger's actual legacy `ambiguous_source_quote` entries byte-for-byte. The
complete-unmarked-verse check runs and passes before Cloud reaches the uncited
p6 marked-span hold. The resource bound is not an earlier gate for either article.
The 15 Cloud and 12 Inquisition outcomes are prospective new-policy diagnostics,
not changes to the persisted decisions or authority to continue paid work.
