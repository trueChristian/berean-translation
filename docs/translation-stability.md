# Translation stability

Ordinary translation and its reviewer default to `gpt-6-luna`; optional improvement defaults to `gpt-6.1-sol`. Manual selection defaults to all languages with free preview enabled. An explicit language list or model choice takes precedence. More languages do not increase a campaign's dollar ceiling. Read [pricing and reservations](model-pricing.md) and [current processing](plain-translation-runtime.md).

## Complete reviews and bounded correction

Reasoning-enabled review requests reserve an 8,192-token completion cap, including hidden reasoning; ordinary nonreasoning review retains its configured base. The model must return complete JSON within its cap. Truncation remains a failure rather than an acceptance shortcut. Caps are frozen at acceptance and cannot authorize spending outside the funded envelope.

When a candidate fails a structural check, the existing single correction receives bounded source/candidate markup excerpts, separate paths, and concrete preservation guidance. The full English article and candidate remain the authoritative inputs. Excerpts are untrusted data rather than application instructions or a license to mechanically transplant wording.

Diagnostics use the strict parser and expose several independent structural problems before consuming the correction opportunity. The output is limited to eight detail findings, 600 characters per quoted excerpt, and 20,000 UTF-8 JSON bytes, with an explicit omission marker. Unsafe or malformed markup receives a bounded diagnostic rather than permissive parsing. The original gate result remains in processing history.

Structural diagnostics protect article identity, image attributes, comments, semantic markup, and complete content. Scripture quotation/reference matching has been removed. Ordinary independent review assesses meaning and language quality at 95, or 98 for accepted-publication improvement. Invalid no-change reviewer suggestions do not establish substantive errors; genuine meaning defects still require correction.

## Durable progress

Fresh translations and saved-candidate recovery have separate scheduling allocations. Every admitted automatic task reserves its complete remaining stages under the existing cumulative authority. Submission checkpoints remain durable while aggregate reports refresh separately to reduce bookkeeping overhead.

Primary pair counts are disjoint: published, unstarted, queued, active, and held without publication. Replacement outcomes and source freshness are separate. A campaign marked finished has stopped processing; it may still contain rejected candidates. Failed improvements leave the accepted publication available.

Offline tests exercise complete stage chains, precise counters, cost/attempt ceilings, lost submission acknowledgments, human-edit protection, and compatible export. They do not measure live translation accuracy or authorize paid work.
