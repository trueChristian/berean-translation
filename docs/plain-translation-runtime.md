# Plain translation policy

Approved October 8, 2026. The current engine translates complete English articles, including quotations and references, with AI and reviews meaning, completeness, and language quality. It performs no Bible-edition matching, quotation alignment, evidence retrieval, or Scripture-specific validation. The website's existing modal retrieves Bible text.

| Operation | Acceptance threshold | Published version during processing |
| --- | --- | --- |
| Initial translation or saved unpublished candidate | 95/100 | None until accepted. |
| Optional improvement of an accepted AI translation | 98/100 | Previous accepted version remains available. |

Scores are review rubrics, not measured percentages of accuracy. A substantive meaning defect, incomplete model response, refusal, malformed or unsafe HTML, changed identity, or missing substantive content still prevents acceptance. Contradictory findings that recommend the existing wording do not establish a substantive defect.

The bounded path is translation, independent review, at most one correction, and final review. A saved failed candidate resumes at review before paying for another translation. A failed improvement leaves public files intact; an accepted improvement archives the previous version before replacing them. Human-edited pairs remain permanently protected from AI work.

OpenAI does not call this repository back. The collector polls stored Batch identities, downloads completed results, advances the permitted stages, and publishes accepted work. Separate scheduled discovery queues new and changed English work. Both workflows share the serialized state writer; manual enqueues persist unique immutable files. See [the operational workflows and commands](../README.md#workflows).

Removing a retired hold does not itself establish quality. Migration retains original queue envelopes, paid payloads, source snapshots, results, findings, attempt counts, funding authorizations, and publication history, with auditable resumed processing. Uncertain provider outcomes and refusals remain explicit holds. Removed Scripture modules, fixtures, YAML, dependencies, and documentation are recoverable from Git history rather than retained as dormant executables.

Each eligible article/language pair counts once as **published**, **unstarted**, **queued**, **active**, or **held without publication**. These categories sum to the eligible total. Replacement attempts and source freshness are separate diagnostics; a failed replacement does not count an accepted pair as unfinished. Reports expose actual collection timing and newly published counts.

The shared cumulative **$30 automatic authority** and separate accepted manual/historical envelopes remain unchanged. Each new automatic envelope reserves its complete remaining stages and remains at most $10. Migration never resets attempts, allocations, or the standing budget. Only complete evidence can settle proven unused automatic headroom; unknown charges retain their reservations.

UUIDs, language folders, public HTML, metadata sidecars, notices, shared image paths, retained accepted publications, and the Remnant export contract remain stable. Read [the runtime contract](runtime-contract.md) for durability, human authority, and export verification.
