# GetBible quotation evidence contract 1

New opted-in campaigns freeze `scripture_quotes` and per-task content-addressed
`state/scripture/<sha256>.json` evidence before any paid Batch request. Existing
campaigns without that field retain their frozen prompts, request schema, ledgers
and request bytes. This is application-side anonymous MCP prefetch; the existing
OpenAI `/v1/chat/completions` Batch request does not have remote MCP tools.

## Source and edition identity

`data/scripture-translations.json` is a byte-for-byte copy of the owner-approved
website map at commit `0b0a8fc52cbbd47bef8e1f84328a825d169d0543`. The pinned repository,
blob SHA, SHA-256, official documentation, server source commit and rights note are
in `docs/third-party/getbible-provenance.json`. The map retains the website's full
per-edition licensing/provenance caveats. Approval of an edition is not a new
license to redistribute its content: Afrikaans and Arabic have restrictions,
Hebrew rights are unspecified, and KJV/Almeida catalog metadata says GPL.

There is no English or alternative-edition fallback. Bengali, Hindi, Indonesian,
Swahili and Urdu are explicitly unavailable for Scripture quote substitution.
Ordinary article prose remains translatable. Unsupported quotation cases become
explicit attention reasons and do not start a paid attempt.

All MCP calls use the exact public root endpoint `https://mcp.getbible.net/`,
explicit mapped edition and `api_version: "v2"`. The pinned `mcp==2.2.0` SDK uses
`Client`, not the older shared environment's `ClientSession`. Install
`requirements-scripture.txt` in the runtime that enables this contract. The SDK
handles discovery and transport; no credentials, custom endpoint or model tool
is accepted from article content. Only `get_scripture` and `query_verses` are
exposed by the adapter. Transport/MCP errors, absent structured data, wrong
edition/scope, missing/duplicate verses and inconsistent hashes fail closed.

Full chapters are fetched through `get_scripture`, which checks the chapter's
published SHA before and after retrieval. That SHA-1 is a provider version token,
not a local JSON-byte checksum or proof of publisher authenticity. Exact native
result envelopes, source/cache information, effective tool arguments, endpoint,
client version and local SHA-256 are retained. `query_verses` is also supported,
but never receives an invented chapter checksum.

## Quotation selection and verification

1. Conservative source extraction uses decoded structural text blocks and exact
   reference identities. Delimited quotes (including inline emphasis), bare
   blockquotes and unmarked complete cited verses are recognized. Reference-only
   prose/allusions are never replaced. An independent reviewer must identify
   missed, unmarked or uncited genuine Scripture quotes; missing evidence is a
   major failure, not permission to generate Scripture words.
2. Each English fragment must align uniquely inside the cited KJV verses after
   case/word-punctuation normalization. Ordered ellipsis pieces align separately;
   too-short ambiguous phrases, unresolvable source words and uncertain reference
   association require attention. Matching words elsewhere in the full chapter
   yield `printed_reference_mismatch`. For example, quoting John 4:15–16 while
   printing John 4:16 never silently drops verse 15 or changes the reference.
3. Source and target numeric addresses are not assumed semantically equivalent.
   Live GetBible v2 queries on 2026-10-04 showed KJV Psalm 20:7 is the
   chariots/horses verse, Synodal Psalm 20:7 is blessings/joy, and Synodal Psalm
   19:8 is chariots/horses. GetBible did not normalize those addresses. Cross-edition
   Psalms remain `unverified_versification` until a reviewed correspondence table
   exists. Other same-address passage correspondence is explicitly independently
   reviewed; no automatic verse/chapter offset is invented.
4. New translation/correction responses add `scripture_selections`. Each quote
   identifies its source quote ID, original structural block, exact candidate
   text offsets, and one ordered list of target verse/text offsets per source
   fragment. Offsets count Unicode characters, are half-open, and refer to the
   exact retrieved verse text. The application reconstructs these substrings;
   it does not trust model-generated Scripture words. Complete source verses
   require complete target verses. Within-fragment discontinuity, reordered or
   overlapping spans, new ellipses, altered words, missing claims, changed block
   locations and claims covering only part of a delimited output quote fail.
5. Valid claims are stored separately with candidate/evidence hashes. Normal
   article candidates and exported metadata retain their original four-field
   and three-field shapes. Independent review receives the same full evidence,
   complete English article/candidate and selection audit. It verifies semantic
   scope, no expansion/omission, negation, attribution, printed citation identity
   and edition correspondence. The gate canonicalizes provider-attested native book names plus existing reviewed German/Hebrew aliases and preserves book/chapter/verse identity/count. Unsupported alternative abbreviations require attention. Mechanical substring checks alone do not prove
   semantic equivalence. Publication rechecks the exact accepted claims.

This initial conservative extractor is not a proof that every archive quotation
can be automated. Uncited quotations, unusual notation, multi-reference or
cross-block associations can need attention. Human-controlled article/language
pairs are excluded before fetching and from all subsequent AI paths; this policy
never applies AI citation gates to human edits.

## Integration and bounds

`policy(config.root)` returns the frozen campaign contract and prompt addendum.
After human exclusion, `freeze_scripture_evidence(engine, source, language,
frozen_policy=campaign['scripture_quotes'])` returns task evidence fields.
`engine.scripture_provider` supports offline injection. Call
`normalize_scripture_candidate(state, task, result)` before storing a new
translation/correction candidate, and `validate_scripture_candidate` before
publication. The request builder includes evidence in every stage and the audit
in reviews. Its byte-based conservative token/cost bound includes the complete
actual request, prompt, evidence and response schema. Full-chain planners must
reserve the complete potential candidate and selection audit too. The frozen full selection-audit ceiling is 32768 bytes; the read-only planning state uses a null scripture_selection_audit placeholder and replaces its size with the complete conservative ceiling. Planning can revalidate historical evidence after provider expiry without rewriting it; real requests cannot. Existing quote candidates without a valid prior audit require repair; an empty detected quote set can receive a deterministically checked empty audit.

A frozen evidence-byte maximum rejects oversized complete inputs; it never
truncates the article or evidence to fit. A shorter provider cache expiry wins;
no-store/unarchivable results cannot be frozen and expired evidence cannot start
another request. A new campaign must retrieve fresh evidence. Already-submitted
request/audit history remains immutable and is not reused as an operational
Scripture cache for future campaigns.

## Verification

On 2026-10-04 an isolated environment with `mcp==2.2.0`, `httpx==0.28.1` and
SOCKS support completed anonymous tools discovery, `query_verses` KJV/v2
John 4:15–16, and `get_scripture` KJV/v2 and Luther1545/v2 John 4. Every result
had `isError=false`, structured native data and the expected identities; both
full chapters had `consistency_checked=true`. The Synodal negative test above
also completed. No OpenAI request or paid API call was made.

`scripts/check_getbible_mcp.py` is an explicit read-only smoke test, never part of
ordinary offline tests. Unit fixtures are deliberately reduced test envelopes,
not production evidence; their README records the modifications.
