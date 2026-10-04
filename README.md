# Berean translation

An independent, resumable OpenAI Batch translation runtime for the authoritative English articles in [`trueChristian/berean-voice`](https://github.com/trueChristian/berean-voice). Python and GitHub Actions manage requests, source revisions, translations, quality checks, review history and website-ready exports. There is no database, permanently running server, website framework or image duplication.

**Publication policy:** a translation that passes the automated checks is available for website export immediately, with a localized AI notice. Human review is optional: a collaborator reviews/corrects the translation, removes the complete notice block and commits. English always remains authoritative. Failed candidates are retained for inspection but are never exported as finished translations.

## English editors do not maintain hashes

Edit an English article in `berean-voice`, commit the normal source change, and stop. This repository's collector reads English `main`, matches article UUIDs against its language records, and computes its own fingerprints to detect revisions. It ignores core `manifest.json` and `navigation.json` completely. A new article is missing work; a changed article is outdated work; unchanged articles are skipped. No source-side regeneration or manually copied commit/hash is required.

The collector respects manual language/issue selections, paid-work budgets, the bounded correction cycle, and protection of human-reviewed translations. The owner-approved source-refresh policy automatically retranslates **already-published AI translations** after their English fingerprint changes, within the limits below. First translations of new articles or languages still require manual requests. Downloading `main` once per run avoids mixing old and new source files; the revision is internal provenance.

## Activate after merging the implementation

1. Add an Actions repository secret named **`OPENAI_API_KEY`** under **Settings → Secrets and variables → Actions**. Use an OpenAI API project with billing and access to the selected models. Do not put the key in a file, workflow input, issue, pull request or chat.
2. Enable GitHub Actions. The request and collector workflows need `contents: write` in this repository. The workflow files request it explicitly; organization policy or a protected `main` may still prevent the standard Actions token from making the runtime commits. Configure an appropriate permitted automation path rather than disabling protections indiscriminately. No personal token is needed for the supplied public English archive.
3. Run **AI — Collect and discover**, operation **collect**, from `main`. This discovers the current English issues and generates [STATUS.md](STATUS.md). With an API key, it can spend on existing authorized requests and eligible automatic source refreshes. For a strictly read-only source check, use `discover --check-only`; discovery also works before the OpenAI key is added.
4. Run **AI — OpenAI** from `main`. Select a language, **next** issue, translation and review models, and a USD budget. Leave **dry_run** checked for a free selection preview. The request is stored immediately; the collector resolves it and records the selection under `state/campaigns/gh-<run-id>.json`.
5. Submit a new manual run with **dry_run** unchecked when ready to translate. The collector starts after a successful request workflow and has a best-effort 15-minute schedule. While submitted work remains, it polls every minute within a 10-minute window, collecting completed batches and submitting the bounded review/correction stages without waiting for another scheduled run. A later collector resumes anything still processing. The runner does not wait for an entire OpenAI batch window.

A dry-run request is deliberately a free selection preview, not a certified price quotation or a translation-quality assessment; it does not pause separately authorized existing campaigns or automatic source refreshes. Adding the API key later resumes any previously authorized, non-dry-run requests already in the queue. Inspect/cancel those requests before adding the key when their intent has changed.

## Workflows

| Workflow | Purpose | Paid requests |
| --- | --- | --- |
| **AI — OpenAI** | Persist one manual translation request with issue/language/model selections. | The collector submits the authorized work; this enqueuer has no OpenAI secret. |
| **AI — Review** | Request a new bounded AI review of existing translations or saved failed candidates. | Review, and at most one correction plus final review. |
| **AI — Recover candidates** | Review an exact allowlist of failed candidate task IDs from one finished original campaign. | Uses only an explicitly allocated part of the original remaining cap; review, at most one correction, then final review. |
| **AI — Collect and discover** | Discover source updates, process queued requests, collect/resume batches, recognize human review, or perform explicit cancellation/recovery. | Manual campaigns plus the explicitly bounded source-refresh policy. |
| **Translation runtime checks** | Offline regression tests, installed SDK contract check, repository validation and read-only source compatibility check. | None. |

Manual workflows are intentionally restricted to `main`. Merge the implementation before trying to run production translation work. Do not add an API secret to a pull-request test environment.

### Select multiple languages and issues

The single-language dropdown includes all twenty languages and **all**. The optional **languages** field overrides it with a comma-separated list, for example `afr,deu,spa`. Three-letter folder codes and the registered two-letter/language-tag aliases are accepted. Duplicate aliases for the same language are rejected.

The issue dropdown supplies **next**, **all**, **outstanding** and **custom**. For particular issues, copy one or several `source_id` values or UUIDs from `STATUS.md` / `state/source.json` into the **issues** field, separated by commas; this overrides the preset. The live issue list is discovered from the core repository, not hardcoded into workflow YAML. GitHub's native workflow dropdown cannot dynamically populate from a repository file or select multiple values, so validated list inputs provide those capabilities.

**next** selects the first issue in the source catalogue with eligible work for the selected languages and operation. It does not guess chronology from seasonal dates. **all** and **outstanding** both examine all selected source issues, but normal translation eligibility still excludes completed, active and protected work. A request currently allows up to 1,000 article/language tasks; a larger archive request must be divided across manual runs. Batch sizes have separate safety limits.

Concurrent manual runs create different immutable queue files. OpenAI batches may run simultaneously, while one collector serializes mutable repository writes. Re-running the same GitHub workflow run is idempotent; submitting a new run creates a new request, whose article eligibility checks still prevent duplicate translation charges. Completed translations are selectable for review, not silently translated again. Explicit **retry_failed** is required to retry an unsuccessful translation through **AI — OpenAI**.

### Recover an exact candidate subset

Use **AI — Recover candidates** when only particular saved candidates should be
re-reviewed. Supply a finished **original_campaign** ID and the exact comma-separated
**previous_task_ids** from that campaign, an explicit **budget_usd** envelope (at most six decimal places), and
models (both default to **gpt-5-mini**). This separate workflow has no language or
issue selectors. Its immutable request uses `recovery_of_campaign` and
`previous_task_ids`; combining these with ordinary selectors or source-refresh
fields is rejected, never expanded to workflow defaults. Each request belongs to
one original campaign. Split selections with different original parents into
separate requests.

Keep **dry_run** checked first. The campaign report lists each exact previous
**task / article / language**, pinned source and candidate hashes, original cap,
original reservations, all prior recovery allocations, proposed envelope, and
remaining balances. `remaining_after_usd` is the **hypothetical** balance after the
proposed envelope; a preview's actual `recovery_allocation_usd` is zero. No recovery
tasks, budget reservations, source snapshots, or API objects are created by a
preview. The collector can still process other independently authorized work.
To proceed, submit a **new** workflow run with `dry_run` unchecked; rerunning or
changing the existing request cannot turn a preview into paid work.

Acceptance is all-or-nothing. Every target must exist exactly once, be the latest
terminal **not_ready** task in the named finished original campaign, retain its
candidate and valid pinned source/model provenance, and still match current
English. Any unknown, duplicate, active, stale, mixed-parent, previously recovered,
human-protected, or already-published target rejects the entire request. Published
translations remain eligible through ordinary **AI — Review**, not this recovery
selector. Recovery is an explicit selection, never automatic filtering of holds.

The full requested envelope is durably checkpointed before any recovery task is created and allocated against:

```text
original cap − original reserved ceiling − every previously accepted recovery envelope
```

The original campaign and task ledgers remain unchanged. Accepted envelopes are
**never released or reused**, even after failure, cancellation, incomplete
acceptance, or lower reported usage. Concurrent enqueuers write independent queue
files; the existing single collector accepts them sequentially against the shared
remaining cap. Every original task can receive at most one accepted exact recovery
across all history. A preview neither allocates funds nor consumes that opportunity.

Recovery copies the existing paid candidate and reuses its verified source
snapshot. It creates a new review-first task with fresh frozen prompts/settings
and explicit links/hashes back to the untouched original task and candidate. No
old attempts, findings, candidates, or source history are reset. A failed review
may use one correction and one final review, all within the new envelope. The
95-point/no-major-or-critical gate and normal human/publication protection still
apply. The envelope is a hard stop, not a guarantee that every stage fits or that
any candidate will pass. Successful enqueues wake the same trusted-main collector
as ordinary review requests; the shared state-writer lock is unchanged.

### Model selection and expenditure

The manual workflow defaults and automatic source-refresh policy use **gpt-5-mini** for translation and review, matching the first campaign; the low-level runtime fallback remains **gpt-4.1-mini** when a caller omits a model. These are operational choices, not proof of theological translation quality for all twenty languages. The allowlist also includes **gpt-4.1-nano** and **gpt-4.1**, with pinned API snapshot names and explicit Batch prices in `config/models.json`. Choose a different model for an explicitly requested independent review when appropriate.

All model work uses OpenAI's Batch API; there is no hidden synchronous fallback. Each campaign has a USD reservation ceiling covering translation, review, correction and final review. The runtime reserves a conservative input/output upper estimate before each batch submission and retains actual returned token usage for comparison. Input estimates deliberately overestimate using UTF-8 bytes plus framing allowance. The application never recycles an uncertain reservation to authorize more work.

A low budget can pay for the initial translation but leave insufficient reservation for its review. That task becomes **budget_blocked**, not ready for publication. Use **AI — Review** to continue from the saved candidate under a new, explicit budget rather than starting its translation again. Application limits depend on the configured model rates and are not a provider billing guarantee; also configure appropriate OpenAI project/account budgets and alerts, and verify whether they enforce a hard cap before relying on them. Review the rate table before large campaigns.

The quality path is strictly:

```text
translation → independent review → publish with notice when accepted
                         ↓ failed
              one correction → final review → publish or not_ready
```

At most two translation/correction requests and two review requests are submitted per task. A score of 95/100 is an acceptance rubric, not a statistical measurement of 95% accuracy. Major/critical findings always block acceptance. Missing/truncated/refused responses, altered IDs/URLs, malformed HTML, missing substantive blocks and changed Scripture chapter/verse numbers fail structural checks. Model requests include the complete source article, theological-preservation instructions, and any configured per-language glossary.

## Languages and folder/URL identity

| Language | Folder | Website language tag | Direction |
| --- | --- | --- | --- |
| Mandarin, initially Simplified Chinese | `cmn` | `zh-Hans` | LTR |
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
| Norwegian, initially Bokmål | `nob` | `nb` | LTR |

The website owns route spelling, `hreflang`, language-switch presentation and any icons. Languages are not equated with national flags. Mandarin and Norwegian variants are explicit initial choices, not interchangeable labels; add a separate, reviewed configuration for another variant. Localized notice text and terminology guidance should also receive native-language review.

## Repository layout and review history

```text
config/                       Language registry, model choices, limits and glossaries
prompts/                      Translation and independent review instructions
berean_translation/           Python CLI and runtime modules
content/<language>/articles/  Published HTML and translated metadata sidecars
state/queue/                  Immutable manual requests
state/campaigns/              Selections, budget reservations and campaign status
state/tasks/                  Candidates, per-stage findings, model/usage history
state/batches/                Exact JSONL inputs, batch IDs and recovery state
state/records/                Article/language identity, publication and review history
state/sources/                Hash-verified pinned source snapshots
state/source.json             Last discovered source issue/article catalogue
state/heartbeat.json          Current meaningful source/work snapshot, refreshed at least daily
index.json                    Generated website-facing translation catalogue
STATUS.md                     Generated issue, language and campaign status
```

The initial checkout has no fake articles or pretend completed batches. Runtime directories appear as genuine work is performed. Every translation retains the original article UUID. Its HTML sidecar contains translated `title`, `subtitle`, and `section`; captions and alt text are translated in the HTML. Absent source metadata stays absent. Authors, credits, source provenance and grouping identities remain available from the English source.

Inspect `state/tasks/<task-id>/results/review1.json` and `review2.json` for the actual findings and rubric scores. A corrected task retains its initial result, correction result, model identities, request usage and attempt counts. The public file is separate from its pending candidate; failed re-review does not replace a last good translation. The reports distinguish public readiness from the status of newer candidates.

### Human review

Open a published HTML file and its adjacent metadata JSON. Review and correct the translation without altering article UUIDs, image URLs or source structure. Remove the **entire** trailing `<aside class="translation-notice" data-translation-notice="ai" ...>...</aside>` after the article. Commit or merge the reviewed change into `main`.

The collector recognizes this ordinary content commit, records its Git attribution, updates the generated catalogue, and preserves the model/source history. A human edit that retains the notice stays AI-unreviewed. Partially deleting the notice is rejected so the application does not misrepresent review status.

An **AI — Review** request can inspect a human-reviewed translation, but it cannot overwrite it or remove its notice on the reviewer's behalf. Any accepted correction is saved as a **proposal** under the task. A human applies the useful changes to the published files in another content commit. Repository commit attribution records the collaborator's action; it is not independent certification of the translation's correctness.

### New and changed English articles

The scheduled collector discovers new issues and articles automatically and makes them selectable. It also updates the observed source revision and identifies stale/withdrawn translations. Use **next** or **outstanding** in a manual translation request for first-time translation work.

The translation runtime computes source text, markup and translation-metadata fingerprints automatically; source editors never maintain them. Those local fingerprints govern compatibility. An image pixel replacement or unrelated category edit alone does not require retranslation. Relevant English changes mark older translations stale without deleting them or overwriting human corrections. Each requested campaign retains its exact source commit and source snapshots.

### Automatic refresh of changed English

`config/runtime.json.automatic_source_refresh` is an explicit standing spending policy, enabled for source changes to existing AI-published translations. At the start of each collector run, the current English scan can create immutable, source-hash-deduplicated refresh requests. Pickup uses the collector's best-effort 15-minute schedule and manual **collect** runs. Website publication independently uses the website repository's polling workflow.

- Only already-published, non-human-reviewed article/language pairs with a changed translation fingerprint qualify. New articles, new languages, withdrawn source articles and compatible publications are excluded
- Each request contains an exact article set and expected source fingerprints for **one issue and one language**. It cannot expand into the rest of an issue or corpus when accepted
- Translation and review use **gpt-5-mini**, with a **$10 maximum per issue-language refresh campaign**, covering all bounded stages. This is a per-campaign cap, not a shared lifetime or daily cap; another new source version or another published language can authorize a separate capped campaign. The collector queues at most five new refresh requests per initial discovery tick; previously queued work remains subject to the normal acceptance and batch limits
- Existing active work and pending overlapping manual requests take precedence. All task history and immutable refresh requests prevent automatically retrying the same source fingerprint, including prior failed, cancelled or budget-blocked work and source-version rollbacks. A failed version requires an explicit manual retry/review
- If the source changes again before a queued refresh is accepted, that old fingerprint is skipped; a later discovery may authorize the new one. If source changes during active work, the next collector can refresh the newer version after that work finishes
- A failed refresh preserves the last good files and provenance; only current-source-compatible publications are exported. Human-reviewed translations stay protected

Merging with this policy enabled allows the next collector to refresh eligible stale publications without another manual translation request. Setting `enabled` to `false` prevents new automatic requests and pauses unaccepted automatic requests; already-accepted campaigns remain durable authorized work and must be cancelled explicitly if desired. Campaign records freeze the policy, exact source selection, spending reservations and reported usage for audit. Tests and CI never submit real paid work.

## Recovery and cancellation

**No progress yet:** inspect the collector workflow and `STATUS.md`. A submitted OpenAI batch may still be processing. The worker polls active batches within its bounded window, then exits; idle runs exit immediately. Manually running **collect** is safe and does not create a duplicate campaign. Scheduled runs are subject to GitHub scheduling availability, not a guaranteed completion deadline. The single state-writer concurrency group is unchanged; a maintenance/cancellation run may wait for the active collector to finish. No new service, token, or synchronous model fallback is required.

**Provider time versus collection delay:** new observations retain the provider's Unix lifecycle timestamps in `remote_*_at` and its request counts. `last_polled_at` records the last read attempt; `remote_observed_at` is the latest successful provider observation. `collected_at` is when this application collected terminal results; legacy `completed_at` remains a local-collection alias, never the provider completion time. Older records lacking provider timestamps cannot establish how much delay occurred at OpenAI versus waiting for a collector. Compare `remote_completed_at` with `collected_at` only when both exist; terminal failures/expiry/cancellation have their own provider timestamps. Read/download/reconciliation failures retain safe diagnostics and resume without creating replacement batches.

**Missing API key:** discovery and durable selection still work, but no OpenAI request is sent. Add the repository secret after checking that queued paid requests are still intended.

**Budget blocked / not ready:** inspect task findings and candidate JSON. Use **AI — Review** for an existing candidate, or explicitly request a failed translation retry. Each is a new manually authorized budget, never an endless automatic loop.

**Interrupted exact-recovery acceptance:** an `acceptance_incomplete` campaign
already owns its full envelope but cannot submit partially staged tasks. To close
it safely, run **AI — Collect and discover**, operation **cancel**, with that recovery
campaign ID. This explicit action audits the immutable request, original records,
and only the exact deterministic children. It records `acceptance_aborted`, cancels
any never-submitted staged children, and retains candidate-only artifacts and
unstaged selections in an auditable partition. Repository validation and normal
later work can then continue. No missing tasks are created, and the full allocation
and every selected original task ID remain permanently consumed.

If the abort itself is interrupted (`acceptance_aborting`), repeat **cancel** for
the same campaign to finish only its recorded cancellation. If a local CLI abort
created a commit but its final push failed, its retry must not claim success while
that commit is unpublished: retain that checkout for audit and rerun cancel from
a fresh `main` checkout (the Actions workflow already uses a fresh checkout). Unexpected batch,
paid-attempt, result, altered-candidate or foreign-history evidence causes a
read-only rejection; inspect the evidence instead of deleting history or resetting
flags. There is no automatic acceptance resume or refund. The equivalent trusted,
durable CLI operation is `python -m berean_translation cancel --campaign <id> --publish`.

**Cancel:** run **AI — Collect and discover**, operation **cancel**, and supply the campaign ID from `STATUS.md`. The runtime checkpoints the owner's request, asks OpenAI to cancel submitted work, and prevents unsubmitted work from starting. OpenAI may charge for requests already completed; cancellation is not a refund. Existing good published translations remain intact.

**submission_unknown:** the runtime persisted submission intent before calling OpenAI, but did not obtain a reliable batch ID. It searches OpenAI batch metadata on later collection runs instead of submitting again. If a matching batch is found it resumes automatically. If no match is found, it continues to wait safely. Only after checking the OpenAI project and confirming no corresponding batch was created should an owner use **resolve-absent**, supply the internal batch ID and check **confirmed_no_remote_batch**. This authorizes one fresh submission; do not use it merely because processing is slow.

**Git write failure:** the next external side effect is blocked until the preceding checkpoint is durable. The failure artifact retains local state for inspection. Check Actions write permission and `main` rules. Never force-push over concurrent work. An uncertain remote submission is recoverable using its already-pushed unique key even when the latest response checkpoint failed.

The discovery/work report refreshes when its source or pending-work snapshot changes, as well as at least once per UTC day. A same-day campaign completion therefore updates pending work immediately. Check Actions if the collector stops, including scheduled-workflow inactivity restrictions.

## Website integration

The future website reads compatible translations from a pinned commit. It does not copy `state/`, instructions, prompts or credentials. From the clean translation checkout, with the English repository checked out at the website's selected source revision:

```bash
python3 -m berean_translation export \
  --source-checkout ../berean-voice \
  --output .build/website-input \
  --base /
```

The English checkout must be clean. The translation exporter computes its own fingerprints directly from that checkout; there is no prerequisite core manifest. Export rejects an uncommitted translation checkout, compares each publication with that fresh source scan, and emits compatible HTML, sidecars, a display index and file hashes. For a Pages project site, supply its actual base path instead of `/`. Image attributes and the notice's English link receive the base prefix without changing matching prose. The site uses shared images from the English archive.

The initial notice links to `/en/articles/<article-uuid>/`. Implement that route in the website or change `config/runtime.json.english_route` **before first publication**. This repository does not deploy a site or invent its future domain. The exporter never erases an existing nonempty output directory.

## Local development and tests

Python 3.11 or later is required. GitHub Actions uses Ubuntu 24.04's Python with a virtual environment. The official OpenAI SDK version is pinned in `requirements.txt`; model and language configuration is reviewed separately.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m berean_translation validate
python -m compileall -q berean_translation tests
```

Tests use artificial articles and simulated API responses, including failures and lost acknowledgements. They do not produce genuine translations or spend money. The SDK resource-contract test needs the installed official SDK; it is skipped in offline environments without it. CI installs the dependency and checks it without a network model call. Real local bare-Git tests exercise durable checkpoints, concurrent enqueuers, conflicts and human-review attribution.

`python -m berean_translation discover --check-only` is a read-only live compatibility check, not a translation. Production collection is `python -m berean_translation tick --publish` on `main`; it requires an authenticated origin push path and an API key for already-authorized paid work. Add `--wait-seconds 600 --poll-seconds 60` for the workflow's bounded pickup behavior (default is still one unrestricted tick). A positive wait budget includes initial work and stops queue acceptance, collection and preparation at resumable boundaries inside a tick. An in-flight operation finishes its durable checkpoints, so reserve extra time for API I/O and final validation. Once submission intent is recorded, the worker records its result or uncertainty before yielding. Prepared batches retain their exact payload, reservation, attempt and uploaded-file identity for the next run. Waits are limited to 900 seconds and poll intervals to 30–300 seconds. All ticks in one window reuse the initial coherent English source scan. Unknown submissions do not keep a runner alive by themselves and are never blindly resubmitted. The CLI rejects real-key collection or maintenance without `--publish`: local-only mode is for tests and credential-free discovery, not production submission.

See [AGENTS.md](AGENTS.md) for agent instructions and [the runtime contract](docs/runtime-contract.md) for invariants. Initial structural tests do not establish real theological translation quality; conduct a small representative, human-reviewed trial before authorizing a large multilingual campaign.

## Website publication

The [Remnant website](https://github.com/trueChristian/remnant.truechristian.church)
checks both source repositories' current `main` revisions on an hourly,
best-effort schedule. It rebuilds when they differ from the last successful
deployment and skips unchanged revisions. Failed deployments remain eligible for
retry on a later check; scheduled start times are not guaranteed. Use **Run
workflow** in the website repository to force a build and deployment immediately.

This repository only maintains and validates its source data. Website export,
build and deployment are owned by the website repository; no website notification
credential or enablement variable is required here.
