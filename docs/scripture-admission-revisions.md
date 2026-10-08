# Append-only offline revisions of never-paid admission holds

This module prepares **offline revision proposals**, not live admissions. It
never changes repository State, an old request/campaign/attention event, paid
history, an accepted publication or a funding allocation. The live component-v2
admission, submission and publication gates remain closed. Completed-publication
updates are explicitly outside this stage.

## Narrow eligible scope

`inspect_never_paid_hold(engine, campaign_id, entry_id)` checks the actual manual
admission ledger and its existing frozen-contract validator. The original request,
selection, campaign models/prices, source and provenance must still match.
Only an uncancelled ordinary translation attention entry is supported. There must
be no prepared evidence fields, previous task, saved review candidate, publication
or pair history; the original task template must be queued with zero attempts.

The proof checks the whole task directory (including orphan artifacts), campaign
task inventory, all existing tasks of that pair and all batch references. Batch
IDs must resolve to task records with matching campaigns; duplicate or malformed
inventory is held. A missing task.json alone is not proof of no paid work. The
existing source-fingerprint, human/working-file, record and overlap guards are
reused. Unrelated English repository revisions do not reset a content fingerprint.

Original batch reservations must reconcile exactly with campaign.reserved_usd.
A batch written before its campaign counter was updated is an unresolved crash
state, not spare funding. Valid human-exclusion partition children retain proven
reciprocal unsubmitted links and strictly smaller task sets; their inherited
reservation is counted only once. Missing or inconsistent links remain held.
Reported usage cannot reduce a reservation.

The original pinned campaign gh-37439892859 was inspected read-only: all 27
attention entries passed this narrow never-paid guard. That does not establish
current live eligibility, complete new Scripture evidence, or permission to
release a hold. No revision was written for any real entry.

## Private journal API

`OfflineAdmissionRevisionStore(root, state_root)` requires a dedicated journal
outside the live repository, with neither root an ancestor of the other.

- `propose(engine, campaign_id, entry_id, scripture_policy, evidence, protocol_root)`
  validates fresh source-bound v2 evidence and freezes an immutable successor
  proposal linked to the exact original attention event. It retains the original
  immutable campaign contract and task template as separately verified anchors.
- `prepare(engine, revision_id)` rechecks current cancellation, source, human,
  record/task and funding observations and appends an offline-prepared event.
  It returns an `OfflineComponentCycle`, never a materialized runtime task.
- `cancel(revision_id, reason)` appends a private cancellation event. It does not
  change the original campaign and does not refund any projected allocation.
- `inspect()` returns the journal head, immutable events, derived proposal states
  and projected allocations. These are historical offline observations, not
  live eligibility or monetary authority.

Mutations optionally take an expected journal head for compare-and-swap.
Identical proposal/prepare/cancel retries do not append or allocate twice.
A stale expected head is accepted only for an idempotent result when that head
is a retained ancestor; a missing retained head (including a deleted cancellation
tail) still fails closed.
A different successor for an already claimed original entry is held. Cancelled
revisions cannot be automatically reopened or used to recycle headroom.

The new task/campaign objects are private execution projections only. They are
never saved into State. Full original funding/model/task anchors are checked
again when reading the journal, so a self-consistently rehashed cycle cannot
substitute a cheaper model, larger output cap, changed language or new prompt
base while claiming the original funding contract.

## Complete-cycle funding projection

The projection conservatively covers two generation requests and two review
requests under the original frozen models, prices and output limits. Each stage
uses the maximum admissible model context, checking pricing at both sides of any
long-context boundary. This remains safe even when a valid positive multiplier
is a discount. Actual request construction independently rejects context overflow.

All projected revisions for one original campaign share its original remaining
manual envelope. Existing durable reservations and reported usage are retained;
there is no new $30 envelope, raised cap, price migration or recycled attempt.
The private journal's projections remain charged after cancellation. They are
planning ceilings only: funding_authorized is false and no real reservation is
created or released.

## Crash, concurrency and path safety

One POSIX flock serializes the private journal. Canonical event files form an
ordered hash chain, with no mutable head file. The writer fsyncs the complete
temporary event, links it exclusively into place, then fsyncs the event directory.
Directory creation parents are fsynced too. Uncommitted temporary files can be
ignored; a committed event is replayed rather than rewritten.

Root, event-directory and lock identities are checked. Operations use pinned
directory file descriptors, no-follow opens and nonblocking event reads. Replaced
symlinks, locks, FIFO races, gaps, forks, malformed/rehashed events, or size-bound
violations fail closed. The journal cannot be redirected into live State through
an ancestor path or post-construction directory replacement.

Tests inject failure before and after durable commit, replay retries, and race
both threads and independent processes for the same original envelope. They
exercise source, human, cancellation and budget changes at recheck/commit points.
These are executable interruption tests, not a hardware power-loss certification.

## Important limits before live use

Use a stable/read-only State view for offline preparation. The journal lock does
not serialize unrelated writers to repository State. Rechecks before/after an
event and before returning a prepared cycle catch observed drift, but cannot
promise atomic live admission against an uncooperative State writer. A returned
cycle is only an unfunded snapshot; later State changes do not authorize it.

A hash chain alone cannot detect deletion of its entire tail without an
independently retained trusted head. Retain the head/checkpoint externally.
Different private journal copies do not create separate funding authority.
A future live implementation needs one authoritative collector/ledger and an
externally anchored append-only checkpoint, coupled to the existing State and
provider submission invariants.

Still required: actual versioned admission materialization, durable real budget
reservation, upload/unknown-create/result handling, independently accepted-review
records, and before-publication drift/human checks. A separate completed-publication
update path must preserve exact predecessor identity and last-good publication.
Do not remove live v2 gates on the strength of an offline-prepared event.
Portuguese inflection adaptation remains held; no new editorial decision was
needed for this stage's supported cases.
