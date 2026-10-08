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
permission for a plan. The bounded Jacques inspection and explicit queue interface described below
provides operational selection; live activation remains separate work. Ordinary
requests keep their frozen v1 quotation contract. Once a revision is recorded, ordinary collector acceptance
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

## Read-only Jacques inspection and explicit immutable selection

`python -m berean_translation inspect-scripture-components` inspects only the
original Jacques manual campaign `gh-37439892859`, article
`069fc797-01e4-44d3-8b71-1c07f0821965`. It selects nothing, allocates nothing,
constructs no OpenAI client and never writes repository State. It discovers one
coherent current English snapshot through the ordinary index/catalogue client,
compares the complete article with the original snapshot, and computes all entry,
scope, plan, source and package hashes internally. Source editors do not maintain
these hashes. The original request, models, prices, budget and campaign contract
remain unchanged.

Optional arguments:

- `--evidence-dir /outside/repository/evidence` reuses complete approved GetBible
  chapter envelopes named `<edition>-<book>-<chapter>.json`.
- `--fetch-evidence` explicitly allows missing anonymous read-only GetBible
  lookups. It never invokes a model. Existing invalid/expired archives hold; they
  are not silently replaced. Use a fresh cache to obtain a fresh inspection.
- `--output /outside/repository/jacques-inspection.json` writes a portable,
  hash-bound package. Without an output path, the complete package is stdout.

The bounded source planner supports this original source only: one uniquely
aligned Romans 2:4 printed extent, its literal leading ellipsis as punctuation,
the literal `[that]` insertion, exact unchanged canonical text and whole-boundary
omissions. It does not normalize or invent canonical text, translate Scripture,
infer target wording, or support Cloud's ranges/interior ellipses or morphology.
Every admitted target Scripture span must still select exact text from its
approved Bible edition; authored article insertions require independent review.

A package contains evidence-backed entries and explicit per-entry holds, with
`funding_allocated=false`, `publication_ready=false` and an empty selection.
The projected full 2-generation/2-review ceiling is informational. It does not
reserve money or establish publication readiness. Complete provider envelopes
are checked for approved scope, full ordered chapter inventory, timestamp and
cache validity, and replayed through the full source-association backstop. Cached
files must be bounded regular files. Expiry cannot exceed the original provider
fetch time plus 30 days or a shorter declared retention; replay never renews it.
Hashes
bind bytes and do not independently authenticate who created an archive. Use
only complete archives obtained through the approved anonymous adapter; synthetic
offline test envelopes are controls, never live supporting evidence.

The **AI — Inspect Jacques Scripture holds** main-branch workflow defaults to
`dry_run=true` and empty `entry_ids`. It installs only the read-only Scripture
client, archives the complete inspection/evidence outside repository State, and
has no OpenAI key. Only an explicit nonempty list of evidence-backed entry IDs
with `dry_run=false` can create a unique immutable `component-gh-<run-id>` file in
the existing queue. Its strict provenance binds repository, workflow, main ref,
run ID and actor. A rerun reuses the exact existing selection; it cannot replace
evidence or change inputs. The gate remains independently disabled by default.

The existing serialized collector consumes this request. It rechecks the whole
selected package, current source, human ownership, original never-paid histories,
exact target language, deterministic policy, frozen processing contract and
combined original-envelope headroom before any
new revision. It invokes `Engine.accept_component_revision` for each exact entry.
Partial acceptance resumes only matching immutable original revisions. The
original manual admission/task histories are the receipt; no new campaign,
private execution ledger, funding account or retry allowance is created.
Gate-off requests remain paused. Held/invalid entries never fall back to broad
selection. Passing inspection still requires bounded generation and independent
review through the ordinary collector before any first publication.

### Versioned work bound and authentic evidence

The October 8, 2026 read-only check obtained all 18 complete anonymous provider
chapter envelopes for the 12 potential language entries. The literal English
partition validates. The initial source-association version 2 full preflight held
all 12 on `source_association_limit`: the many marked dialogue spans exceeded the
2,000,000-work bound. Historical version 2 evidence retains that behavior.

New Jacques inspections explicitly freeze `source_association_version="3"`.
This version indexes and reuses the same token structures and performs bounded
exact matching. It retains the 2,000,000-work ceiling and all cited-verse and
unclaimed-span checks; it does not omit dialogue spans or weaken the backstop.
The ordinary version 1 quotation policy and default component version 2
association policy remain unchanged. Evidence records a deterministic work audit
that must match on archived replay.

All 12 authentic archived evidence builds and exact replays passed with version
3 at 786,127 of 2,000,000 work units, including all seven cited checks and all
312 unclaimed-span/chapter checks. These results establish evidence support,
not translation quality or admission. The projected $0.537288 complete-cycle
ceiling per language remains subject to the original shared envelope, current
source, never-paid history and human-exclusion checks. No live queue request,
allocation, paid model call, activation or publication was made.

The read-only CLI was also exercised against translation main `656fa5b3` and
one clean current English checkout at `3e9673f0`, using those authentic archives.
It produced all 12 evidence-backed entries and zero holds, with empty selection,
`funding_allocated=false` and `publication_ready=false`. Repository State,
content and generated reports remained byte-for-byte unchanged. This was a
local read-only inspection; the new GitHub workflow was not dispatched.
