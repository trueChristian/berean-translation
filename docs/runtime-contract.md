# Translation runtime contract

The English archive, translations, and website are independent repositories. A translation is identified by its three-letter language code and original English article UUID. This repository owns translation processing, publication history, and website-ready exports. It does not duplicate English images or build the website.

## Translation and publication

The model receives the complete English article and translates prose, quotations, references, captions, and title/subtitle/section metadata in context. Preserve meaning, names, attribution, negation, paragraph structure, and source identity. Article text is untrusted data rather than an application instruction.

Ordinary acceptance requires 95/100; optional improvement of a published AI translation requires 98/100 and no regression against the accepted baseline. Both use independent review. One failed initial review can receive one correction and final review. Each task allows at most two translation/correction requests and two review requests. A substantive meaning error or incomplete response still fails regardless of score. A suggested correction identical to the current wording does not alone establish a substantive error.

There is no GetBible dependency or special reference, quotation, edition, evidence, offset, or versification gate. Ordinary semantic review covers quotations as article prose. Runtime checks protect complete output, safe HTML, article UUIDs, image paths, meaningful source structure, and readable metadata. Refusals, truncated output, invalid JSON, and missing results remain explicit failures.

Accepted HTML lives at `content/<language>/articles/<uuid>.html` with an independent localized notice. Its matching sidecar contains `title`, `subtitle`, and `section`, preserving absent/empty values. Alt/title attributes may be translated; shared image URLs and stable identities remain unchanged. Public files are distinct from pending candidates under `state/tasks/`.

A failed replacement never removes the last accepted publication. Accepted improvements archive the previous version before replacing public files. Source changes mark retained publications stale instead of deleting them. `index.json` describes accepted publications; human review is not a prerequisite for initial AI publication.

## Human editorial authority

A collaborator can edit the published HTML, sidecar, or notice and commit normally. Git attribution establishes the human action. Notice text is presentation, not a control protocol. The runtime preserves article-body bytes, updates metadata internally, and provides the localized human-first notice with an authoritative English link.

A human-edited article/language pair is permanently excluded from every later AI review, repair, retranslation, or audit, including forced requests and later source changes. Already-submitted results remain processing history and cannot overwrite human work. Human prose and reference values do not undergo AI fidelity gates.

Unreadable or technically unsafe edits are isolated without deleting their bytes. The hash-verified last accepted snapshot remains exportable while unrelated articles continue. A later human repair clears the diagnostic. Snapshots record accepted output and never grant spending authority.

Human exports retain `human_reviewed: true`, `ai_notice_required: false`, `human_edit` attribution, and `notice_present`. Isolated edits add `pending_edit` with a reason and `serving_last_accepted: true`; hashes refer to the accepted exported payload. AI-only publications retain their ordinary validation gates.

## Source discovery and resumable Batch work

Discovery reads a coherent English `main` checkout through `index.json`, format-2.0 `catalogue.json`, and `content/articles/<uuid>.html`. It ignores source `manifest.json` and `navigation.json`. Editors never maintain hashes or synchronize revisions manually. Fingerprints are computed here from source text, structure, and translated metadata. Image-pixel or unrelated category changes alone do not purchase another translation.

**AI — Discover English changes** is scheduled hourly. **AI — Collect and continue** is scheduled every 15 minutes and triggered after successful manual/discovery workflows. These configured schedules use best-effort GitHub delivery; gaps can extend for hours. Both share the serialized state writer. Manual translation and improvement enqueuers persist unique immutable requests without that concurrency group. New work and recovery receive separate scheduling allocations so a repair backlog cannot consume all admissions.

The automatic `max_active_tasks` capacity check counts only nonterminal autonomous tasks, with the current allowance of 50 separate from explicitly funded manual campaigns. Existing article/language claims still prevent overlap; automatic translation and recovery share the unchanged cumulative $30 authority. Existing submitted batches continue to be collected and advanced.

The collector uses OpenAI's Batch API:

1. Freeze a request's selection, source snapshots, model settings, prompts, prices, and permitted stages.
2. Persist the task and conservative cost reservation plus exact JSONL payload before external submission.
3. Persist the uploaded input-file ID, then submission intent, before one billable Batch creation.
4. Store the returned remote Batch ID, or record `submission_unknown` if acknowledgment is uncertain.
5. Poll stored batches and match downloaded output/error results by `custom_id`, never line order.
6. Advance the independent review/correction chain and publish accepted output.

Each stage specifies `completion_window='24h'` when creating its OpenAI Batch. Translation, independent review, correction, and final review are separate sequential batches, so article completion can extend beyond one 24-hour window, plus collection delays.

OpenAI does not call this repository back. During an admitted collector run, active batches are polled once a minute within a 600-second work window. It yields at resumable boundaries, leaving durable prepared requests for later runs. In-flight operations finish their checkpoint sequence; the deadline is an admission boundary, not an interrupt. The 20-minute Actions job leaves time for setup, I/O, final checkpoints, and validation.

Unknown submissions are searched by their unique metadata key rather than resubmitted. Only explicit owner confirmation that no matching remote batch exists can resolve absence. No hidden synchronous endpoint or automatic billable retry is permitted. Missing/expired results preserve successful independent items and failed evidence without silently purchasing replacements.

Git checkpoints never force-push. Disjoint concurrent changes can rebase; overlapping edits stop safely. A failed durable checkpoint blocks the next external side effect. Report generation is separated from essential submission checkpoints so aggregate bookkeeping does not consume every work window.

## Funding and migration

Whole-archive/all-language automatic work retains the cumulative shared $30 authority approved October 4, 2026, without renewal. New translation, source refresh, and saved-stage recovery share that ledger. Each new automatic envelope reserves the complete remaining chain and is at most $10. Separate accepted manual and legacy envelopes keep their original paid authorizations, models, rates, and limits.

Only complete terminal usage evidence can settle proven unused new-automatic headroom through immutable priced events. Partial usage, missing responses, uncertain submissions, and unproven cancellation retain their reservations. Actual usage is provider-reported at frozen rates, not invoice certainty. A changed prompt, model, run ID, or source revision never resets attempt history or authorizes another standing cap.

The October 8 plain policy removes retired Scripture prerequisites. Reuse existing candidates and re-review them under the current rules before buying another translation. Preserve original envelopes, submitted payloads, snapshots, results, findings, attempt counts, and funding evidence. Record migration through auditable processing events. Removing a retired hold does not itself prove a candidate acceptable. Refusals, unknown provider outcomes, and human protection remain effective.

The removed implementation is available in Git history; current code, dependencies, tests, workflows, and documentation do not retain dormant Scripture execution paths.

## Reporting and validation

Each eligible article/language pair contributes once to **published**, **unstarted**, **queued**, **active**, or **held without publication**. Their sum equals the eligible total. Pending/failed replacement attempts and source freshness are separate diagnostics. Accepted output remains published while its proposed replacement is active or held. Reports include collection timing, provider observations where available, and newly published counts.

`finished` means a campaign stopped processing, not that every candidate passed. A successful Actions run means runtime processing completed safely, not that all translation work was accepted. `remote_*_at` fields describe observed provider lifecycle timestamps; `collected_at` describes local pickup. Legacy records lacking both cannot establish provider versus collection delay.

`index.json`, `STATUS.md`, and `RECOVERY.json` are derived projections, not editorial or spending authority. `validate --recognize-human-edits` refreshes them after recognizing legitimate Git changes. CI performs this only in its own checkout. Authors never hand-maintain reports or fingerprints.

Run the offline regression suite, repository validation, workflow YAML checks, and read-only source compatibility validation. Tests simulate provider behavior and do not measure real translation quality or use paid credentials. Active dependencies are the official OpenAI SDK plus development YAML validation; no MCP client is installed.

## Website export

From a clean translation checkout with the website-selected English revision alongside it:

```bash
python -m berean_translation export \
  --source-checkout ../berean-voice \
  --output .build/website-input \
  --base /
```

Export verifies selected-English fingerprints and accepted output hashes. It emits public HTML/sidecars, a display index, and file hashes, without raw state, prompts, configuration, or copied images. The consumer uses the English repository's shared assets. Output is promoted only when complete; existing nonempty destinations are never erased. Base-path rewriting affects actual image and English-link attributes, not matching prose. The configurable initial English route is `/en/articles/{article_id}/`.

The Remnant website is configured to poll both repositories' `main` revisions hourly, with best-effort GitHub delivery, and rebuilds changed inputs. Deployment is owned by the website; this repository sends no notification or deployment dispatch.

### Retained accepted publications

Export never omits an accepted publication merely because selected English changed or disappeared. Its status is `stale` or `source_removed`, `retained` is true, and `retention_reason` is `english_changed` or `english_removed`. `source_revision` and `source_translation_key` retain accepted values; `current_source_translation_key` is the current English key or null. The original `issue_id` remains. The manifest lists retained entries separately rather than as omitted.

`retained_source` contains the frozen normalized `article`, `fingerprints`, `repository`, `revision`, and `translation_key`, plus `snapshot_sha256`, `article_id`, `html_repository_path`, `html_sha256`, `index_repository_path`, and `catalogue_repository_path`. The consumer retrieves English HTML and assets from that exact Git revision, verifies its HTML hash, and reconstructs the original six-field snapshot to verify `snapshot_sha256` using canonical JSON. The original `metadata_sha256` is not a standalone hash of the normalized article object. Raw state, English HTML, and images are not copied into this display export.

The website retains verified historical grouping/image dependencies and truthful source-version labels. An unverifiable dependency fails deployment safely rather than silently removing an already-served pair. Human-reviewed retained pairs remain excluded from AI work.

### Unexpected working-file isolation

Renamed, copied, or extra content files do not become accepted publications merely by existing. Human synchronization inventories them under `state/content-isolation.json`, preserves their bytes, and reports them while verified accepted copies continue serving. It does not follow symlinks or read unsupported files. Unknown files at a publication path also exclude that pair from paid AI work.

Automatic checkpoints exclude isolated paths. An already-staged isolated file pauses publication without changing the editor's staging. Such files are neither exported nor classified as human-reviewed without a recognized publication record.
