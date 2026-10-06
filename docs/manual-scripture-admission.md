# Resuming manual Scripture prefetch

An ordinary manual translate or review request can exhaust the collector window
while fetching read-only Scripture evidence. That deadline is not a completed
translation attempt. The collector resumes the exact selected target under the
same campaign and deterministic task ID on a later run. It never selects `next`
again, creates another manual envelope, resets an attempt, or uses automatic
funding to complete admission.

## Durable authority and progress

`state/manual-admissions/<campaign>.json` is a separate version-1 admission and
resolution ledger. Existing campaign `selection` and `skipped` rows remain audit
history, including original deadline skips. The original queue request is never
changed. Each target binds its original English snapshot, record/publication
baseline and previous task hash before evidence collection. New review requests
also freeze a hash-named candidate artifact. Model settings, prices, prompts,
language settings, review contract and Scripture policy come from the original
campaign; current configuration cannot replace them.

Entries move from `pending` through `ready` to `admitted`, or to explicit
`attention` or `cancelled` outcomes. Only `prefetch_wait_budget` is automatically
resumable after an evidence failure. Missing editions, ambiguous quotation
scope, fetch failures, refusals, quality failures and content filters are not
converted to temporary deferrals. Pending admission is reported as unfinished
in campaign status, `STATUS.md` and the collector heartbeat, even when the
campaign has zero tasks. Completed ledgers do not consume the queue's bounded
acceptance page.

Resumption checks the current source content fingerprint, human protection,
publication/record identity, latest predecessor and other active tasks. An
unrelated English repository revision is allowed when the content fingerprint
is unchanged, but evidence always uses the original hash-verified snapshot.
Changed source or human work is never substituted or overwritten.

Pending and ready targets exclusively cover their exact pair and source
fingerprint during manual and automatic selection. Same-source permanent
attention prevents automatic funding from bypassing the hold, while a later
explicit manual request may proceed. Once that later request produces a task,
its normal saved-stage recovery policy applies. The resumption path itself
never allocates automatic funds or changes the original manual cap.

## Attention diagnostics

When resumption newly observes a substantive `ScriptureAttention`, its existing
entry receives one optional `attention_detail` record. This retains the reason's
reference/quotation detail even when the campaign already contains an original
deadline skip. It is supplemental diagnostic data, never a request, instruction,
provider response archive, retry allowance or replacement for frozen evidence.

The record contains `text`, the existing `provenance_sha256`, an `event_index`
and `event_sha256` pointing to the exact attention transition, and a `sha256`
checksum of those four fields. The provenance binds the campaign, target and
original source snapshot; the event binds the reason and observation time.
Validation rejects malformed records, changed checksums, mismatched provenance,
non-attention events and a text prefix inconsistent with the recorded reason.
These hashes detect inconsistent state; they do not authenticate against an
actor who can rewrite the entire ledger.

Text is limited to 2,048 UTF-8 bytes with a visible truncation marker when needed.
Visible Unicode is retained; control characters, invisible formatting characters
and `<`, `>` and `&` are escaped into printable text. It remains untrusted data;
consumers must not execute it, interpret it as markup or use it as model
instructions. Newly added campaign skip detail uses the same bound. Existing
skip rows and events are retained unchanged, including their original detail.
Repeated deadlines add no diagnostic records. A held entry is not retried, so
replay cannot replace its detail or grow history. Cancellation retains the
diagnostic and appends only the existing cancellation transition.

This is an additive version-1 extension with no migration or backfill. Existing
ledgers without the optional record remain readable, and already-held entries
stay held without fetching evidence again. Missing historical provider detail
cannot be reconstructed by this change. A previously pending entry gains detail
only when a subsequent authorized resumption actually observes a new substantive
failure. Original request bodies, selection, funding, claims and attempt policy
are unchanged.

## Legacy translate requests

A pre-ledger campaign is considered only when its original skipped history
contains the exact `prefetch_wait_budget` reason. Reconstruction requires the
original request hash, matching explicit request authority, and exactly one
hash-valid snapshot for the selected article at the original source revision.
A missing or ambiguous snapshot stays in attention; the current English source
is never a fallback. Existing record history must prove the baseline predates
the original campaign. Missing predecessors, invalid timestamps and events in
the same second as acceptance are conservatively unproven.

Legacy review deferrals remain in attention because their original candidate
binding cannot be reconstructed safely. New review requests capture that proof
before their first prefetch, so they can resume normally. Source-refresh,
exact-recovery, downstream-repair and automatic campaigns keep their existing
admission protocols.

## Crash, cancellation and cost behavior

The full initial ledger is checkpointed before evidence prefetch. Per-target
local writes preserve ready evidence, task, record and campaign materialization;
the ordinary queue acceptance checkpoint persists the page. Existing reservation,
payload, input-file and submission-intent checkpoints still precede billable
work. Partial admission tasks cannot be prepared. A crash between initial ledger
and campaign writes restores the original frozen campaign before discovery or
billing. Replay never rewrites a completed task or duplicates requested history.
Operational write/checkpoint failures propagate instead of becoming permanent
evidence holds.

Cancellation retains exact partially materialized children and their history,
marks unresolved entries cancelled and prevents admission from restarting.
Preparation and submission validate frozen manual authority, including live
task models and source identity, before upload or provider creation. A tight
original cap produces the normal `budget_blocked` task outcome without a hidden
fallback inside admission. Later autonomous recovery retains its separately
authorized existing policy.

The ledger is limited to the original target set, six transitions per target
and 16 MiB. Model registries are stored once in the initial campaign copy, not
per target, and review candidate bytes live in separate hash-named artifacts.
Claim indexes are built once per selection pass rather than rereading campaigns
for every archive/language pair.

`tests/test_manual_admission.py` exercises real fake-clock expiry, fresh-engine
replay, frozen configuration, mixed accepted/deferred work, legacy proof,
source/human/concurrent changes, cancellation, partial writes, malformed history
and budget tampering. Diagnostic regressions also cover a legacy deadline
followed by a substantive failure, fresh and existing attention, replay,
Unicode/control/oversized data, detached or malformed detail, and preservation
of original history and cancellation behavior. All fixtures are temporary and
all network calls are blocked. These tests do not migrate production state or
run paid requests.
