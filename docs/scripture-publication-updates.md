# Offline completed-publication updates v1

`berean_translation.scripture_publication_updates` adds an executable, durable
**offline-only** lifecycle for updating an exact completed AI publication using
component evidence v2. It is separate from the never-paid admission-revision
journal and from live downstream recovery. It never writes repository `State`,
changes the accepted publication, submits a Batch, allocates money, activates v2,
or grants publication authority. Existing historical contracts are unchanged.

## Entry and predecessor contract

`inspect_completed_publication(engine, language, article_id, predecessor_task_id)`
reads the existing runtime state without changing it. Admission requires:

- The exact predecessor task is `complete`, is the record's latest task, and owns
  the current publication. It has no active batch or human protection, is at a
  review stage, and retains valid bounded completed-task attempt counters.
- Language, article, issue, source snapshot and English content key agree across
  task, publication and source. The currently observed English register still
  contains that same article/issue/content key. An unrelated English repository
  revision does not change the pinned source or reset this lineage.
- Publication paths are canonical for the article/language. Working HTML and
  metadata match the accepted hashes. The archived predecessor candidate agrees
  with the accepted candidate, allowing only the existing publication boundary's
  leading/trailing HTML whitespace normalization.
- Permanent human-review history, human flags, editorial issues, unrecognized
  working edits, pending manual-admission claims, active/orphan pair tasks,
  uncertain or unresolved batch histories, missing files and inconsistent
  provenance block entry. The engine's existing human-protection guard is reused.

The immutable origin records the exact source, predecessor task, entire campaign,
record, publication, raw archived candidate, normalized public candidate, public
HTML/metadata, model and complete related task/batch inventory hashes. Full
original campaign/task/record/publication copies, exact accepted publication
payload and related task/batch inventory remain in the journal. Live inspection and archived replay use the same pure semantic
anchor validator, including publication payload hashes and canonical paths.
Original paid reservations, usage, attempts, findings, source snapshot and history are never reset or rewritten. Even an
unrelated change to the original campaign after proposal conservatively blocks
new update work until it can be separately reconciled.

This stage only handles the exact latest completed publication on unchanged
English. It does not choose a predecessor from failed subsequent work, update a
stale English publication, or create an automatic sequence of successor cycles.

## Public API

Create `OfflinePublicationUpdateStore(private_root, repository_root)` outside the
live repository, in a dedicated directory. Its methods are:

- `propose(engine, language, article_id, predecessor_task_id, scripture_policy,
  evidence, protocol_root, funding, *, expected_head=None, fault=None)` binds all
  inputs, checks fresh evidence, and appends one complete-cycle proposal.
- `request(engine, update_id, *, expected_head=None, fault=None)` persists the exact
  next Batch-format request **preview** before returning it. Repeating a pending
  request returns the same preview and does not consume another attempt.
- `receive(engine, update_id, row, *, expected_head=None, fault=None)` validates and
  durably records a supplied response and its replayed outcome. It makes no model
  call. Exact repeated responses are idempotent; conflicting replacements cannot
  overwrite history. Oversized/unarchivable responses durably hold the cycle
  without storing invented evidence or allowing another attempt.
- `accept(engine, update_id, *, expected_head=None, fault=None)` appends an
  `accepted_offline` proof only after a new independent complete review, current
  predecessor/source/human/publication checks, and fresh component evidence.
- `cancel(update_id, reason, *, expected_head=None, fault=None)` appends a bounded
  cancellation. It does not free the predecessor slot or projected allocation.
- `inspect(*, expected_head=None)` replays the journal and returns `events`, `head`,
  `updates`, predecessor claims, funding contexts and projected allocations.

`propose` returns `update_id` and an item. Items in `updates[update_id]`, and the
results of `receive`/`accept`/`cancel`, contain `proposal`, `status`, `cycle` and
`acceptance`. The cycle is a complete processing snapshot. Store statuses are
`proposed`, `processing`, `held`, `review_accepted_offline`, `accepted_offline`, or
`cancelled`. Only a passing fresh review can reach `review_accepted_offline`; that
is still distinct from the journal's final rechecked `accepted_offline` event.

`OfflinePublicationUpdateCycle` supplies an independently replayable processor.
Its constructor accepts source, evidence, Scripture policy, processing contract,
and a versioned publication-update context. Its request/receive/snapshot/restore
methods retain the existing bounded component-processing behavior. Using that
in-memory class alone does not confer durable lineage or funding authority.

## Separate unfunded planning envelope

The caller must provide exactly this funding projection shape:

```json
{
  "id": "separate-offline-update-plan",
  "currency": "USD",
  "budget_usd": 5,
  "reserved_usd": 0,
  "reported_usage_usd": 0,
  "offline_only": true,
  "funding_authorized": false
}
```

This is **a planning input, not user spending approval, new money or an accepted
campaign envelope**. It must use a separate identity from the predecessor's paid
campaign. It cannot be automatically adopted live. Real future funding requires
a separately authorized successor contract and durable live admission; neither
is implemented here.

The child preview uses distinct synthetic campaign and task identities. Its
models, exact API model names, frozen prices, base prompts, language settings,
glossary, quality threshold and output limits come from the completed campaign.
The proposed child campaign contains no copied predecessor budget/reservation.
Review contract v2 and component policy v2 apply only to this new child; historical
review and Scripture contracts retain their original meaning.

A conservative complete two-generation/two-review ceiling is projected before
admission. It uses frozen model context/output bounds and both sides of pricing
thresholds. All proposals with the same funding identity share its immutable
projection; the larger of reserved/reported amounts remains consumed. No usage,
failure, cancellation or acceptance refunds a projection. Changing a funding
identity, policy, evidence or prompt does not allow a second child of the same
completed predecessor in the retained journal.

## Bound requests and fresh independent review

Every generation, correction and review preview includes the exact raw completed
predecessor candidate and origin hashes under `completed_publication_update`.
The context hash and request binding cover that predecessor, source, evidence,
new child contract, model, schema and complete input. The ordinary candidate hash
in the stage binding refers to the newly generated candidate; the separately
bound predecessor is never confused with it. Full candidates/evidence are never
truncated to fit a context or byte ceiling.

The only update chain is generation → independent full review → at most one
correction → final independent full review. The existing 2+2 ceiling applies to
the new child, while original completed-task counters remain immutable audit.
A review request contains no old verdict, score, findings or acceptance rationale.
It includes the whole authoritative article, whole new candidate, predecessor,
component evidence and deterministic selection proof. Independence means a new
review request and complete rubric, not necessarily a different model family.

A first rejection can justify one correction only with complete actionable
findings. Refusal, invalid output, incomplete review, model mismatch, changed
bindings, unsafe HTML or a failed final review holds the cycle. Prior approvals
cannot be reused for any changed candidate. Final acceptance records the exact
review request/response, new candidate/proof, cycle and retained-publication
hashes. It sets both funding and publication authorization to false.

The supplied provider-shaped rows are offline test inputs, not authenticated
provider receipts. Fresh real receipt collection and immutable association must
be implemented before any live acceptance can rely on this protocol.

## Journal, crashes and concurrency

The new event contract is separate: `proposed`, `requested`, `responded`,
`unarchivable_response`, `accepted_offline`, `cancelled`. Only the safe filesystem
primitives are shared with the never-paid admission journal: bounded canonical
hash-linked events, exclusive filesystem locking, inode/path checks, fsynced
write-ahead temporary files, atomic no-overwrite linking and directory fsync.
Requests, responses, complete projected allocations and outcomes are replayed,
not inferred from mutable summary files. Failed/temporary writes do not count as
events. A committed event survives a lost acknowledgement and exact retries do
not allocate a second cycle or consume another logical request.

Every new event validates its transition before append. For proposals, requests,
responses and acceptance, current predecessor, human, source and publication
guards run at entry and immediately before and after event commit. Cancellation
needs no current-State guard, so drift cannot prevent safely stopping an offline
cycle. A drift discovered after commit leaves immutable offline
history, but prevents further work; it cannot alter the last accepted article.
Cancelled/accepted terminal history cannot be rewritten. Reordered, missing,
malformed, oversized or semantically inconsistent events fail closed.

Callers should retain the returned head and supply `expected_head` for subsequent
operations. New mutations require the current head; exact lost-acknowledgement
retries may use a retained ancestor. A retained head missing from the journal
reveals truncation. A local journal alone cannot prove its own deleted tail or
prevent someone creating a different empty journal. This is intentionally not an
authoritative live distributed ledger. An external retained head and future live
admission controls are required before real spending/publication.

Read-only replay allows unchanged expired evidence without renewing its TTL. New
request construction and final acceptance require fresh evidence. Interrupted
work does not gain new attempts or refreshed evidence by reopening the store.

## Publication retention and remaining live work

The accepted article is retained in every outcome, including `accepted_offline`.
There is no publication API here. Even after all offline gates pass, this module
cannot write a candidate, notice, record, campaign, task or publication to live
State. Human work cannot be overridden, and all existing live v2 holds remain.

Still separate work: explicit live successor funding and authorization, durable
reservation/materialization, exact prepared request/upload checkpoints,
uncertain-create reconciliation, authenticated result collection, final live
human/source/publication guards and atomic last-good replacement. The offline
journal's acceptance cannot bypass any of these requirements.
