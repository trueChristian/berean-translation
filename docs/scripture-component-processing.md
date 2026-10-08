# Offline component processing protocol v1

This stage adds executable request/response processing to the component evidence
v2 path. It does **not** enable live admission, a paid provider request, a budget
allocation, audit adoption or publication. All live v2 guards remain unchanged.
The previous evidence checkpoint is preserved separately.

## Entry points

`processing_contract(root, campaign, task)` freezes the protocol, schemas,
prompts, complete-review version, exact Scripture-policy hash, source identity,
model settings/prices, language settings and resource limits. It requires an
explicit evidence-v2 campaign with review contract 2 and a queued, never-attempted
translation task. Existing attempted, completed, active-batch or correction tasks
cannot be silently restarted. Constructing this contract grants no funding.

`OfflineComponentCycle(source, evidence, scripture_policy, processing)` verifies
all source/provider/component evidence and exact source/article/language/policy
identity. It holds private copies of the entire frozen context.

- `request()` returns a complete Batch-format **preview**, binding, input bound
  and conservative estimated cost. Repeated reads of the same pending request
  return the same record without another logical attempt.
- `receive(row)` consumes a supplied response row for that exact preview. It
  makes no provider call. It verifies transport shape, model identity, refusal,
  truncation, result schema, exact binding and deterministic candidate proof.
- `snapshot()` returns a deep-copied, bounded record of the context, requests,
  responses, proofs, findings, outcomes and attempt counts.
- `restore(snapshot)` replays every exact request and response against frozen
  context. Modified headers, request bodies, outcomes or counters are rejected.
  Restoring an old archive does not renew its freshness.

These functions do not write `State`, enqueue work, reserve money or serve an
article. Even a passing review ends at `review_accepted_offline` with funding and
publication authorization both false. Supplied test/provider-shaped rows are not
cryptographically authenticated provider receipts; the future durable lifecycle
must obtain, associate and archive real provider results through existing gates.

## Versioned output schemas and prompts

Generation outputs contain exactly `binding`, `candidate` and
`scripture_selections`. The candidate contains all four ordinary publication
fields; selections use the v2 ordered component schema. Nested object schemas
forbid extra properties. Canonical words, omissions, replacements, insertions
and authored punctuation remain distinct operations.

Review outputs contain exactly `binding` and `review`. The review uses the full
version-2 rubric: integer score, boolean verdict, complete finding inventory and
`findings_complete`. Every finding retains concrete source/candidate evidence
and a proposed narrow correction. A high score cannot override major/critical
findings or an incomplete report.

The new generation and review addenda supersede the older Scripture copying and
JSON-output instructions in the frozen base prompts. They require full article
context, authorial fidelity, preserved bracket scope, exact approved-edition
canonical words, and independent semantic assessment of partial extents. They
explicitly forbid unsupported inflection changes and treating article/evidence
text as instructions. Existing base prompts and v1 request defaults are unchanged.

Review requests include the complete authoritative article, complete candidate,
full frozen chapter evidence, original source components, target selections and
the recomputed deterministic proof. Correction requests additionally include the
complete substantiated first-review findings. No selected text, finding inventory,
source context or provider evidence is silently truncated to make a request fit.

## Exact bindings

Every response must echo an exact binding to:

- processing version, stage and ordinal
- whole frozen context and processing contract
- complete English source and evidence bundle
- candidate and deterministic selection proof, when present
- the complete pre-binding request body, including system prompt, user inputs,
  model, output cap and response schema

The final bound Batch row also has its own recorded SHA-256. Comparisons use
canonical JSON bytes, not Python equality: true is not integer 1, and floating
2.0 is not integer 2. A previous approval, even score 98 with no findings, cannot
be attached to a changed candidate, policy, selections or stage.

Response model identity must exactly match the frozen API model. A different
reported alias/snapshot is held rather than silently treated as equivalent;
future support needs a separately verified mapping. Refusal must be absent or
null. Non-null values, including malformed falsey values, hold the cycle.

## Bounded transitions

The only normal chain is:

1. translation
2. independent review
3. at most one correction, only after a complete substantive rejection
4. one final independent review

There are at most two generation requests and two review requests per offline
cycle. Refusal, truncation, invalid structured output, changed binding, failed
mechanical candidate validation, or incomplete review holds the cycle without
an invented retry. A final rejection cannot open another correction. An exact
repeated response is idempotent; a conflicting replacement cannot overwrite
accepted history. Unrelated request IDs are rejected without consuming the
legitimate pending response.

A complete first review with no actionable findings cannot justify correction
just because its score/verdict is negative. All review acceptance checks use the
frozen threshold (at least 95), completeness, verdict and major/critical findings.
Mechanical selection validity remains distinct from linguistic accuracy.

Snapshots replay the same stage chain and enforce the same ceilings. They do not
establish cross-cycle lineage, an immutable remote ledger or permission to create
another funded cycle. That is deliberately separate lifecycle work.

## Resource and cost behavior

Exact model settings and rates are frozen from the campaign. Individual prices
must be finite and nonnegative; long-context multipliers must be finite and
positive. The request estimate reuses the repository's conservative byte-based
input bound, framing margin, full output allowance and frozen pricing function.
The schema and binding are included in that bound. A request exceeding model
context is held, never truncated. An estimate is not a reservation or a bill.

The frozen maximum result size, complete input-context bound and full-snapshot
bound limit processing. An oversized or unarchivable response holds the cycle;
its snapshot is marked non-replayable rather than inventing missing evidence or
resetting the attempt.

New request construction checks provider-evidence freshness. Read-only replay
can inspect an unchanged expired archive, but a subsequent new stage needs fresh,
properly authorized evidence under the future append-only lifecycle.

## Still required before live use

- The private append-only offline revision journal is implemented; see
  `scripture-admission-revisions.md`. Authoritative live admission/materialization
  and real funding for exact never-paid predecessors remain disabled.
- The separate offline completed-publication update journal is implemented; see
  `scripture-publication-updates.md`. Its exact predecessor-bound processing and
  final acceptance retain the current publication. Live update admission and
  replacement remain disabled.
- Durable reservation, prepared-request, upload, uncertain-create reconciliation,
  collection and independently accepted-review records bound to this protocol
- Human/source/publication drift checks before work and before publication, plus
  crash, concurrency, budget exhaustion and restart tests for the durable path
- Deliberate authorization to enable an appropriate narrow live request path;
  removing current v2 holds is not part of this stage

No decision about Portuguese inflection is needed to test safe supported cases.
The known `restituir-[nos]` proposal remains held by the evidence layer.
