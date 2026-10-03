# Downstream held-translation recovery

This adds a separate repair-first pipeline beside PR #7's exact, parent-budgeted
candidate re-review. Historic campaigns, reservations, source snapshots, prompts
and terminal tasks remain unchanged.

## Operation

New version 2 requests follow [bounded continuation](recovery-continuation.md).
The once-per-source descriptions below document historical version 1 requests;
new requests count those accepted attempts within a finite three-cycle lineage.

`AI — Repair held translations` offers manual model/reviewer choice, 1–5 article
pairs, a campaign USD envelope and a free preview (the default). The collector
selects the exact latest held pairs and freezes their predecessor task, candidate,
source and record hashes. Both roles default to `gpt-6.1-sol`; `gpt-6-astra` and
existing registered models are selectable. User-launched campaigns have confirmed
Luna and Sol API access; a selected model does not guarantee translation quality.

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

### Complete repair coverage

Newly accepted downstream campaigns freeze a dedicated repair system prompt:
the ordinary translation fidelity/HTML contract plus `prompts/repair.txt`.
Its instructions require a complete comparison of the candidate and metadata
against the English, including passages without a previous finding. Findings
and rejection reasons are non-exhaustive evidence to substantiate, not a claim
that the remaining text is correct. The repair should correct every substantive
source-backed defect while preserving faithful wording and avoiding stylistic
rewrites, doctrinal reinterpretation or unsupported reviewer suggestions. A
final comparison includes unchanged passages before returning the complete JSON.

This addresses the coverage problem observed in the owner's
[Sol repair run of October 3, 2026](https://github.com/trueChristian/berean-translation/actions/runs/37109005676).
All five responses were structurally valid and fixed listed problems, but final
review scores were 65–76 and each still contained at least one pre-existing
meaning defect outside the supplied correction instructions:

| Article | Printed English | Surviving candidate error |
| --- | --- | --- |
| Mennonites | land patent | `boupermit` (building permit) |
| Road to Emmaus | breaking of the day | `krag van die dag` (power of the day) |
| A Call to Holiness | white linen garment | `wit serwelingsaatklere` (garbled wording) |
| The Sanctified Home | thine they were | `U was van hulle` (reversed belonging) |
| The Foolishness of Preaching | followers | `nageslag` (offspring) |

The immutable [campaign and task links](https://github.com/trueChristian/berean-translation/blob/9fec314dfc71daaea23b212398585ade99e05958/state/campaigns/gh-37109005676.json)
retain original candidates, corrected candidates and final findings. The full
English and correct candidate were present in the submitted requests; this was
not a missing-context, stale-input or truncated-response failure. Some reviewer
suggestions were themselves imperfect, so instructions still require source
substantiation instead of blindly applying every proposed wording change.

Only a new downstream `correct` request uses the dedicated frozen prompt.
Ordinary corrections, candidate-less fresh translation and independent reviews
retain their existing prompts. Historical campaigns without the new prompt key
reproduce their original request bytes and reservation bounds. The new prompt
bytes are included in the existing conservative reservation, with no increase
to the approved envelope or attempt limits. The independent reviewer still sees
only the full English and corrected candidate, without prior findings or repair
claims. Existing holds are unchanged and are not made eligible for another
downstream attempt by this prompt update.

Offline tests verify request scope, freezing, bounds and review isolation. They
do not establish a quality or acceptance-rate improvement; that requires a
separately authorized observed run on eligible work.

A newly recorded non-policy failure with no usable candidate can take a bounded
fresh-translation → independent-review path. It gets no extra correction loop.
Refusals, content filters, legacy unclassified provider errors, and historic
ambiguous/truncated responses remain held for owner attention. The twelve current
ambiguous legacy outcomes are not silently declared safe to retry. No candidate
is invented, and no model or prompt switch is used to bypass a provider refusal.

## Spending and activation

The owner approved a **$10 total lifetime automatic-recovery ceiling** on October
3, 2026, separate from manual runs. This branch configures hourly recovery enabled
with that cap and the unchanged $1 per-campaign envelope. It becomes effective
only after the configuration reaches trusted main; preparing the PR does not
activate or dispatch work. Preview remains free. Permanent allocations stop new
hourly campaigns at the approved total and are never reset or recycled.

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
