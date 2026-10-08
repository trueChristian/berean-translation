# Model pricing and request defaults

Verified against official OpenAI documentation on **2026-10-02**. Rates below
are USD per million tokens for **Batch**, using the ordinary global API endpoint.
Regional/FedRAMP premiums, synchronous processing, fast tiers, tools, and media
are outside this text-only Batch adapter's scope.

| Registry model / API ID | Input | Cached input | Cache writes | Output |
| --- | ---: | ---: | ---: | ---: |
| `gpt-6-luna` | $0.05 | $0.005 | $0.0625 | $0.25 |
| `gpt-6.1-sol` | $1.00 | $0.05 | $1.25 | $5.00 |
| `gpt-6-astra` | $5.00 | $0.50 | $6.25 | $25.00 |

For **more than 272,000 input tokens**, double all three input/cache rates and
multiply output rates by 1.5 **for the entire request**, not just the excess.
Exactly 272,000 input tokens remains in the short tier. Cache writes use their
own input category; they are not an extra fee added to ordinary input billing.
Sources: [API pricing](https://developers.openai.com/api/docs/pricing) and
[prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

## Verified request contract

All three models support a 1,050,000-token context and at most 128,000 output tokens.
The documented API IDs above are used as published; the pages do not supply
dated snapshots to pin. The registry records `reasoning_effort: low` for each.
All support `low`, `medium`, `high`, `xhigh`, and `max`; Luna also supports `none`.
Sources: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra).

This repository keeps Batch requests to `/v1/chat/completions`, with no tools,
`reasoning_effort`, `max_completion_tokens`, and the existing strict JSON-schema
`response_format`. Chat Completions is supported for these text-only requests;
tool calling at the selected `low` effort would require Responses. There is no
synchronous fallback. The completion cap includes reasoning as well as visible
output. Sources:
[migration guide](https://developers.openai.com/api/docs/guides/latest-model#update-api-and-model-parameters)
and [Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
The [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
documents the strict JSON-schema response format, and the
[Batch guide](https://developers.openai.com/api/docs/guides/batch) documents batched
Chat Completions requests.

## Defaults and bounded alternatives

Ordinary translation and its independent reviewer default to `gpt-6-luna`.
Optional improvement of published AI translations defaults to `gpt-6.1-sol`
for generation and validation. Both manual workflows offer the configured
model registry, with explicit selections preserved in accepted requests.
The specialized manual repair and exact-candidate-recovery workflows are removed.

Luna's short-context uncached input rate is 60% lower and its output rate 75% lower
than the existing GPT-5 mini Batch registry rates; this compares configured rates
rather than predicting total spend or quality. The ordinary translation/correction
cap is 16,384 tokens and the reasoning-review cap is 8,192, including reasoning.
Existing funded downstream campaigns retain their 32,768-token generation and
8,192-token review caps, original model settings, rates, and dollar ceilings.

A stronger model does not establish improved source fidelity by itself. Initial
translations use 95 and optional accepted-publication improvements use 98, with
meaning and technical validity required. Current defaults do not reprice old
requests, reset paid history, or increase the standing budget.

## Conservative reservation and reported usage

`build_request` bounds input using serialized UTF-8 bytes plus 4,096 tokens of
framing allowance. It rejects a request when that bound plus the output cap
exceeds the model context. `reserve_cost` uses the highest applicable input
category, including cache writes, and the full output cap, then rounds upward to
one microdollar. If the input bound crosses the long-context threshold, the
entire reservation uses the higher tier even if actual tokenization is shorter.
Cache hits and shorter responses never justify spending beyond the reserved
campaign budget. The estimator assumes the verified rates; it cannot guarantee
future vendor pricing.

`usage_cost(model, usage)` prices Chat Completions `prompt_tokens` and
`completion_tokens`. When a valid `prompt_tokens_details` breakdown is supplied,
it subtracts `cached_tokens` and `cache_write_tokens` from the ordinary share and
prices each once. Reasoning tokens are already in `completion_tokens`. Missing
cache counts are conservatively bounded; malformed or contradictory cache
counts receive no discount. Missing, negative, noninteger, or boolean total
counts return `None` rather than a false zero. Thus reported usage may remain an
upper estimate, and is not an invoice or permission to release reservations.

Campaign acceptance copies the registry into the campaign/task records. All
costing uses those frozen entries, so a later registry change cannot silently
reprice accepted work. Historical entries without cache/tier fields keep their
original conservative input/output calculation. The four older registry entries
are unchanged. Already accepted `gpt-5-mini` source-refresh envelopes retain their original
models and limits. New source refreshes use the canonical autonomous scheduler
under the same shared authority as new translations and saved-candidate recovery.

Offline tests cover cache mixes, tier boundaries, rounding, reasoning accounting,
missing usage, frozen campaigns, context rejection, and blocking before upload
when the request does not fit its budget. No model access or paid API call is
needed to run them. Documentation verifies the public contract, not access for
any particular OpenAI project.
