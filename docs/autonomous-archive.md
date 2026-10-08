# Automatic archive translation

The hourly discovery workflow reads current English `main` and queues missing
and changed work across every article and all configured languages. The collector
runs every 15 minutes, polls provider batches, and advances saved translations
through independent review and bounded correction. No manual “next” dispatch is
needed. Active collection windows poll already submitted work between those runs. Turn off repository Actions to stop starting
work. Batches already accepted by OpenAI can still finish there.

`page_size` and `max_active_tasks` bound one worker's queue/concurrency. They are
not completion limits. Later ticks select the next eligible pairs. New translations and saved-candidate recovery have separate scheduling
allocations so recovery cannot consume all admissions. A held final-review stage
resumes that review; a saved correction input resumes correction and review.
Candidates and historical terminal tasks are never erased or rewritten.

## Spending authority

`state/automatic-budget.json` records the October 4 authorization once. The
automatic cap is $30 cumulative, with no renewal. Existing accepted shared
recovery allocations (currently $6 in production) form an explicit immutable
baseline. Existing accepted source-refresh campaigns retain their separate
original authority. Existing manual allocations retain their
original caps, provenance and non-recyclable history.

All newly automatic translation, source refresh and recovery uses the same
budget authority. Before any billable request, the worker freezes the complete
remaining stage chain: translation, review, possible correction, and final
review, or only the stages still needed by a saved candidate. The envelope is
that calculated ceiling, at most $10; a task needing $1.03 reserves $1.03, not
$10. The planner bounds complete canonical candidate and review-finding bytes,
nested JSON escaping, model context, cache-write rates, output limits and long
context pricing. Oversized results are retained and held, never truncated.

New automatic reservations can settle after every potentially billable request
has a known terminal result with complete usage counters. Immutable settlement
events record the evidence and provider-reported usage priced at the campaign's
frozen rates. Only proven unused reservation is released. This is not invoice
reconciliation. Missing responses, partial usage, uncertain submissions and
incomplete collection retain the entire envelope. Cancellation does not by
itself establish zero cost. Settlement never changes old manual/legacy ledgers.

Each new admission records exactly which earlier settlements it used. Allocation
indices, evidence hashes, prices and complete stage plans are checked again
before submission. The same serialized state writer handles admission and
settlement, so concurrent manual dispatches cannot independently spend automatic
headroom. Budget-blocked automatic requests stay pending with their exact IDs;
known settlement may let the next tick resume them. Raising a live setting does
not replace the frozen $30 authority or silently create a new budget period.

## Quality and attention

Publication still requires 95/100, no major/critical findings, valid complete
HTML and metadata, unchanged English source, and no human intervention. Human
editorial authority permanently excludes that pair from every later AI path.
Refusals, content filters and ambiguous historical outcomes remain attention
holds. Unknown provider submissions are reconciled by submission key, never
blindly recreated.

Version-3 automatic continuations may proceed beyond the former three-cycle
limit only within the same finite authority and with measurable progress.
Accepted history still counts for audit. Repeated candidate content, recurring
substantiated findings, ambiguous progress, repeated technical failures and
policy refusals stop for attention. Successors wait at least one hour. Every
cycle is a new exact child of the latest predecessor; it cannot reset or fork
history. Existing version-1/version-2 requests retain their frozen semantics.

## Workflows

The five active workflows are manual translation, hourly English discovery,
result collection/continuation, optional improvement review, and offline CI.
Specialized repair and Scripture workflows, their executable archives, and
Scripture dependencies have been removed; Git history preserves the old code.
Manual defaults remain $30 with free previews. Ordinary translation uses 95;
optional replacement of an accepted AI publication uses 98 and retains the
previous public version until an accepted improvement exists. Collector
maintenance retains cancellation and explicit reconciliation of a provider batch
confirmed absent. See [current operation](../README.md#workflows).

All tests simulate provider results offline and do not establish live translation
quality or grant further spending authority.
