# Bounded continuation of held translations

A failed downstream repair used to consume its only recovery opportunity for an
article/language/source fingerprint. A normal rerun is idempotent and cannot reset
that history. Version 2 requests instead create a new child of the exact latest
terminal task, using its full saved candidate, authoritative English and latest
substantiated findings. Every child still has one repair (or a typed candidate-less
fresh translation) and one independent review. Publication still requires all
HTML, metadata, reference and source checks and the 95/no-major-or-critical review.

## Finite lineage

New manual workflow requests and hourly requests freeze an explicit continuation
policy. Requests without it keep version 1 behavior, including old GitHub workflow
reruns: an absent workflow version variable does not change their queued inputs.

- A language/article/English **content fingerprint** gets at most three accepted
  downstream cycles across all manual and standing funding scopes. Existing
  accepted cycles count. Unrelated English repository commits do not reset it.
- Each applicable repair strategy gets at most two cycles. Its identity includes
  actual repair/review prompt bytes, selected API models/reasoning settings,
  language guidance, glossary, output ceilings and quality threshold. Pricing,
  unrelated model records, timestamps and run IDs cannot create a new strategy.
- Every accepted successor names the immediate predecessor and immutable task,
  candidate and source hashes. Forks, duplicate successors and active overlap are
  forbidden. Cancelled or partially staged selections still consume their cycle.
- Automatic successors wait at least an hour after the prior task finished. A
  separately requested manual cycle can run sooner. Oldest waiting terminal work
  is selected first across languages, so a repeatedly failing article cannot
  monopolize the queue.
- Under the same strategy, an unchanged or cyclic candidate stops. Repeated
  blocking source-quote evidence stops even if reviewer wording changes. Missing,
  ambiguous or nonliteral evidence does not establish progress and requires
  attention. Scores alone never prove progress. A material new strategy can get
  a bounded opportunity within the same total limit; it never resets it.

Provider refusals/filters, ambiguous legacy outcomes, uncertain submissions,
technical downstream review failures, owner cancellations, source changes and
public/human replacements remain explicit attention cases. This change does not
add review shopping, recover uncertain remote batches, or invent missing content.

The five failed Sol tasks from run `37109005676` retain their original prompts,
results and $10 manual envelope. Their first cycle counts. A newly authorized
version 2 request can select a bounded successor using PR13's materially changed
whole-article prompt. It does not restart the old campaign or spend its remainder.

## Complete-cycle funding

Every newly selected pair must fit the worst-case repair **and** independent review
reservation before either is submitted. The exact repair request is priced with
frozen models. Review uses its exact request skeleton plus a conservative bound
for the complete future candidate, including nested JSON escaping, output tokens,
cache-write pricing and the applicable long-context tier.

The default maximum complete canonical candidate is 120,000 UTF-8 JSON bytes,
including HTML and all metadata. A manual request can choose 1,024–1,000,000 bytes;
the entire review must still fit the selected model's context and approved budget.
An over-limit result is retained in full and held with an explicit reason. Text is
never shortened to meet this limit. Oversized or unfunded pairs are reported so
an owner can choose an appropriate bounded manual request.

The planned cycle ceilings are frozen separately from actual stage reservations.
Each stage must fit its own ceiling; actual prepared requests still increment the
existing campaign reservation exactly once. Reported savings never replenish
funds. New campaigns have independent permanent envelopes, and existing manual or
parent allocations cannot fund unrelated successors.

Hourly recovery remains disabled with a $0 standing cap. An explicit total cap is
required before activation. The existing $1 hourly envelope is unchanged and may
fit fewer than the maximum three pairs: with Sol and a 120 KB candidate ceiling,
conservative example cycles reserve about $0.53 for a tiny source, $0.96 for 25 KB,
and $1.31 for 120 KB. These are offline planning examples, not measured API bills.

## Progress and completion

`state/recovery-frontier.json` is a derived, non-authorizing report of unfinished
latest tasks. It records accepted/remaining cycle counts, processing status,
required complete-cycle reserve and the next action or blocking reason. STATUS
summarizes these categories. Escalated items remain visibly unfinished.

A historical campaign can be `finished` while its translations are still held.
Issue/language publication coverage remains separate. An issue is complete only
when all requested pairs have current-source-compatible public files; new articles
or languages do not silently expand an old paid request. Repository export
readiness does not establish live website deployment, which needs its own evidence.

## Acceptance checks

The implementation must preserve all legacy request bytes, reservations and
historical records; exercise first repair → failed review → bounded successor →
independent review → publication; and reject no-progress, third same-strategy,
fourth total, forks, replayed identities, policy/refusal/unknown states, budget
exhaustion and source/public/human races. Workflow-shell tests cover new versioned
requests, old-YAML reruns and explicit candidate ceilings. Cost tests prove the
review bound across escaping, Unicode, cache and long-context boundaries.

Offline tests validate orchestration and limits. They do not establish that a
model will find every defect or guarantee all articles will pass. Exhausted cases
require a specific intervention rather than an endless paid loop or lowered gates.
