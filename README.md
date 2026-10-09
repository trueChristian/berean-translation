# Berean translation

Translate the complete English articles from [`trueChristian/berean-voice`](https://github.com/trueChristian/berean-voice) with OpenAI's Batch API, review their meaning and language quality, and publish accepted translations for the [Remnant website](https://github.com/trueChristian/remnant.truechristian.church).

**Ordinary translations pass at 95/100. Optional improvements to published translations pass at 98/100.** These scores are review rubrics, not measured percentages of accuracy. English remains authoritative. A complete, technically valid translation that meets its threshold and preserves the source meaning becomes available immediately, with the localized AI notice.

**Scripture matching is retired from translation processing.** The AI translates quotations and references as part of the complete article. There is no GetBible prefetch, Bible-edition requirement, exact quotation alignment, quotation-offset contract, or special Scripture publication gate. The website's Scripture modal owns retrieval from the selected Bible edition. Read [the current processing and migration policy](docs/plain-translation-runtime.md).

## Automatic operation

The scheduled discovery workflow reads current English `main`, finds new and changed articles, and queues authorized work. The collector submits and polls OpenAI batches, receives completed results, advances independent review and bounded correction, publishes accepted translations, and resumes eligible failures. OpenAI does not call this repository back; GitHub Actions polls it.

All twenty configured languages participate. New translation, source refresh, and saved-candidate recovery share the existing cumulative **$30 automatic authority**, without renewal. New automatic work reserves its complete remaining stages; each campaign envelope remains at most $10. Separate accepted manual and historical envelopes keep their original limits. A new run or changed prompt never resets budget or attempt history.

Discovery and collection share one serialized state writer. Manual requests persist unique immutable queue files, so concurrent requests are not lost to GitHub's limited pending-run concurrency queue. Submitted batches can run concurrently. Unknown submissions are reconciled by their stored submission identity and are never blindly resubmitted.

The automatic `max_active_tasks` allowance is 50 active automatic tasks, separate from explicitly funded manual campaigns. Manual work does not consume those slots; article/language claims still prevent overlap, and new automatic translation and recovery retain the same cumulative $30 funding authority.

## Workflows

Only five workflows are active:

| Workflow | Responsibility | Schedule or trigger |
| --- | --- | --- |
| **AI — Translate articles** | Select an issue, languages, models, and a manual budget; persist a translation bundle. | Manual dispatch; free preview is the default. |
| **AI — Discover English changes** | Discover upstream English changes and queue automatic work within its standing authority. | Scheduled hourly at minute 3, or manual dispatch. |
| **AI — Collect and continue** | Submit authorized work, poll results, validate/correct, publish, recover eligible candidates, and perform explicit maintenance. | Scheduled every 15 minutes; successful manual/discovery workflows; published human edits; manual dispatch. |
| **AI — Improve translations** | Review existing publications against English with stronger models; validate accepted replacements at 98. Saved unpublished candidates use the ordinary 95 threshold. | Manual dispatch; free preview is the default. |
| **Translation runtime checks** | Run offline regression tests, dependency contract checks, and repository/source validation. | Pull requests, pushes to main, or manual dispatch; no paid requests. |

The specialized repair and Scripture-inspection workflows and their runtime dependencies have been removed. Git history preserves the previous implementation; durable paid requests, results, and allocations remain audit records.

These are configured schedules, not guaranteed start times. GitHub delivers scheduled runs on a best-effort basis, and gaps can extend for hours.

## Run production work

1. Merge the reviewed implementation into `main`. Production workflows use trusted `main`; a pull request does not activate them.
2. Ensure the repository Actions secret **`OPENAI_API_KEY`** is set and Actions can commit runtime changes with `contents: write`. Manual enqueuers and discovery do not receive the OpenAI secret.
3. Run **AI — Discover English changes**, then **AI — Collect and continue** if immediate pickup is wanted. Their configured schedules request subsequent runs automatically; start times remain best effort.
4. Read [STATUS.md](STATUS.md) and [RECOVERY.json](RECOVERY.json) for publications, primary progress counters, batches, remaining funding, and specific holds.

New manual requests default to a $30 ceiling and **dry_run=true**. A preview makes no paid requests or reservations for that selection; it does not suspend other previously authorized work. Select **dry_run=false** to authorize the selected bundle. Adding a missing API key resumes existing authorized queued work, so inspect those requests first if their intent has changed.

### Choose articles and languages

Use the language dropdown for one code or **all**, or override it with a comma-separated **languages** list such as `afr,deu,spa`. Registered language-tag aliases are accepted. Duplicate aliases for one language are rejected.

The issue selector supports **next**, **all**, **outstanding**, and **custom**. Supply comma-separated issue `source_id` values or UUIDs from `STATUS.md` or `state/source.json` in **issues** to override that preset. **next** selects the first catalogue issue with eligible work; it does not infer chronology from a seasonal label. Up to 1,000 article/language tasks can be selected in one manual request; automatic bounded pages continue through the whole archive.

Manual translation and its independent reviewer default to **gpt-6-luna**. Optional improvement and its independent validation default to **gpt-6.1-sol**. Models, context limits, and rates are frozen from `config/models.json` at acceptance. Existing accepted requests retain their original paid authorization, models, and history.

## Translation, review, and improvement

The normal path is a translation, independent review, and publication at 95. A failed review can receive one correction followed by final review. At most two translation/correction requests and two reviews are submitted per task. The model receives the complete English article, context-preservation instructions, and configured terminology guidance. It must preserve meaning, negation, attribution, names, references, headings, paragraphs, images, captions, notes, and metadata; it must not summarize or invent.

Each stage is submitted with a `24h` Batch completion window. Translation, review, correction, and final review use separate batches, so completing an article can span multiple windows plus collection delays.

There is no special Scripture acceptance layer. Ordinary meaning review still checks quotations as article prose. Basic technical checks still reject incomplete responses, unsafe or malformed HTML, changed article identities or image URLs, missing substantive content, refusals, and invalid JSON. A score at the threshold does not excuse a substantive meaning error. Contradictory no-change reviewer findings are not automatically treated as substantive rejection evidence.

Existing held candidates are reconsidered through the plain-translation policy. Saved candidates receive review before paying for another translation. Retired Scripture holds no longer require editions or evidence. Migration adds auditable processing events and preserves original requests, candidates, paid attempts, and findings. Uncertain provider outcomes and refusals are not silently converted into accepted work.

Optional improvement compares the existing accepted translation with original English and uses the 98 threshold. The current public translation remains available while a proposed replacement is processed. An accepted improvement archives the previous version and replaces the public files; a failed improvement leaves the previous publication available. Human-edited pairs remain permanently protected from AI replacement.

## Accurate progress

Each eligible article/language pair contributes to one primary category: **unstarted**, **queued**, **active**, **held without publication**, or **published**. These categories account for the eligible total without counting one pair twice. Counts of upgrade attempts and historical failures are separate diagnostics; a failed newer attempt does not turn an accepted publication into an unfinished pair. Source freshness is reported explicitly rather than relabeling a retained stale translation as current.

Reports include collection timing and provider state. A successful collector run means the runtime completed its work safely, not that every translation was accepted. A submitted Batch can remain processing between polls. Compare provider completion observations and local collection times only when both are recorded; historical entries without those observations cannot establish their delay.

## English editing and website compatibility

English editors change HTML and canonical catalogue metadata in `berean-voice` and commit normally. They do not regenerate manifests, maintain hashes, or copy revisions into this repository. Discovery reads `index.json` and the format-2.0 catalogue, ignores source `manifest.json` and `navigation.json`, and computes translation-owned fingerprints from a coherent English checkout.

The translation output remains `content/<code>/articles/<original-uuid>.html` with its matching `.json` sidecar. The sidecar contains translated `title`, `subtitle`, and `section`, preserving absent or empty values. Images remain shared `/images/articles/<uuid>-<sequence>.<ext>` URLs. No English articles, images, website, or database are duplicated here.

Accepted publications are retained when English changes or disappears, with truthful stale/source-removed status and their original source provenance. A source refresh does not remove the last accepted publication while its replacement is pending. Existing UUIDs, language codes, export structure, and the Remnant consumer contract are preserved.

| Language | Folder | Website language tag | Direction |
| --- | --- | --- | --- |
| Mandarin, Simplified Chinese | `cmn` | `zh-Hans` | LTR |
| Hindi | `hin` | `hi` | LTR |
| Spanish | `spa` | `es` | LTR |
| Arabic | `ara` | `ar` | RTL |
| French | `fra` | `fr` | LTR |
| Bengali | `ben` | `bn` | LTR |
| Portuguese | `por` | `pt` | LTR |
| Indonesian | `ind` | `id` | LTR |
| Urdu | `urd` | `ur` | RTL |
| Russian | `rus` | `ru` | LTR |
| German | `deu` | `de` | LTR |
| Dutch | `nld` | `nl` | LTR |
| Afrikaans | `afr` | `af` | LTR |
| Swahili | `swa` | `sw` | LTR |
| Korean | `kor` | `ko` | LTR |
| Italian | `ita` | `it` | LTR |
| Hebrew | `heb` | `he` | RTL |
| Modern Greek | `ell` | `el` | LTR |
| Swedish | `swe` | `sv` | LTR |
| Norwegian, Bokmål | `nob` | `nb` | LTR |

The website owns route spelling, language switches, and Scripture modals. Languages are not equated with national flags. The [Remnant website](https://github.com/trueChristian/remnant.truechristian.church) is configured to poll both source repositories' `main` revisions on an **hourly**, best-effort schedule, rebuilding when revisions differ from the last successful deployment. No translation-repository notification token or website dispatch is required.

## Human editing

A human collaborator edits published HTML or the title/subtitle/section sidecar and commits normally. The collector recognizes Git attribution, preserves the edited article body, updates internal metadata, and protects the pair from every future AI review, repair, or retranslation. Notice wording is presentation, not an authorization protocol. The runtime supplies the localized human-first notice with an English link and no public reviewer name or model version.

A technically broken working file is preserved and isolated with a diagnostic. Its hash-verified last accepted copy remains exportable while unrelated work continues. No model call repairs human edits. Uncommitted or bot-authored changes cannot claim human review.

## Recovery and maintenance

The collector automatically resumes eligible failures using saved candidates and bounded funding. New work has its own scheduling allocation so repairs cannot occupy every available slot. Exhausted budgets, unresolved provider outcomes, substantive repeated failures, and protected human content remain visible rather than being reset or silently bypassed.

Use **AI — Collect and continue**, operation **cancel**, with a campaign ID to stop that campaign's unsubmitted work and request cancellation of submitted batches. Existing publications remain intact. Cancellation retains task history and allocations; it is not a refund. An interrupted historical acceptance is closed through this same cancellation path, not by deleting records.

For **submission_unknown**, collection reconciles the persisted unique submission key with provider batches. Only after confirming that the provider created no matching batch should an owner choose **resolve-absent** with the internal batch ID and explicit confirmation. Slow processing alone does not establish absence.

Checkpoint pushes are never forced. A failed durable checkpoint blocks the next external side effect, and failed Actions runs retain recovery artifacts. Disjoint Git changes can rebase; same-file conflicts require resolution without overwriting concurrent work.

## Export and development

From a clean translation checkout with the website-selected English revision checked out alongside it:

```bash
python3 -m berean_translation export \
  --source-checkout ../berean-voice \
  --output .build/website-input \
  --base /
```

The exporter verifies source fingerprints and accepted publication hashes, emits compatible HTML/sidecars plus display metadata, and never exports raw processing state. It refuses to erase an existing nonempty output directory. Use the actual Pages base path where applicable. The initial notice's English route is configured by `config/runtime.json.english_route`.

Python 3.11 or later is required. The collector installs the official OpenAI SDK from `requirements.txt`. Development adds YAML validation through `requirements-dev.txt`; there are no MCP or GetBible dependencies.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m berean_translation validate
python -m compileall -q berean_translation tests
```

Tests use artificial source articles and simulated provider responses. They do not produce live translations or spend money. Real local bare-Git tests cover checkpoints, concurrency, conflicts, and human attribution. Production commands require trusted `main`, a durable authenticated origin, and `--publish`:

```bash
python -m berean_translation tick --discover-only --publish
python -m berean_translation tick --no-discover --publish --wait-seconds 600 --poll-seconds 60
```

The positive collection wait budget includes initial work and yields at resumable boundaries. An in-flight operation finishes its durable checkpoints. Prepared batches retain their exact payload and reservations for the next run. Unknown submissions do not keep a runner alive by themselves. Read-only source compatibility checks use `python -m berean_translation discover --check-only`.

See [AGENTS.md](AGENTS.md), [the current processing policy](docs/plain-translation-runtime.md), and [the retained publication/export contract](docs/runtime-contract.md#retained-accepted-publications).
