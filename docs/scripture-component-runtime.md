# Gated collector integration for never-paid Scripture holds

The collector now has an opt-in component path. The checked-in configuration does
not enable `scripture_components_runtime_enabled`; absence and boolean `false`
keep it closed. Only exact boolean `true` enables the path. No checked-in queue,
State, content, live provider, deployed feature or funding authority is changed by
this implementation. Tests use disposable repositories and fake Batch providers.

## Entry point and deliberate limits

`Engine.accept_component_revision(campaign_id, entry_id, scripture_policy,
evidence)` accepts an explicitly supplied, complete source-bound component plan
and archived approved-provider evidence. It does not discover, infer or obtain
permission for a plan. A trusted operational plan selection/queue interface and
live activation remain separate work; ordinary requests keep their frozen v1
quotation contract. Once a revision is recorded, ordinary collector acceptance
and resume, prepare, upload, submit, reconciliation, collection, bounded review
and first-publication paths execute it.

Admission requires an original uncancelled manual translation attention hold,
with no previous task, candidate, publication or pair history and no task directory,
prepared reservation, uncertain submission or unresolved batch inventory. The
complete source/evidence/plan must validate and the original frozen complete-review
contract must be version 2. Missing/ambiguous associations, unsupported morphology,
uncited inference, subword replacement, interior omission and multiverse component
scopes remain held. Supported whole-word replacements, bracketed insertions,
punctuation and boundary omissions still require independent semantic review.

Existing publications cannot be reopened by this path. Manual retries, review,
automatic continuation and downstream recovery explicitly exclude component
predecessors until a separately versioned successor lifecycle is authorized.
Completed-publication update helpers remain offline and unfunded.

## One authoritative history

The existing manual-admission entry receives one additive, immutable
`component_revision`. Its original attention status, events, diagnostics and
provenance stay unchanged. Original queue bytes, campaign v1 policy, prompts,
models, prices, limits and budget are not edited. The same original task identity
is materialized only after a durable revision intent; the collector owns all
ordinary task, campaign, record and Batch history. There is no second execution
state machine or private funding journal.

The revision freezes the explicit v2 evidence policy, source and original campaign
projection, complete response schemas, component prompt addenda and a 2-generation
plus 2-review ceiling. Exact request artifacts, immutable bounded provider attempt
text, parsed results, decisions and per-generation selection proofs are retained
under the ordinary task. The mutable candidate/selection pointers do not replace
that stage history. Parsed results are rechecked against immutable provider text,
exact request binding, actual model and usage before they can support later stages
or publication. Malformed, refused, truncated, ambiguous, oversized and incomplete
responses hold without another correction request.

## Original-envelope funding

`campaign.reserved_usd` continues to equal actual permanent root Batch reservations.
The full-cycle earmark is derived from the frozen revision minus that task's proven
prepared-request allocations. Original remaining headroom is checked as:

- Permanent original Batch reservations (or higher reported usage)
- Plus every component cycle's still-unspent frozen earmark
- Plus any proposed ordinary sibling reservation

Every subtraction is recomputed from exact canonical prepared payload bytes and
frozen model rates. Reservation-transfer children are verified and never counted
twice. Neither cancellation, failed review, unused correction capacity nor unknown
creation releases this versioned component commitment. Legacy siblings compete for
the same remaining envelope. Inconsistent legacy inventories hold; the collector
never invents spare funds or rewrites original ceilings.

## Crash and safety boundaries

A component-containing Batch carries an immutable preparation intent before task
attempt counters or campaign reservation counters change. Exact before/after
partial writes reconcile idempotently before ordinary preparation or submission.
Upload and create retain the existing separate input-file and submission-intent
checkpoints. Unknown creates only reconcile by submission key; absence of a match
never permits a second create. Source, permanent Git-backed human control (including committed deletions),
cancellation, feature gate, evidence freshness and exact request provenance are checked before known-new create.
An intent-time guard failure is recorded as proven `create_not_called`; an actual
uncertain submission never takes that path.

Old result replay cannot consume a new stage, duplicate usage or erase an attempt.
An advanced task with an uncollected older Batch is reconciled only against the
exact already-consumed result. Cancellation and terminal late results retain
bounded provider evidence and reported usage without advancing work.

First publication requires the current submitted independent review, an immutable
quality-pass decision and exact candidate/selection/result provenance. An immutable
publication intent is written before any HTML, sidecar or publication-record write.
Restart may complete only absent or byte-identical owned outputs. Changed human
bytes or verified committed human ownership (even byte-identical planned output),
displaced latest-task ownership, source drift, cancellation and existing
publications block it. A finalized task is never permission to recreate or replace
later content. Partial terminal decision/history writes reconcile without duplicate
audit events.

## Validation

The runtime tests exercise actual Engine and collector methods with fake provider
I/O, including all-write crash boundaries, exact replay, source/human drift,
cancellation, frozen contracts, shared budget earmarks, supported component shapes,
malformed outputs, unknown creates and the hard 2+2 stage maximum. Run the focused
`tests/test_scripture_component_runtime.py` and the full validation commands in
`AGENTS.md`. These results establish offline behavior only; they are not live
OpenAI, GitHub Actions, translation or deployment results.
