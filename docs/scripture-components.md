# Explicit authored-quotation component evidence (offline v2)

This is an executable **opt-in evidence and candidate-validation path**. It is
not enabled for admission, paid requests, review approval or publication. The
ordinary `policy(root)` still returns the unchanged version-1 policy. Historical
version-1 policies, missing fields, source-association versions and saved audits
retain their previous interpretation.

## Rebuild provenance

The previous cloud workspace reset after a foundation patch passed 855 tests.
That patch, its exact provider envelopes and its logs were lost. The 855 result
is historical and does not verify this reconstruction. This implementation was
rebuilt on pinned main `8c8ea3177f0187955a493ded2d383f4fbba0c736` and must be tested
again. Recorded source and accepted-candidate hashes are recovered from immutable
repository state; the component source was reconstructed from the task record,
then hardened during independent review.

Two anonymous GetBible MCP reads retrieved new Joel chapter envelopes on
October 7, 2026. Their timestamps and hashes differ from the lost October 6
envelopes. The files explicitly say `fresh`; they are not represented as exact
recovered bytes. The actual Joel 2:25 verse strings match the recorded strings.
Tests using these archives freeze the historical verification clock. They do not
renew freshness or permit live reuse after cache expiry. Other short controls
and the existing John fixture provider are offline synthetic test controls.

## Frozen contract and executable entry points

`scripture_component_evidence.component_policy(root, source, plans)` constructs
an explicit version-2 policy. Each plan is exactly `{scope_sha256, operations}`.
The ordered list must cover every quotation returned by the existing independent
source association parser. The whole English snapshot and full plan list are
hash-bound. No missing plan, repeated quotation identity or uncited verse
association is guessed. All English source bytes and printed references remain
unchanged.

`scripture_evidence.frozen_policy` validates the new schema;
`scripture_evidence.build_evidence` routes version 2 through the integrated
component branch of `_build_associated_evidence`. That branch retains the same
approved-edition, missing-edition, versification, allusion/uncited-quotation,
complete evidence-size and structural-association guards. Complete provider
chapters are frozen, exact edition/chapter identities checked with the approved
adapter, and result hashes/provenance/cache timestamps validated. English source
components are checked against exact returned verse text. Source plans cannot
change the HTML or silently strip author annotations.

`load_evidence` recomputes version-2 proof against the full saved source and only
the archived provider envelopes. It never fetches a missing chapter. Contract,
source, structural scope, citation inventory, verse text, ordered operations and
stored proofs must all match recomputation. Freshness remains required for new
evidence; historical inspection may replay an expired, unchanged archive.

`check_selections(..., source=source, component_contract=contract)` invokes the
version-2 candidate path and returns a hash-bound **review input**, not approval.
It requires the complete four-field candidate, checks normal HTML/metadata gates,
reconstructs each whole target scope, and validates exact target-edition spans
plus visible authored components. Every source quotation has exactly one claim.
Moved, shortened, overlapping, unclaimed duplicated, or re-attributed quotation
scopes are held. Source and target lexical boundary-omission topology must agree;
provider whitespace padding is not treated as an omitted Biblical phrase.

No old field name silently gains a new interpretation. Version-2 policy/evidence
markers are rejected under historical versions, and old audit adoption or numeric
selection repair cannot reinterpret component proofs.

## Component primitives

All `scripture_components` functions require explicit string component version
`1`. Offsets are Unicode code points; no spelling, case, punctuation or whitespace
normalization is performed.

- `canonical`: exact `start`, `end`, `text` from the frozen verse
- `omit`: exact range and `reason: excerpt_boundary`; only leading/trailing
  boundary omissions are supported
- `insert`: authored `id`, `at`, and visibly bracketed `authored_text`
- `replace`: authored `id`, exact replaced canonical range/text, and bracketed
  `authored_text`; it must replace lexical content
- `punctuation`: authored `id`, `at`, `authored_text`; cannot carry generated words

Consuming operations partition the entire verse once in order. They must render
the complete printed quotation exactly. Every authored ID is unique and mapped
one-to-one in source order and kind; source punctuation is preserved. Canonical
word runs cannot be replaced by mere whitespace. Bracketed empty, control-only,
or punctuation-only payloads cannot erase authored meaning structurally.

`[wo]man` is representable as authored `[wo]` plus canonical `man`; the correct
linguistic translation is not mechanically inferred. Interior omissions,
subword replacements, joined/hyphenated-word replacements and explicit morphology
operations stay held. Whole-word replacement correspondence and translated
insertion meaning still require independent review.

The reference decoder preserves the printed alias and opaque partial marker:
`Rom 2:4` has lookup identity `Romans 2:4`; `Joel 2:25a` retrieves verse 25 without
inventing where `a` ends. Only single-verse component quotations are supported;
complex suffixed ranges and cross-verse component scopes remain held.

## Guards against accidental activation

The new contract is explicitly refused by:

- `freeze_scripture_evidence`, before admission or evidence writes
- `requests.build_request` for translation, correction and both review stages
- the prepared-batch submission guard, before upload and again before creation
- candidate normalization, publication validation and saved-audit adoption

Already-uncertain provider submissions retain their existing read-only
reconciliation behavior. None of these changes authorizes new spend, resets
attempts, releases attention entries, changes published articles or edits
historical requests. A passing component proof always requires independent
review and always reports runtime admission unsupported.

## Portuguese Our Testimony remains held

The complete original source hash is
`c54fa4226bc3571d72d5ba970bfd4c62db65eb663495f74611d338b1b68642ab` and accepted
candidate hash is
`7151479b3bf135e78f6d053acda6ec461415e31fe2aac4369da30a25ac6a583f`.
The integration test binds that actual source and Joel 2:25a scope to authentic
new KJV/Almeida envelopes. It creates only a private in-memory proposal and proves
that `restituir-[nos]` remains held for replacing embedded `vos` and dropping
`-hei`. Source, candidate, history and accepted publication stay unchanged.

Permitting that inflection change requires an explicit morphology policy decision
and independent linguistic review. This implementation does not grant it.

## Remaining work before live use

1. The offline processing protocol now supplies versioned output schemas/prompts,
   exact response bindings and bounded correction/review handling; see
   `scripture-component-processing.md`. Durable provider/result admission and
   independently accepted review records are still required before live use.
2. Separate offline append-only journals now cover proven never-paid holds
   (`scripture-admission-revisions.md`) and exact completed-publication updates
   (`scripture-publication-updates.md`). Their live admission/funding remains
   disabled. Neither rewrites historical contracts or recycles budgets.
3. Exercise end-to-end restart/concurrency, cancellation, human edits, source and
   publication drift, funding ceilings and failed-update-keeps-publication cases.
4. Only after those guards pass, deliberately enable an appropriate new default
   or narrowly authorized request path. Do not remove the explicit runtime holds
   just because evidence/selection checks pass.

Run the focused files and full repository commands documented in `AGENTS.md`.
No ordinary test uses live provider requests or model credentials.
