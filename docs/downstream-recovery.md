# Downstream recovery: implementation and acceptance plan

This change builds on exact-candidate recovery (PR #7), without modifying its
parent-linked budget policy or any completed campaign history.

## Planned behavior

- Preserve each failed stage, exact candidate, English source snapshot, reviewer
  findings, provider outcome, actual model, usage and action in durable history.
- Select a small bounded set of current-source held translations hourly, or from
  a manual workflow with translation/review model choice.
- Repair using English plus the latest candidate and rejection reasons. Historical
  attempt ordinals stay in audit metadata, outside model input.
- Make one repair request followed by a separate, independent quality review.
  Only a passing review plus all deterministic gates can publish.
- Preserve theology and target-language quotations; assess fidelity, not agreement
  with the author's religious positions. Provider refusals are separately held,
  never an invitation to bypass content filters.
- Protect human-reviewed/public replacements, recheck source fingerprints, avoid
  duplicate active work and cap recovery once per article/language/source version.
- Keep new recovery spending disabled until an owner-approved separate total cap
  is configured. No reset, increase or recycling of existing campaign reservations.
- Use verified stronger models and conservative pricing, retaining frozen historic
  model and prompt settings. Availability and quality still require a paid pilot.

## Acceptance checks

Offline tests must cover happy-path repair/review/publication; rejected, refused,
truncated and invalid results; audit retention; source changes; human protection;
repeated and concurrent requests; hourly deduplication; total and campaign caps;
crash-safe reservations; frozen history; disabled execution; and model cost bounds.
Run the full unit suite, repository validation, workflow YAML checks and CI on the
final commit. A draft PR is not live activation, merging or a paid run.
