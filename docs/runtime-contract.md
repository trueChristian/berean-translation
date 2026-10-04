# Translation runtime contract 1.0

The English archive, translations, and website are independent repositories. A translation is identified by `(language, English article UUID)`. No image or category catalogue is duplicated. Language selection uses three-letter folders while website routing uses the registry's language tags; the website owns navigation, category translations, flags/icons, search and SEO markup.

## Publication

`content/<language>/articles/<uuid>.html` is the translated source fragment followed by one independent AI-notice aside. Its adjacent JSON file has translated title/subtitle/section. The HTML article itself has the same UUID, element structure and immutable attributes as the source. Alt/title attributes may be translated; URLs, IDs, classes and meaningful element structure may not.

Clock-notation exemptions require an explicit source AM/PM time and one-to-one equivalent, clearly marked target times in the same HTML block. Bare or ambiguous colon expressions and ranges remain protected. Errors identify the first differing HTML path/signature or missing/extra chapter/verse values. This deterministic check supplements, rather than replaces, the semantic review. Prompt version 1.0.2 separates context-only bylines from translatable fields; historical campaigns retain their frozen prompt and request contract. A failed old campaign needs a new explicitly authorized retry/review to use the new prompts, never an edited history or reset attempt counter.

Prompt version 1.0.3 clarifies that quoted Scripture and other substantive quoted prose must be translated faithfully from the printed English into the target language, without substituting another Bible version or restoring English as a fidelity workaround. Localize Scripture book names while retaining exact chapter/verse numbers, ranges, reference counts and placement. Legitimately unchanged proper names, established titles of cited works and identifiers are not automatically translation failures. Corrections reject only invalid English-verbatim demands, while still addressing substantiated fidelity findings: an invalid suggested fix does not invalidate a genuine underlying semantic defect, which must still be repaired faithfully in the target language. New campaigns freeze these instructions for translation, both reviews and the bounded correction; existing campaigns, including 1.0.2, keep their original frozen prompts and are never migrated. The 95-point threshold, major/critical holds, attempt limits and spending caps are unchanged. Artificial offline prompt/request regressions verify packaging and immutability, not live model compliance or theological translation quality.

`index.json` describes public translation records. `human_reviewed: false` is not a publication blocker. `status: ready` means the last accepted translation matches the observed English translation fingerprint; human-reviewed and AI-unreviewed articles are both allowed. An already accepted translation remains exported when its English source changes or disappears, with status `stale` or `source_removed` and its original source provenance. Source revision, actual translation/reviewer models and output fingerprints remain traceable.

Failed or pending candidates live under `state/tasks`, not public `content`. A failed re-review never removes an existing good version. Human-reviewed article/language pairs are permanently excluded from every new AI request, including manual review and forced retries. Already-submitted suggestions cannot replace them. These proposals include full candidate JSON and findings; applying changes to the existing reviewed HTML/metadata is a normal human commit, without adding an AI notice or silently changing provenance.

Human editorial authority is recorded after a committed human change to an existing publication, independent of the visible notice. Prose, structure, reference numbers and notice wording are not compared with AI quality rules. Git history supplies attribution; this records a collaborator's editorial action, not independent certification of linguistic accuracy. The article body remains verbatim; the system regenerates a localized human-first presentation notice without model/version or reviewer identity, retaining the English-authority link. An unreadable or technically unsafe edit is isolated per article, with its observed file hashes and reason recorded; a hash-verified accepted snapshot supplies the public payload until the human commits a repair. Legacy accepted copies are recovered from saved task candidates or independently hash-matched Git history. Snapshots are immutable and never spending authority.

Human exports add `human_edit` (commit, author, email, time) and `notice_present` to the existing `human_reviewed: true` / `ai_notice_required: false` flags. The generated footer uses `data-translation-notice="human-reviewed"`; input notice text and marker count never establish authority. `images` describes the accepted human HTML, not English image parity. An isolated edit adds `pending_edit` with a reason and `serving_last_accepted: true`; hashes refer to the actual accepted exported payload. AI-only publications retain their strict gates. Stale/source-removed entries carry the retained-publication contract below; the website no-loss guard remains the final backstop against dropping any already-served pair.

## State and durability

1. Manual Actions workflows persist unique immutable queue requests, without a shared enqueuer concurrency group. The single collector may also persist exact, deterministic requests under the enabled owner-authorized source-refresh policy.
2. The collector checks out current `main`, synchronizes human review, resolves upstream `main` to a commit, reads the eligible IDs from `index.json`, and computes article fingerprints itself from current English HTML and relevant metadata. A sparse checkout reads current English `main` once; missing/stale core manifests do not matter.
3. It resolves issue selectors from that snapshot, skips processed/active/protected combinations, freezes model settings/prompts/glossaries, and creates durable tasks and hash-named source snapshots.
4. For each stage it prepares single-model JSONL batches, reserves conservative token-cost ceilings, and pushes the reservation before calling OpenAI.
5. It persists the uploaded file ID, then submission intent, then calls Batch creation once with retries disabled. A lost response remains `submission_unknown`; subsequent ticks search Batch metadata instead of making another paid request.
6. Further ticks within the bounded active polling window, or later collector runs, download completed output/error files and match every result by `custom_id`, never line order. Partial expiry preserves successful items; missing items fail closed without automatic billed retries. A polling window reuses the initial coherent source scan.
7. A passed review publishes immediately. A failed first review allows one correction and final review. A final failure remains `not_ready`.

Every checkpoint refreshes the publication index/status in the same commit. Batch work can run concurrently at OpenAI; repository mutation is serialized. An enqueuer or human may move main during a worker checkpoint. Disjoint changes are rebased without force; overlapping file edits halt safely. An in-progress request with a lost remote ID can be recovered through its persistent submission key even when an artifact is unavailable.

## Limits and expenditure

The configured model list is an explicit allowlist of dated model snapshots and Batch token rates. The default is a cost-conscious `gpt-4.1-mini` candidate, not a claim that it is the cheapest model meeting a proven translation-quality benchmark. Lower-cost `gpt-4.1-nano`, `gpt-5-mini` and higher-capability `gpt-4.1` choices remain selectable. The list is deliberately not an unvalidated automatic model-discovery mechanism.

A campaign records its USD cap, conservative reserved cost and API-reported token-usage estimate. Input estimates use UTF-8 bytes plus a framing allowance; output tokens are explicitly limited. Reservations are never silently released and reused. A subsequent stage that cannot fit the remaining cap becomes `budget_blocked` and does not call OpenAI. Selecting a very large batch with too small a budget can therefore stop after translation but before review; the saved candidate can be selected in **AI — Review** without paying to translate it from scratch. Caps are application safeguards based on the configured rate table, not a provider billing guarantee. Configure OpenAI project/account budgets and alerts as an independent safeguard, verify whether they impose a hard cap rather than merely notifying, and review rates before a large campaign.

No more than two translations/corrections and two reviews occur per task. No billable endpoint is automatically retried. File uploads have a separate three-failure bound. Manual re-review/retry is a new, explicitly authorized bounded campaign. Discovery never authorizes a first translation of an article/language pair. The separately approved automatic source-refresh policy applies only to already-published AI pairs whose current source fingerprint changed: gpt-5-mini for both stages, at most $10 per one-issue/language campaign, at most five new campaigns per initial scan, exact source selections and all-history fingerprint deduplication. These caps are per campaign, not an aggregate lifetime/daily spending cap. Failed/cancelled/budget-blocked fingerprints require manual intervention; active, human-reviewed, withdrawn and compatible work is excluded. Immutable requests and frozen campaign policy/usage provide the spending ledger. Disabling the policy pauses unaccepted automatic requests; already-accepted work needs explicit cancellation.

## Exact manual candidate recovery

The dedicated recovery workflow emits review-only immutable requests containing
`recovery_of_campaign` and a nonempty unique `previous_task_ids` allowlist. There
are no language/issue fallback selectors; combining selection families fails
closed. Explicit budget (at most six USD decimal places) and dry-run fields are required. A target must exist once,
belong to the finished original campaign, be the latest terminal `not_ready` task,
have a saved candidate and hash-verified source/model provenance, match current
source, and have no public/human-reviewed replacement or overlapping active work.
The entire selection is rejected if any target fails. No automatic hold filter,
new language detector, or semantic acceptance rule is introduced.

Accepted campaigns record `recovery_allocation_usd` equal to the full budget,
`previous_task_ids`, immutable request snapshot/hash, frozen selection/provenance and `recovery_budget`. The accepted envelope is audited against that snapshot and its queue request when present.
For each original campaign, its reserved ceiling plus **all** accepted recovery
envelopes must remain within its original cap. Allocation history is additive:
finished, failed, cancelled, partially accepted and unused allocations still count.
Neither actual usage nor cancellation returns headroom. Deduplication scans the
same all-history ledger, not merely current latest tasks or active campaigns.
The single serialized collector owns acceptance, so requests enqueued concurrently
cannot reserve the same headroom. The original campaign is not rewritten.

Dry runs record a selection report only, with zero allocation and no tasks or
stage reservations. Their remaining-after amount is hypothetical. Replaying an
identical request returns its prior report/campaign; changed inputs with the same
identity are rejected. A paid request needs a new identity after a preview.
Acceptance durably checkpoints the full envelope before materializing tasks and marks
`recovery_acceptance_complete` only after all selected tasks exist. Interrupted
acceptance is a diagnostic blocker, never permission to submit partial work or
recycle the allocation.

An owner can explicitly abort interrupted acceptance through the existing
`cancel` operation. Preflight freezes/checks the original record snapshot as well
as task/candidate/source hashes and enumerates only deterministic children derived
from the campaign/language/article identities. It rejects unexpected child paths,
results, batch records, reservations, attempts, model events, candidate changes or
foreign record/history evidence before writing anything. This includes candidate
files created before task records and task records created before record/history
or campaign-list updates.

A durable `acceptance_aborting` journal records a timestamp and exact partitions:
materialized tasks, candidate-only artifacts, and entirely unstaged child IDs.
Repeated explicit cancellation can finish that same local-only abort idempotently.
A terminal retry also verifies that origin contains its checkpoint; a locally
committed but failed final push requires another explicit cancel from a fresh
main checkout, retaining the failed checkout for audit. It cannot report a durable
abort based solely on an empty local diff.
Materialized children receive one cancelled status/history entry; candidate-only
artifacts remain untouched; no missing task is created. Terminal
`acceptance_aborted` keeps `recovery_acceptance_complete=false`, the full immutable
request/selection/allocation, and a task list containing only materialized children.
The validator checks this terminal schema, immutable original history prefix,
cancellation evidence and retained artifacts. Legitimate later work may extend
article records without invalidating the abort. All planned previous IDs stay
consumed and the entire envelope remains allocated, even when no task existed.
An incomplete/aborting campaign cannot submit recovery work; an explicit terminal
abort restores valid repository state without rewriting old tasks or deleting
history. Any evidence of already-submitted work is a blocker for this specialized
abort rather than something to conceal or relabel.

Each new task starts at review1 with the original candidate and pinned source,
new frozen campaign prompts/settings, and immutable previous task/candidate hashes.
The original records, attempt counters, results, candidate, source cache and public
files remain untouched. Existing runtime reservation and quality checks govern
review1, at most one correction, and review2 within the recovery envelope. Runtime
validation audits provenance and cumulative allocations. Failed recovery does not
publish or overwrite human work; accepted review still needs at least 95 and no
major/critical findings. Offline tests establish selection and ledger behavior,
not model compliance, billing guarantees or theological translation quality.

## Source compatibility

The source reader consumes archive format 2.0: `index.json.articles`, `catalogue.json.issues`, and `content/articles/<uuid>.html`. It does not read a core manifest or navigation file. It computes fingerprints inside the translation runtime, stores the observed inventory in `state/source.json`, and verifies each HTML fragment and its indexed image references. Existing compatible translation keys are preserved by retaining the previous fingerprint recipe.

Compatibility uses the source text, structure and translation-metadata fingerprints. Shared image pixel replacements do not incur a new translation. A wording change, structural change, or translated metadata change marks the previous translation stale. Conservative invalidation is intentional; this version does not silently transplant reviewed prose into changed markup. Snapshot caches are not independently authoritative and must never be edited.

## Website export

The website must pin both repository checkouts. From the clean translation checkout:

```bash
python -m berean_translation export \
  --source-checkout ../berean-voice \
  --output .build/website-input \
  --base /
```

For a Pages project path use the actual site's base, for example `--base /articles/`. The export includes translated HTML/sidecars, a display index, and a file-hash manifest. It contains no state, prompts, configuration, snapshots, skipped audit records, or images. The website uses the English repository's shared images and canonical article/group associations. It should use the exported translated `images[].alt` when generating image accessibility metadata.

Export compares every publication with a fresh runtime scan of the **selected** English checkout, not merely the translation repository's most recent poll. Incompatible, removed, unready and failed candidates are omitted. Output is assembled in a temporary sibling directory and promoted only when complete. Existing nonempty destinations are never erased. Base-path rewriting affects actual image attributes and the application's English-link attribute, not matching text inside article prose.

The configurable initial English route is `/en/articles/{article_id}/`. The website must implement that route or update `config/runtime.json.english_route` before initial publication. No live website URL is invented here.

## Provider and platform references

- OpenAI Batch lifecycle, ordering, expiration and 50% Batch rates: https://developers.openai.com/api/docs/guides/batch
- Official Python SDK: https://github.com/openai/openai-python
- Token pricing (recheck before production bulk work): https://developers.openai.com/api/docs/pricing
- Model snapshot/capability references: https://developers.openai.com/api/docs/models/gpt-4.1-mini and the corresponding configured model pages.
- GitHub manual inputs and concurrency behavior: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax

Batch stage completion can take up to its provider processing window. A collector polls submitted batches every 60 seconds within a 600-second window, then leaves remaining work to later runs. It exits immediately when no submitted/cancelling batch remains or no provider is configured. The positive wait budget includes initial work and is also checked between whole queue acceptances, collected batches and preparation units, and before uploads or submission intent. This prevents one tick from starting an entire backlog after the polling window has elapsed. The default zero budget retains a single unrestricted tick for callers that supply their own runtime boundary.

This is a soft admission deadline, not an interrupt: an in-flight operation and its durable checkpoint finish first. Once the worker begins recording submission intent, it makes its single create call and records the response or uncertainty even if the deadline passes meanwhile. Yielding before intent leaves a prepared batch with its original payload, reservation, attempt count and any uploaded input-file ID. The next collector resumes that identity instead of reserving or uploading again. A real interruption after intent still follows the existing conservative reconciliation rules. The worker finalizes campaign status and derived reports before returning to repository validation.

Bounded collection visits the least recently checked batches first, using a saved `last_collector_visit_at` alongside existing observation clocks. This includes paused prepared work and failed reads, so a slow prefix does not repeatedly consume every run. The visit timestamp describes local work only and never implies a provider poll, submission or completion. Terminal batches are not updated by this scheduling bookkeeping.

The 20-minute workflow retains a 600-second work window, leaving the remaining time for setup, in-flight I/O, durable checkpoints and validation. A single unusually slow operation can still exceed that allowance; increasing the archive does not make this a hard wall-clock guarantee. No retry, attempt, budget, concurrency, or publication guard is relaxed. The fallback schedule is every 15 minutes, offset from the hour; GitHub may delay scheduled runs, so this is not a completion SLA or a replacement for monitoring Actions. Public-repository schedules can be disabled after inactivity.

Provider observations retain `remote_*_at` lifecycle timestamps (Unix seconds), request counts, status, and `last_polled_at`. `collected_at` is local terminal-results pickup time; `completed_at` remains its legacy local-time alias. Do not infer provider timings for older records without those fields. Failed rows and provider failures remain terminal or safely recoverable under the existing rules; telemetry never authorizes a retry.

Successful state checkpoints are real commits. The report in `state/heartbeat.json` records actual successful discovery counts, source revision and pending tasks. It refreshes on meaningful snapshot changes and at least once per UTC day, so same-day completion is visible without meaningless idle keepalive changes.
## Reasoning-review output headroom

New ordinary campaigns freeze a model-aware review completion limit when they are
accepted: 3,000 tokens for non-reasoning reviewers, and at least 8,192 for a
registered reviewer with reasoning enabled, bounded by that model's output limit.
`review_output_tokens` remains the ordinary base; `reasoning_review_output_tokens`
is the finite reasoning-review floor. The downstream recovery policy retains its
separate explicit 8,192-token review limit. Reasoning and visible JSON share the
completion cap, so this provides headroom, not guaranteed JSON completion.

The acceptance rule also covers newly accepted exact-candidate re-reviews and
authorized source-refresh campaigns; downstream repair keeps its separate policy.
Queued requests not yet accepted use the policy in force at acceptance. Already
accepted campaigns, their recorded limits, request bytes, attempts and
reservations are unchanged. No failed work is replayed. The full resolved limit
is included in context checks and conservative reservations before submission.
Campaign dollar caps are never raised: the larger reservation can leave fewer
reviews affordable in a fixed-budget campaign. At the registered GPT-5-mini Batch
output rate, 8,192 instead of 3,000 increases the output reservation by $0.005192
per review. Truncation, refusals, malformed responses and quality failures still
fail closed; no automatic fallback or extra attempt is introduced.

## Never-submitted batch exclusion

Human control is rechecked before preparation, upload and the billable create, including edits incorporated by a checkpoint rebase. A prepared mixed batch is retained as cancelled-before-submission; its replacement contains only byte-identical authorized requests for unaffected articles. The replacement records `reservation_reused_from`, and the original records `replacement_batch`. The conservative existing reservation is carried forward, never added again, released or recycled; campaign allocation and task attempts are unchanged. Validation checks the one-to-one link, exact subset and proof that create was not called. An already uncertain submission still follows reconciliation and is never partitioned or blindly recreated.

## Derived-report regeneration

`validate --recognize-human-edits` always rebuilds `index.json`, `STATUS.md` and `RECOVERY.json` after recognition, then performs the same strict repository validation. This also supports accepted records that were backfilled before their reports. The reports are display projections, not human-review or spending authority. Repeating recognition is idempotent for unchanged inputs. CI refreshes them only in its checkout; the normal serialized collector publishes them with its durable checkpoints before billable work. No author needs to maintain generated reports or hashes.


### Retained accepted publications

Export never omits a previously accepted publication merely because the selected
English revision changed or removed it. Its status is `stale` or `source_removed`,
`retained` is true, and `retention_reason` is `english_changed` or `english_removed`.
`source_revision` and `source_translation_key` remain the accepted historical
values. `current_source_translation_key` is the current English key or null when
removed. The entry's `issue_id` retains the accepted source article's issue.
The manifest lists these entries under `retained`; they are not counted as omitted.

`retained_source` contains the frozen normalized `article`, `fingerprints`,
`repository`, `revision` and `translation_key`, plus `snapshot_sha256`,
`article_id`, `html_repository_path`, `html_sha256`, `index_repository_path`
and `catalogue_repository_path`. The consumer retrieves the English HTML and
needed catalogue/assets from that exact Git revision, without running historical
code. It verifies the HTML hash and reconstructs the original six-field snapshot
(the first five fields plus `html`) to verify `snapshot_sha256` using canonical
JSON. This binds historical metadata and HTML together. The original snapshot's
`metadata_sha256` is not a standalone hash of the normalized article object.
Raw state, original HTML and images are not copied into this display export.

The website must explicitly support this contract, keep source-version labels
truthful, and retain verified historical image and grouping dependencies. An
unverifiable retained dependency fails the candidate deployment safely; it does
not authorize silently removing an existing article or labeling stale work current.
Human-reviewed retained pairs stay permanently excluded from AI work.
