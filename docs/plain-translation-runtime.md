# Plain translation runtime: current policy

The owner approved this processing policy on October 8, 2026. It replaces Scripture-specific admission and publication checks while preserving translation quality, runtime safety, original paid authorizations, durable history, and the Remnant export format.

## What the engine does

The translator receives the complete original English article, translated-language guidance, and normal metadata. It translates the article in context, including Scripture quotations and references. Meaning, names, attribution, negation, headings, notes, captions, and paragraph structure remain important. The engine does not obtain replacement Bible words or resolve reference/quotation mismatches.

Ordinary independent review uses **95/100**. A passing candidate must preserve meaning and completeness and satisfy basic technical checks. Technical checks protect article identity, image URLs, safe markup, coherent metadata, and complete model output. Refusal, invalid JSON, truncation, and missing substantive content remain failures. Reviewer scores are rubric assessments, not statistical measurements.

Review findings must identify substantive errors. A finding that proposes the current wording as its own correction does not establish a meaning defect. Publication still rejects genuine omissions, changed meaning, altered attribution, and incomplete or unsafe output. No special Scripture evidence, matching, normalization, offsets, edition, or versification prerequisite participates in current acceptance.

The ordinary task makes one translation and one independent review. If needed, it makes one correction and one final review. Accepted output enters the public collection with its localized AI notice. Failed candidates and their findings remain in task history.

## Batch lifecycle and schedules

The runtime submits durable OpenAI Batch requests and stores their identities. OpenAI does not send this GitHub repository a callback. **AI — Collect and continue** polls stored batches, downloads terminal results, queues the next permitted stage, and publishes accepted candidates.

**AI — Discover English changes** scans current English once hourly and queues authorized work. **AI — Collect and continue** runs every 15 minutes and after a successful manual translation, improvement, or discovery workflow. Its active polling window lasts up to ten minutes with one-minute polls, yielding safely at resumable work boundaries. Both writers share one concurrency group. Manual enqueues persist unique immutable files and do not share that group.

Discovery and result collection are distinct operations:

```bash
python -m berean_translation tick --discover-only --publish
python -m berean_translation tick --no-discover --publish --wait-seconds 600 --poll-seconds 60
```

Discovery does not receive the OpenAI secret or submit paid work. Collection receives the secret and acts only on existing authorized work. Batches can run concurrently at OpenAI, while repository state changes remain serialized. Scheduling shares available slots between fresh translations and recovery so the failed-candidate backlog cannot consume every slot. Both use the existing cumulative funding authority.

Keep durable checkpoints around external side effects. Aggregate reports may refresh less often than submission checkpoints, but reports must reconcile to durable current state. Unknown submissions are reconciled before any retry; no hidden synchronous fallback is allowed.

## Removing the old Scripture dependency

Current processing does not use:

- GetBible prefetch or MCP client construction;
- required language-specific Bible editions;
- printed quotation/reference admission;
- exact target-edition words or selected verse substrings;
- character offsets or quotation alignment;
- special Scripture review and publication holds;
- the specialized Scripture inspection/repair workflows.

The website's existing Scripture modal retrieves the selected Bible text in the reader's language. AI-translated article quotations can differ from that edition's wording. This is the accepted division of responsibilities.

Historical requests, results, evidence, and modules remain readable and auditable. Retired YAML fixtures live under `docs/historical-workflows/`; they are not active Actions workflows. The worker installs only `requirements.txt`. Optional `requirements-scripture.txt` remains for historical clients and offline SDK regression tests, not active translation.

## Recovering existing work

A candidate held under the retired Scripture contract must not be discarded or declared accepted merely because the gate was removed. Reuse the candidate, review it under the current plain policy, and publish it only if it passes. An entry held before translation becomes eligible for ordinary work once its retired prerequisite is superseded.

Record policy migration and resumed work with append-only history. Keep original immutable queue envelopes, frozen Batch payloads, source snapshots, results, findings, model identities, attempt history, budget authorizations, and reservation evidence. No migration resets allocations or grants a new automatic ceiling.

Submitted historical batches retain their exact request bytes. Their collected output is evaluated through the current plain policy, with a recorded processing-policy event and original provider history retained. Legacy review responses keep their original JSON schema. An unpublished returned candidate retires its old queued next stage and resumes through plain-policy review. Genuine provider refusals, uncertain outcomes, and protected human content are not converted into publishable translations. Saved-candidate recovery uses remaining authorized funding and complete-stage reservations.

## Optional improvement at 98

**AI — Improve translations** defaults to `gpt-6.1-sol` for improvement and validation. It assesses an existing AI publication against original English. A proposed upgrade must reach **98/100** and preserve meaning, completeness, and technical validity. A higher score or stronger model name alone does not establish improvement; compare the source fidelity and current accepted text.

The public accepted version stays available throughout processing. An accepted replacement retains the previous version in publication history before replacing the public HTML/sidecar. Failure leaves the last accepted version available. Human-edited article/language pairs are permanently excluded from AI replacement.

A saved unpublished candidate selected through this workflow is recovery, rather than an upgrade to an existing publication, and uses the ordinary **95** threshold. Preserve the exact baseline and requested operation so that the two cases cannot silently change thresholds.

## Counters and output compatibility

Count each eligible article/language pair once in a primary category:

| Category | Meaning |
| --- | --- |
| Unstarted | No accepted publication, active reservation, or terminal hold. |
| Queued | Eligible work is durably waiting to start. |
| Active | A translation/review/correction chain is in progress. |
| Held without publication | Unpublished work currently needs resolution, funding, or substantive correction. |
| Published | An accepted publication exists, including a retained older publication. |

The categories sum to the eligible total. Historical failed attempts, pending upgrades, and source freshness are separate diagnostics. A failed new candidate does not count the same pair as both published and unfinished. Reports expose meaningful last-collection information and new publication counts; green Actions execution alone does not establish successful translation.

Keep original UUIDs, three-letter language folders, HTML and metadata sidecars, notices, shared image paths, display index/export, and accepted source provenance. Stale or source-removed output stays available with truthful labels. Remnant continues polling source revisions hourly and consumes the same export contract. This change requires no website notification service or new token.

An unreadable English article is isolated from new paid work and remains in the target counts. If its accepted translation cannot be compared with a verified current English fingerprint, export reports that specific source failure and preserves the last deployed site; it does not invent a fingerprint or silently drop the article.

## Activation

Merge the implementation PR into `main`. Runtime changes trigger **AI — Collect and continue** immediately, with its 15-minute schedule as a fallback. The initial collector seeds the source cache if needed, migrates never-paid admission holds, reviews saved candidates under the plain policy, and regenerates truthful reports. Subsequent hourly discovery refreshes the source cache. Improvements remain optional manual requests.

## Funding and human authority

The existing shared cumulative **$30 automatic authority** remains unchanged and does not renew. Automatic envelopes reserve complete remaining stages and remain at most $10 each. Accepted legacy/source-refresh/manual envelopes keep their original terms. Settlement is allowed only where existing rules prove unused automatic headroom through complete usage evidence; unknown charges remain reserved.

Human commits remain final editorial authority. The runtime recognizes Git attribution, preserves article-body bytes, records review metadata, and prevents any later AI replacement. A broken human working file is preserved and isolated while the last accepted verified copy remains exportable. Never bypass human protection, funding, refusals, or uncertain submission guards to improve completion statistics.
