# Translation runtime contract 1.0

The English archive, translations, and website are independent repositories. A translation is identified by `(language, English article UUID)`. No image or category catalogue is duplicated. Language selection uses three-letter folders while website routing uses the registry's language tags; the website owns navigation, category translations, flags/icons, search and SEO markup.

## Publication

`content/<language>/articles/<uuid>.html` is the translated source fragment followed by one independent AI-notice aside. Its adjacent JSON file has translated title/subtitle/section. The HTML article itself has the same UUID, element structure and immutable attributes as the source. Alt/title attributes may be translated; URLs, IDs, classes and meaningful element structure may not.

`index.json` describes public translation records. `human_reviewed: false` is not a publication blocker. `status: ready` means the last accepted translation matches the observed English translation fingerprint; human-reviewed and AI-unreviewed articles are both allowed. A removed source or stale source is not exported. Source revision, actual translation/reviewer models and output fingerprints remain traceable.

Failed or pending candidates live under `state/tasks`, not public `content`. A failed re-review never removes an existing good version. AI suggestions for a human-reviewed article remain proposals until a human applies them. These proposals include full candidate JSON and findings; applying changes to the existing reviewed HTML/metadata is a normal human commit, without adding an AI notice or silently changing provenance.

Human review is recorded only after a human repository commit changes an existing publication and removes its complete aside. Git history supplies attribution. This records a collaborator's review action, not an independent certification of linguistic accuracy. Initial localized notices are supplied in configuration and should themselves be reviewed by native-language editors.

## State and durability

1. Manual Actions workflows persist unique immutable queue requests, without a shared enqueuer concurrency group.
2. The collector checks out current `main`, synchronizes human review, resolves upstream `main` to a commit, verifies raw index/catalogue bytes against the source manifest, and discovers eligible articles.
3. It resolves issue selectors from that snapshot, skips processed/active/protected combinations, freezes model settings/prompts/glossaries, and creates durable tasks and hash-named source snapshots.
4. For each stage it prepares single-model JSONL batches, reserves conservative token-cost ceilings, and pushes the reservation before calling OpenAI.
5. It persists the uploaded file ID, then submission intent, then calls Batch creation once with retries disabled. A lost response remains `submission_unknown`; subsequent ticks search Batch metadata instead of making another paid request.
6. Later collector runs download completed output/error files and match every result by `custom_id`, never line order. Partial expiry preserves successful items; missing items fail closed without automatic billed retries.
7. A passed review publishes immediately. A failed first review allows one correction and final review. A final failure remains `not_ready`.

Every checkpoint refreshes the publication index/status in the same commit. Batch work can run concurrently at OpenAI; repository mutation is serialized. An enqueuer or human may move main during a worker checkpoint. Disjoint changes are rebased without force; overlapping file edits halt safely. An in-progress request with a lost remote ID can be recovered through its persistent submission key even when an artifact is unavailable.

## Limits and expenditure

The configured model list is an explicit allowlist of dated model snapshots and Batch token rates. The default is a cost-conscious `gpt-4.1-mini` candidate, not a claim that it is the cheapest model meeting a proven translation-quality benchmark. Lower-cost `gpt-4.1-nano`, `gpt-5-mini` and higher-capability `gpt-4.1` choices remain selectable. The list is deliberately not an unvalidated automatic model-discovery mechanism.

A campaign records its USD cap, conservative reserved cost and API-reported token-usage estimate. Input estimates use UTF-8 bytes plus a framing allowance; output tokens are explicitly limited. Reservations are never silently released and reused. A subsequent stage that cannot fit the remaining cap becomes `budget_blocked` and does not call OpenAI. Selecting a very large batch with too small a budget can therefore stop after translation but before review; the saved candidate can be selected in **AI — Review** without paying to translate it from scratch. Caps are application safeguards based on the configured rate table, not a provider billing guarantee. Configure OpenAI project/account budgets and alerts as an independent safeguard, verify whether they impose a hard cap rather than merely notifying, and review rates before a large campaign.

No more than two translations/corrections and two reviews occur per task. No billable endpoint is automatically retried. File uploads have a separate three-failure bound. Manual re-review/retry is a new, explicitly authorized bounded campaign. Source discovery never starts new paid work automatically.

## Source compatibility

The source reader consumes archive format 2.0: `index.json.articles`, `catalogue.json.issues`, `manifest.json.articles`, and `content/articles/<uuid>.html`. Index/catalogue fingerprints are SHA-256 of the raw files, matching `tools/archive.py` in the source repository. Each downloaded HTML file must match the corresponding raw HTML hash and image list.

Compatibility uses the source text, structure and translation-metadata fingerprints. Shared image pixel replacements do not incur a new translation. A wording change, structural change, or translated metadata change marks the previous translation stale. Conservative invalidation is intentional; this version does not silently transplant reviewed prose into changed markup. Snapshot caches are not independently authoritative and must never be edited.

## Website export

The website must pin both repository checkouts. From the clean translation checkout:

```bash
python -m berean_translation export \
  --source-manifest ../berean-voice/manifest.json \
  --source-revision "$(git -C ../berean-voice rev-parse HEAD)" \
  --output .build/website-input \
  --base /
```

For a Pages project path use the actual site's base, for example `--base /articles/`. The export includes translated HTML/sidecars, a display index, and a file-hash manifest. It contains no state, prompts, configuration, snapshots, skipped audit records, or images. The website uses the English repository's shared images and canonical article/group associations. It should use the exported translated `images[].alt` when generating image accessibility metadata.

Export compares every publication with the **selected** English manifest, not merely the translation repository's most recent poll. Incompatible, removed, unready and failed candidates are omitted. Output is assembled in a temporary sibling directory and promoted only when complete. Existing nonempty destinations are never erased. Base-path rewriting affects actual image attributes and the application's English-link attribute, not matching text inside article prose.

The configurable initial English route is `/en/articles/{article_id}/`. The website must implement that route or update `config/runtime.json.english_route` before initial publication. No live website URL is invented here.

## Provider and platform references

- OpenAI Batch lifecycle, ordering, expiration and 50% Batch rates: https://developers.openai.com/api/docs/guides/batch
- Official Python SDK: https://github.com/openai/openai-python
- Token pricing (recheck before production bulk work): https://developers.openai.com/api/docs/pricing
- Model snapshot/capability references: https://developers.openai.com/api/docs/models/gpt-4.1-mini and the corresponding configured model pages.
- GitHub manual inputs and concurrency behavior: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax

Batch stage completion can take up to its provider processing window. The workflow runner never waits for that window: it submits/checkpoints/exits, and a later collection run resumes. The hourly collector must remain enabled; public-repository scheduled workflows can be disabled after inactivity, so check Actions if collection stops. Successful state checkpoints are real commits. A once-per-UTC-day report in `state/heartbeat.json` records actual successful discovery counts, source revision and pending tasks; do not add meaningless keepalive changes.
