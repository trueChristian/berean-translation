# Berean translation

An independent, resumable OpenAI Batch translation runtime for the authoritative English articles in [`trueChristian/berean-voice`](https://github.com/trueChristian/berean-voice). Python and GitHub Actions manage requests, source revisions, translations, quality checks, review history and website-ready exports. There is no database, permanently running server, website framework or image duplication.

**Publication policy:** a translation that passes the automated checks is available for website export immediately, with a localized AI notice. Human review is optional: a collaborator reviews/corrects the translation, removes the complete notice block and commits. English always remains authoritative. Failed candidates are retained for inspection but are never exported as finished translations.

## Activate after merging the implementation

1. Add an Actions repository secret named **`OPENAI_API_KEY`** under **Settings → Secrets and variables → Actions**. Use an OpenAI API project with billing and access to the selected models. Do not put the key in a file, workflow input, issue, pull request or chat.
2. Enable GitHub Actions. The request and collector workflows need `contents: write` in this repository. The workflow files request it explicitly; organization policy or a protected `main` may still prevent the standard Actions token from making the runtime commits. Configure an appropriate permitted automation path rather than disabling protections indiscriminately. No personal token is needed for the supplied public English archive.
3. Run **AI — Collect and discover**, operation **collect**, from `main`. This discovers the current English issues and generates [STATUS.md](STATUS.md). It does not start a paid campaign by itself; discovery also works before the OpenAI key is added.
4. Run **AI — OpenAI** from `main`. Select a language, **next** issue, translation and review models, and a USD budget. Leave **dry_run** checked for a free selection preview. The request is stored immediately; the collector resolves it and records the selection under `state/campaigns/gh-<run-id>.json`.
5. Submit a new manual run with **dry_run** unchecked when ready to translate. The collector starts after a successful request workflow and also runs hourly. Later collector runs retrieve completed batches, submit the bounded review/correction stages and publish passed translations. The runner does not wait for an entire OpenAI batch window.

A dry run is deliberately a selection preview, not a certified price quotation or a translation-quality assessment. Adding the API key later resumes any previously authorized, non-dry-run requests already in the queue. Inspect/cancel those requests before adding the key when their intent has changed.

## Workflows

| Workflow | Purpose | Paid requests |
| --- | --- | --- |
| **AI — OpenAI** | Persist one manual translation request with issue/language/model selections. | The collector submits the authorized work; this enqueuer has no OpenAI secret. |
| **AI — Review** | Request a new bounded AI review of existing translations or saved failed candidates. | Review, and at most one correction plus final review. |
| **AI — Collect and discover** | Discover source updates, process queued requests, collect/resume batches, recognize human review, or perform explicit cancellation/recovery. | Only existing manually authorized campaigns. |
| **Translation runtime checks** | Offline regression tests, installed SDK contract check, repository validation and read-only source compatibility check. | None. |

Manual workflows are intentionally restricted to `main`. Merge the implementation before trying to run production translation work. Do not add an API secret to a pull-request test environment.

### Select multiple languages and issues

The single-language dropdown includes all twenty languages and **all**. The optional **languages** field overrides it with a comma-separated list, for example `afr,deu,spa`. Three-letter folder codes and the registered two-letter/language-tag aliases are accepted. Duplicate aliases for the same language are rejected.

The issue dropdown supplies **next**, **all**, **outstanding** and **custom**. For particular issues, copy one or several `source_id` values or UUIDs from `STATUS.md` / `state/source.json` into the **issues** field, separated by commas; this overrides the preset. The live issue list is discovered from the core repository, not hardcoded into workflow YAML. GitHub's native workflow dropdown cannot dynamically populate from a repository file or select multiple values, so validated list inputs provide those capabilities.

**next** selects the first issue in the source catalogue with eligible work for the selected languages and operation. It does not guess chronology from seasonal dates. **all** and **outstanding** both examine all selected source issues, but normal translation eligibility still excludes completed, active and protected work. A request currently allows up to 1,000 article/language tasks; a larger archive request must be divided across manual runs. Batch sizes have separate safety limits.

Concurrent manual runs create different immutable queue files. OpenAI batches may run simultaneously, while one collector serializes mutable repository writes. Re-running the same GitHub workflow run is idempotent; submitting a new run creates a new request, whose article eligibility checks still prevent duplicate translation charges. Completed translations are selectable for review, not silently translated again. Explicit **retry_failed** is required to retry an unsuccessful translation through **AI — OpenAI**.

### Model selection and expenditure

The default translation and review choice is **gpt-4.1-mini**. This is a cost-conscious starting candidate, **not** a claim that theological translation quality has been proven for all twenty languages. The allowlist also includes **gpt-4.1-nano**, **gpt-4.1** and **gpt-5-mini**, with pinned API snapshot names and explicit Batch prices in `config/models.json`. The smallest model should be benchmarked on representative articles before broad use. Choose a different model for the independent review request when appropriate.

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
state/heartbeat.json          Daily real source-discovery report
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

The hourly collector discovers new issues and articles automatically and makes them selectable. It also updates the observed source revision and identifies stale/withdrawn translations. **Discovery never authorizes paid work.** Use **next** or **outstanding** in a manual translation request for new eligible work.

Source text, markup and translation-metadata fingerprints govern compatibility. An image pixel replacement or unrelated category edit alone does not require retranslation. Relevant English changes mark older translations stale without deleting them or overwriting human corrections. Each requested campaign retains its exact source commit and source snapshots.

## Recovery and cancellation

**No progress yet:** inspect the collector workflow and `STATUS.md`. A submitted OpenAI batch may still be processing. The worker checks status and exits; manually running **collect** is safe and does not create a duplicate campaign. Scheduled runs are subject to GitHub scheduling availability, not a guaranteed completion deadline.

**Missing API key:** discovery and durable selection still work, but no OpenAI request is sent. Add the repository secret after checking that queued paid requests are still intended.

**Budget blocked / not ready:** inspect task findings and candidate JSON. Use **AI — Review** for an existing candidate, or explicitly request a failed translation retry. Each is a new manually authorized budget, never an endless automatic loop.

**Cancel:** run **AI — Collect and discover**, operation **cancel**, and supply the campaign ID from `STATUS.md`. The runtime checkpoints the owner's request, asks OpenAI to cancel submitted work, and prevents unsubmitted work from starting. OpenAI may charge for requests already completed; cancellation is not a refund. Existing good published translations remain intact.

**submission_unknown:** the runtime persisted submission intent before calling OpenAI, but did not obtain a reliable batch ID. It searches OpenAI batch metadata on later collection runs instead of submitting again. If a matching batch is found it resumes automatically. If no match is found, it continues to wait safely. Only after checking the OpenAI project and confirming no corresponding batch was created should an owner use **resolve-absent**, supply the internal batch ID and check **confirmed_no_remote_batch**. This authorizes one fresh submission; do not use it merely because processing is slow.

**Git write failure:** the next external side effect is blocked until the preceding checkpoint is durable. The failure artifact retains local state for inspection. Check Actions write permission and `main` rules. Never force-push over concurrent work. An uncertain remote submission is recoverable using its already-pushed unique key even when the latest response checkpoint failed.

The daily discovery report records actual observed source revision/counts and pending work; it is not a meaningless counter. Check Actions if the collector stops, including scheduled-workflow inactivity restrictions.

## Website integration

The future website reads compatible translations from a pinned commit. It does not copy `state/`, instructions, prompts or credentials. From the clean translation checkout, with the English repository checked out at the website's selected source revision:

```bash
python3 -m berean_translation export \
  --source-manifest ../berean-voice/manifest.json \
  --source-revision "$(git -C ../berean-voice rev-parse HEAD)" \
  --output .build/website-input \
  --base /
```

The English checkout must be clean and its manifest validated by that repository's own archive tools. Export rejects an uncommitted translation checkout, compares each publication with the selected English manifest, and emits compatible HTML, sidecars, a display index and file hashes. For a Pages project site, supply its actual base path instead of `/`. Image attributes and the notice's English link receive the base prefix without changing matching prose. The site uses shared images from the English archive.

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

`python -m berean_translation discover --check-only` is a read-only live compatibility check, not a translation. Production collection is `python -m berean_translation tick --publish` on `main`; it requires an authenticated origin push path and an API key for already-authorized paid work. The CLI rejects real-key collection or maintenance without `--publish`: local-only mode is for tests and credential-free discovery, not production submission.

See [AGENTS.md](AGENTS.md) for agent instructions and [the runtime contract](docs/runtime-contract.md) for invariants. Initial structural tests do not establish real theological translation quality; conduct a small representative, human-reviewed trial before authorizing a large multilingual campaign.
