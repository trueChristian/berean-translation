# Automatic archive translation

The hourly discovery workflow reads current English `main` and queues missing
and changed work across every article and all configured languages. The collector
requests runs every 15 minutes as a cron fallback, polls provider batches, and advances saved translations
through independent review and bounded correction. No manual “next” dispatch is
needed. Active collection windows poll already submitted work between those runs. Turn off repository Actions to stop starting
work. Batches already accepted by OpenAI can still finish there.

`page_size` and `max_active_tasks` bound one worker's queue/concurrency. They are
not completion limits. Later ticks select the next eligible pairs. New translations and saved-candidate recovery have separate scheduling
allocations so recovery cannot consume all admissions. A held final-review stage
resumes that review; a saved correction input resumes correction and review.
Candidates and historical terminal tasks are never erased or rewritten.

## Provider-managed spending

On October 29, 2026 the owner removed all repository monetary limits. Automatic
translation, source refresh, saved-candidate recovery and manual campaigns have
no per-run, per-stage, per-cycle, monthly or cumulative dollar ceiling. This also
applies to already accepted work. Owners manage spending at the AI provider and
can disable Actions or cancel campaigns; batches already submitted may finish.

Before paid processing, the worker checkpoints
`state/policies/provider-managed-spending.json`. Historical
`state/automatic-budget.json`, campaign limits, allocations and settlements remain
immutable audit records. New campaigns carry the provider-managed policy identity;
no historical dollar limit is treated as a live balance. Old budget-only holds
remain on disk but are reported as pending admission and reconsidered normally.

The planner records complete-stage cost estimates and technical candidate,
findings, context and output bounds. Only the technical limits restrict processing.
Prepared requests keep their exact payload and unique submission identity.
Reported costs exceeding estimates do not stop the worker. Usage evidence and
settlement records are informational, priced at frozen rates, not invoice
reconciliation. Missing usage or uncertain submissions are never reported as
proof of no charge, and unknown batches are never blindly resubmitted.

## Quality and attention

Publication still requires 95/100, no major/critical findings, valid complete
HTML and metadata, unchanged English source, and no human intervention. Human
editorial authority permanently excludes that pair from every later AI path.
Refusals, content filters and ambiguous historical outcomes remain attention
holds. Unknown provider submissions are reconciled by submission key, never
blindly recreated.

Version-3 automatic continuations may proceed beyond the former three-cycle
limit with measurable progress.
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
Manual requests have no budget input and default to free previews. Ordinary translation uses 95;
optional replacement of an accepted AI publication uses 98 and retains the
previous public version until an accepted improvement exists. Collector
maintenance retains cancellation and explicit reconciliation of a provider batch
confirmed absent. See [current operation](../README.md#workflows).

All tests simulate provider results offline and do not establish live translation
quality or grant further spending authority.

Successful validated collection also explicitly dispatches the next cycle while actionable work remains. The collector can request discovery during continued processing, so its progress does not rely solely on scheduled-event delivery. This continuation is controlled by `automatic_collection_continuation`; see [the runtime contract](runtime-contract.md#automatic-collection-continuation).
