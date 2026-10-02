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

Once its separate standing policy is enabled, the existing serialized collector queues at most one recovery batch
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

Hourly downstream execution ships **disabled**, with a zero standing total USD
authorization. Preview is available without a provider. Before enabling hourly
work, the owner must approve and configure a separate total cap and per-campaign
envelope in `automatic_downstream_recovery`.

A manual **AI — Repair held translations** run from `main`, with preview unchecked
and an explicit positive budget, authorizes only that run's chosen USD ceiling,
models and at most five pairs. It can run while hourly recovery is disabled.
Trusted GitHub Actions `workflow_dispatch` context supplies the run ID, workflow
ref, repository and actor; no workflow input can manufacture this authorization.
The immutable queue request binds that provenance to all selected inputs. A
scheduled request cannot carry manual authorization. The collector checkpoints a
separate permanent manual envelope before creating any child task. It can finish
that bounded work without enabling the standing policy.

Rerunning the same workflow run is idempotent: its stable `gh-<run_id>` request ID
does not allocate or restart work again. Different inputs under the same ID are
rejected. A new manual run is a new explicit authorization, but cannot retry an
already-attempted article/language/source fingerprint. Legacy requests without
manual authorization remain bound to the shared policy; they are not migrated.

Both funding scopes retain nonempty accepted envelopes forever, even when unused,
failed, cancelled or partially staged. Empty selections cost/allocate nothing.
The status report shows standing allocations and historical manual allocations
separately. No existing parent or standing budget is raised, reset or recycled.
Each article/language/source fingerprint gets at most one downstream attempt
across both scopes, including cancelled/partially staged selections.

This intentionally does not turn a small envelope into guaranteed completion.
A later stage that cannot fit its conservative reservation stays held; preview
selection is free but not a quality test. Use a measured, owner-approved pilot to
choose a practical envelope before bulk activation. New reasoning-model caps are
32,768 translation and 8,192 review completion tokens, including reasoning.
See [model pricing assumptions](model-pricing.md) for cache-write and long-context
conservative cost bounds. Reported usage never frees reserved/allocated money.

Disabling the standing policy pauses its queued/prepared submissions; it does not
revoke a separately authorized manual envelope. Use normal campaign cancellation
to stop that manual campaign. Collection
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
model tampering, workflow entrypoints and cache/long-context pricing. The actual
manual Actions shell is also exercised offline through the real CLI/queue path,
with only GitHub HTTP transport stubbed, including the reported Luna/three-pair/$10
inputs, rerun idempotency, context rejection and bounded collector completion. Full unit
suite, repository validation and exact-commit CI must pass before ready status.
No paid run, activation, merge or deployment is part of this PR.
