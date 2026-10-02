# Bounded translation stability improvements

This change extends PR #9's reasoning-review headroom work. It does not retry old
campaigns, relax publication gates, authorize new spending, or certify a model's
linguistic/theological quality.

## Defaults and cost

Ordinary Translate and Review now default to all configured languages, with free
preview still the default. An explicit language or comma-separated override still
wins. Exact-task recovery and bounded held recovery do not acquire broad language
selectors. Campaign/task limits and explicit dollar caps are unchanged; selecting
more languages does not increase a campaign's budget.

The newer affordable ordinary model/reviewer default is `gpt-6-luna` with low
reasoning. All model pickers include it; stronger downstream repair/review retain
`gpt-6.1-sol`. Existing source-refresh policy retains its approved `gpt-5-mini`
models. Registry/request compatibility and conservative pricing are tested
against the documented API contract, but account access and Berean quality are
unverified by paid calls. See [pricing](model-pricing.md). Existing campaigns keep
the model, prompts, output limits and prices frozen at their original acceptance.

New reasoning-review campaigns reserve an 8,192-token total completion budget,
including hidden reasoning, while nonreasoning reviews retain 3,000. A larger cap
can prevent some reasoning-only truncation, but cannot guarantee visible JSON.
The existing stage reservation still refuses a request that cannot fit its
campaign envelope. More headroom does not raise the dollar cap.

## Structural correction evidence

The strict HTML signature, attribute, reference, metadata and semantic gates are
unchanged. When a new version-1 campaign's initial translation fails a structural
check, its existing single correction gets several bounded source/candidate
markup excerpts, separate source and target paths, and concrete preservation
guidance. Multiple errors are exposed before using the only correction attempt.
The full English and candidate remain authoritative input; excerpts are untrusted
context, not instructions, literal text alignment or permission to insert markup
mechanically. Emphasis belongs around the corresponding target-language meaning.

Diagnostics use the same strict parser, align signatures within structural text
blocks, and report at most eight detail findings plus an explicit omission marker.
Quotes are at most 600 characters each and the entire findings JSON is at most
20,000 UTF-8 bytes. HTML/event work is bounded too. Malformed/forbidden markup
gets a bounded fallback diagnostic; it is never parsed permissively or accepted.
The original gate decision and the diagnostic findings are retained in audit.

`structural_feedback_version` is frozen when a campaign is accepted. Older
campaigns without that version continue to build their original correction
feedback/request bytes. Existing saved batch payloads and task history are not
rewritten. No extra model call, correction or automatic markup transplant is added.

Regression fixtures are short authentic source/translation excerpts, with original
file hashes and task/source/result paths. They include missing Nero emphasis,
Mandarin A Call to Holiness's separated strong/em omissions, lost caption breaks,
missing superscript and split emphasis. These prove the diagnostics cover observed
failures and keep rejecting them. They do not prove improved live acceptance.

## Completion reporting and recovery

`finished` remains the durable terminal-processing state used by existing recovery
contracts. The generated status report now explicitly separates processing state
from task outcomes (complete, held, active, proposal, cancelled, unknown). A
finished campaign with held work is not described as wholly successful. Current
source-compatible publication readiness remains in the issue/language tables;
website deployment must be verified separately.

The report also states whether new downstream recovery is paused or whether the
next hourly envelope is budget blocked. Hourly recovery remains disabled until a
separately approved total cap is configured. Explicit main-branch manual repair
runs use their own one-time ceiling and report those allocations separately.
Once authorized, eligible held pairs
have one bounded repair plus independent review, not an unlimited retry loop.
Every accepted envelope remains allocated after failure/cancellation. Policy
refusals, unknown legacy outcomes, changed source, and exhausted source attempts
remain visible holds requiring owner attention.

Offline whole-issue tests exercise all-language work, retained holds, explicitly
funded mocked repair, independent review and repository publication. Budget-blocked
runs, failed re-review retaining a prior publication, and append-only report
rendering are covered. Mock outputs do not measure translation accuracy; no paid
run or production activation is part of these changes.
