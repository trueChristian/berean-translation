# Downstream held-translation recovery

This adds a separate repair-first pipeline beside PR #7's exact, parent-budgeted
candidate re-review. Historic campaigns, reservations, source snapshots, prompts
and terminal tasks remain unchanged.

## Operation

`AI — Repair held translations` offers manual model/reviewer choice, 1–5 article
pairs, a campaign USD envelope and a free preview (the default). The collector
selects the exact latest held pairs and freezes their predecessor task, candidate,
source and record hashes. Both roles default to `gpt-6.1-sol`; `gpt-6-astra` and
existing registered models are selectable. Actual project model access and
translation quality have **not** been verified by paid calls.

Once enabled, the existing serialized collector queues at most one recovery batch
per UTC hour. GitHub schedules are best effort; this is not an on-the-hour SLA.
The default small batch is three pairs. Manual enqueuers write unique immutable
requests and never mutate runtime records themselves. Automatic source refresh
retains its existing independent policy and mini model.

The path is one repair followed by a separate independent review. Repair input is
full authoritative English, latest candidate, rejection reason and substantiated
findings. The reviewer sees English and the resulting translation, without prior
findings or repair claims. Attempt ordinals remain outside model input. Faithful
translation, target-language quotations, the author's theology, all structural
contracts and the 95/no-major-or-critical gate remain mandatory.

A newly recorded non-policy failure with no usable candidate can take a bounded
fresh-translation → independent-review path. It gets no extra correction loop.
Refusals, content filters, legacy unclassified provider errors, and historic
ambiguous/truncated responses remain held for owner attention. The twelve current
ambiguous legacy outcomes are not silently declared safe to retry. No candidate
is invented, and no model or prompt switch is used to bypass a provider refusal.

## Spending and activation

Paid downstream execution ships **disabled**, with a zero total USD authorization.
Preview is available without a provider. Before enabling, the owner must approve
and configure a separate total cap and per-campaign envelope in
`automatic_downstream_recovery`. The total cap covers manual and hourly work
combined. Nonempty accepted envelopes count forever, even when unused, failed,
cancelled or partially staged. Empty selections cost/allocate nothing. A manual
choice cannot increase the approved total. No existing parent budget is raised,
reset or recycled. Each article/language/source fingerprint gets at most one
downstream attempt, including cancelled/partially staged selections.

This intentionally does not turn a small envelope into guaranteed completion.
A later stage that cannot fit its conservative reservation stays held; preview
selection is free but not a quality test. Use a measured, owner-approved pilot to
choose a practical envelope before bulk activation. New reasoning-model caps are
32,768 translation and 8,192 review completion tokens, including reasoning.
See [model pricing assumptions](model-pricing.md) for cache-write and long-context
conservative cost bounds. Reported usage never frees reserved/allocated money.

Disabling the policy pauses queued/prepared downstream submissions. Collection
and reconciliation of work already submitted remain allowed. Source fingerprints
are checked against the collector's coherent current English scan at selection,
preparation, resumed submission and publication. Changed, public or human-reviewed
replacements cannot be overwritten.

## Audit and interrupted work

Exact inputs remain in immutable batch JSONL. Successful structured results remain
in per-stage result records. New attempt records additionally retain allowlisted
finish reason, refusal, provider diagnostic, actual model, usage and bounded raw
returned content, including parse failures. No headers, secrets or SDK exception
bodies are logged. Archived content is hashed; oversized evidence is explicitly
marked truncated. Terminal replay cannot rewrite an old failure.

Acceptance checkpoints the permanent envelope before staging children. A staging
failure blocks paid work. Use the normal collector `cancel` operation for that
campaign to reconcile all deterministic partial children and preserve their
cancelled evidence. It does not reset eligibility or free money. Uncertain batch
creation still uses the existing submission-key reconciliation and never blind
resubmission. Same-file Git conflicts remain blockers, with no force pushes.

## Verification

Offline regression coverage exercises repair/review/publication, quality failures,
fresh fallback, truncation/refusal classification, immutable history, current-source
checks, public/human protection, duplicate requests, UTC-hour deduplication,
cumulative and stage budgets, disabled execution, interrupted staging/cancellation,
model tampering, workflow entrypoints and cache/long-context pricing. Full unit
suite, repository validation and exact-commit CI must pass before ready status.
No paid run, activation, merge or deployment is part of this PR.
