# Bounded review reports and preserved negative evidence

The reviewer and the local validator must use the same response contract. New
campaigns freeze `review_contract_version: 2` at acceptance, including ordinary
translation, explicit improvement, and funded downstream recovery.
The version selects both the schema and the response instructions. It is included
in new downstream execution and strategy identities; it does not reset any
lineage, attempt count, budget or permanent allocation.

Version 2 requires a score from 0 through 100, a boolean `passed`, no more than
30 findings, and a boolean `findings_complete`. Each finding retains the exact
severity, location, source quote, translation quote and suggested-fix fields.
The API schema and the local parser impose the same limits. OpenAI documents
[`maxItems` as a supported Structured Outputs constraint](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas)
for the configured standard models; this constraint is not supported for
fine-tuned models, which are not configured here.

The reviewer examines the whole article before assigning its score. It lists
critical and major problems first and may combine repeated instances of the same
defect only while preserving every affected location and repairable evidence.
If its identified findings cannot fit, it must mark the report incomplete and
return a negative verdict. The completeness marker describes whether the report
contains what the model identified. It is not proof that the model found every
error.

An incomplete report always produces an explicit attention hold, even if the
model contradicts its instructions by returning `passed: true` and score 100.
The worker preserves every returned finding, does not queue a correction from
that report, and does not admit an automatic or manual downstream successor.
A changed strategy cannot bypass this attention condition. A complete, valid
negative review retains the existing bounded correction behavior. Ordinary approval still
requires score at least 95 (98 for an accepted-publication improvement), `passed: true`, a complete report, and no major or
critical finding, in addition to the independent structural and source gates.

## Frozen legacy requests

A campaign without the version field keeps its exact original schema and frozen
prompt. In particular, its historical API schema does not gain `maxItems` or the
new completeness field. Its local 30-finding acceptance limit remains unchanged.
Request bytes, input estimates, cycle reservations, execution hashes and strategy
identities must reproduce exactly. Existing tasks, results, decisions and batch
payloads are never rewritten to adopt version 2.

The mismatch surfaced in two Bengali Summer 2024 re-reviews at translation
revision `ba457a18dd147de4588329f8758e2ab36f988411`:

- [ALL NATURE SINGS review](https://github.com/trueChristian/berean-translation/blob/ba457a18dd147de4588329f8758e2ab36f988411/state/tasks/eac67cbcb51ae1e038a51c66d4802ec6/results/review1.json):
  score 32, negative verdict, 49 findings
- [Dealing with Cynicism review](https://github.com/trueChristian/berean-translation/blob/ba457a18dd147de4588329f8758e2ab36f988411/state/tasks/43cff43c105045d25904e1e43a91a84b/results/review1.json):
  score 27, negative verdict, 32 findings

Both completed normally and satisfy the individual finding field contract. Their
submitted schemas and prompts did not specify the array limit. All findings were
already preserved in the parsed results, raw attempts, task findings and terminal
decisions; the review-stage decision recorded a generic contract failure.

The status report now explains qualifying legacy overflows without changing that
history. It validates all finding fields within the existing response byte bound,
shows the negative verdict, score, count and original operational failure, and
links the complete parsed result and raw response. Malformed or positive overflow
reports are not classified as usable negative evidence. This is diagnostic
reporting only: no acceptance, correction, publication, retry or budget authority
comes from the display. Existing public versions retain their prior protection.

Offline regressions cover the 30/31 boundary, complete and incomplete verdicts,
malformed findings, both campaign paths and review stages, exact legacy replay,
unchanged historical records, preserved publications, and the absence of paid
correction or continuation after incomplete or legacy-overflow reports.
