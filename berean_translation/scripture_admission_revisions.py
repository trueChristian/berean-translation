"""Append-only OFFLINE revisions of provably unmaterialized manual holds.

This private journal never writes repository State, allocates funding, admits a
task or submits a provider request. Its conservative projections share the exact
original envelope; they are not new authority. Live v2 guards remain closed.
"""
from __future__ import annotations

import copy
import fcntl
import math
import os
from pathlib import Path
import re
import stat
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

from . import manual_admission
from .common import ContractError, canonical, digest, json_hash, loads
from .requests import reserve_cost
from .scripture_component_evidence import validate_component_evidence
from .scripture_component_processing import (OfflineComponentCycle, processing_contract,
                                             validate_processing_contract)

VERSION = '1'
MAX_EVENT_BYTES = 8000000
MAX_JOURNAL_BYTES = 64000000
MAX_EVENTS = 256
ZERO = '0' * 64


def _same(left, right):
    return canonical(left) == canonical(right)


def _money(value):
    if type(value) not in (int, float, str):
        raise ContractError('Unknown original funding amount')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or not math.isfinite(float(result)):
            raise ValueError('Invalid amount')
    except (ValueError, ArithmeticError) as exc:
        raise ContractError('Invalid original funding amount') from exc
    return result


def inspect_never_paid_hold(engine, campaign_id, entry_id):
    """Read existing write-ahead histories; absence of one task.json is not enough."""
    state = engine.state
    campaign = state.read(f'state/campaigns/{campaign_id}.json')
    ledger = state.read(manual_admission.path(campaign_id))
    if not campaign or not ledger or not manual_admission.supported(campaign):
        raise ContractError('No supported original manual admission contract')
    manual_admission.validate(state, campaign, ledger)
    if 'cancel_requested' in campaign and type(campaign['cancel_requested']) is not bool:
        raise ContractError('Original cancellation state is unknown')
    if campaign['operation'] != 'translate' or campaign.get('cancel_requested'):
        raise ContractError('Only uncancelled manual translation holds can be revised offline')
    entry = ledger['entries'].get(entry_id)
    if (not entry or entry['status'] != 'attention' or not entry.get('provenance')
            or entry.get('ready_fields') is not None):
        raise ContractError('Revision requires an unresolved, unmaterialized attention entry')
    proof, task = entry['provenance'], entry['provenance']['task']
    if (proof['previous_task_id'] is not None or proof['candidate_path'] is not None
            or task['stage'] != 'translate' or task['status'] != 'queued' or task.get('protected')
            or any(type(task.get(key)) is not int or task[key] != 0
                   for key in ('translation_attempts', 'review_attempts')) or task.get('batch')):
        raise ContractError('Paid/previous/publication/review work is outside never-paid admission revision')
    task_directory = state.path(f'state/tasks/{entry_id}')
    if task_directory.exists() or task_directory.is_symlink() or entry_id in campaign.get('tasks', []):
        raise ContractError('Original task was materialized or reserved; never-paid proof is absent')
    # This deliberately excludes terminal orphan tasks too, not just active ones.
    tasks = state.tasks()
    known_tasks = {}
    for existing in tasks:
        if (not isinstance(existing, dict) or any(not isinstance(existing.get(key), str) or not existing[key]
                for key in ('id', 'campaign', 'language', 'article_id', 'status'))
                or existing['id'] in known_tasks):
            raise ContractError('Task inventory is incomplete or malformed')
        known_tasks[existing['id']] = existing
        if (existing['id'] == entry_id or
                (existing.get('language'), existing.get('article_id')) == (task['language'], task['article_id'])):
            raise ContractError('Existing task history prevents a never-paid pair proof')
    batches = state.batches()
    for batch in batches:
        if (not isinstance(batch, dict) or not isinstance(batch.get('tasks'), list)
                or not isinstance(batch.get('id'), str) or not batch['id']
                or not isinstance(batch.get('campaign'), str) or not batch['campaign']
                or any(not isinstance(identity, str) for identity in batch['tasks'])):
            raise ContractError('Batch inventory is incomplete or malformed')
        if entry_id in batch['tasks']:
            raise ContractError('Prepared, submitted or uncertain batch refers to the original hold')
        if any(identity not in known_tasks or known_tasks[identity]['campaign'] != batch['campaign']
               for identity in batch['tasks']):
            raise ContractError('Batch task history is unresolved or belongs to a different campaign')
    campaign_batches = {batch.get('id'): batch for batch in batches if batch.get('campaign') == campaign_id}
    if len(campaign_batches) != sum(batch.get('campaign') == campaign_id for batch in batches):
        raise ContractError('Duplicate original campaign batch identity')
    reserved_total = Decimal(0)
    for identity, batch in campaign_batches.items():
        if not isinstance(identity, str) or not identity:
            raise ContractError('Incomplete original campaign batch identity')
        amount = _money(batch.get('reserved_usd'))
        parent_id = batch.get('reservation_reused_from')
        if parent_id is not None and not isinstance(parent_id, str):
            raise ContractError('Malformed original reservation parent identity')
        if not parent_id:
            reserved_total += amount
        else:
            parent = campaign_batches.get(parent_id)
            if (not parent or parent.get('replacement_batch') != identity
                    or parent.get('status') != 'cancelled_before_submission'
                    or parent.get('exclusion_reason') != 'human_editorial_authority' or parent.get('remote_id')
                    or (parent.get('submission_started_at') and parent.get('create_not_called') is not True)
                    or any(not _same(batch.get(key), parent.get(key)) for key in ('campaign', 'stage', 'model', 'reserved_usd'))
                    or not isinstance(parent.get('excluded_task_ids'), list)
                    or not parent['excluded_task_ids'] or not 0 < len(batch['tasks']) < len(parent['tasks'])
                    or batch['tasks'] != [value for value in parent['tasks'] if value not in parent['excluded_task_ids']]):
                raise ContractError('Original batch reservation reuse is not proven unsubmitted')
        replacement = batch.get('replacement_batch')
        if replacement is not None and not isinstance(replacement, str):
            raise ContractError('Malformed original reservation replacement identity')
        if replacement and (replacement not in campaign_batches
                or campaign_batches[replacement].get('reservation_reused_from') != identity):
            raise ContractError('Incomplete original campaign reservation replacement')
    if reserved_total != _money(campaign.get('reserved_usd', 0)):
        raise ContractError('Original campaign reservation inventory is inconsistent; reconcile before revision')
    source, record = manual_admission._guard(engine, campaign, entry)
    if record.get('published') or record.get('latest_task') or record.get('history'):
        raise ContractError('Publication or prior pair history is outside this revision path')
    budget = _money(campaign['budget_usd'])
    # Manual reservations are permanent; reported usage must never reduce them.
    consumed = max(_money(campaign.get('reserved_usd', 0)), _money(campaign.get('reported_usage_usd', 0)))
    if consumed > budget:
        raise ContractError('Original manual envelope is already over its ceiling')
    origin = {'campaign_id': campaign_id, 'entry_id': entry_id,
        'request_sha256': ledger['request_sha256'], 'campaign_contract_sha256': ledger['campaign_sha256'],
        'entry_sha256': json_hash(entry), 'attention_event_sha256': json_hash(entry['events'][-1]),
        'source_sha256': json_hash(source), 'record_sha256': json_hash(record),
        'task_template_sha256': json_hash(task), 'models_sha256': json_hash(campaign['models']),
        'budget_usd': campaign['budget_usd']}
    return {'origin': origin, 'campaign': campaign, 'entry': entry, 'source': source,
            'consumed_usd': str(consumed)}


def complete_cycle_ceiling(processing):
    """Reserve the entire possible 2+2 preview chain, never just its first stage.

Maximum admissible model context is deliberately conservative. Check both sides
of a long-context pricing boundary so even a discount multiplier cannot undercut
the bound. Actual request previews independently reject context overflow.
"""
    validate_processing_contract(processing)
    stages, total = {}, Decimal(0)
    for kind, model_key, limit_key in (('generation', 'generation_model', 'max_output_tokens'),
                                       ('review', 'review_model', 'review_output_tokens')):
        model = processing['models'][processing[model_key]]
        output = min(processing[limit_key], model['max_output_tokens'])
        maximum_input = model['context_tokens'] - output
        if maximum_input <= 4096:
            raise ContractError('Frozen model cannot fit a complete component stage')
        points = {0, maximum_input}
        threshold = model.get('long_context_threshold_tokens')
        if threshold is not None:
            points.update((min(maximum_input, threshold), min(maximum_input, threshold + 1)))
        amount = max(Decimal(str(reserve_cost(model, number, output))) for number in points)
        if not amount.is_finite() or amount < 0:
            raise ContractError('Invalid complete-cycle projected ceiling')
        stages[kind] = str(amount)
        total += 2 * amount
    return {'generation_stage_usd': stages['generation'], 'review_stage_usd': stages['review'],
            'generation_stages': 2, 'review_stages': 2, 'total_usd': str(total),
            'kind': 'original-envelope-offline-projection', 'funding_authorized': False}


class OfflineAdmissionRevisionStore:
    """Single serialized private journal, outside the live repository.

Events are canonical immutable files, linked into place only after fsync. No
mutable head file is required. Interrupted temporary files are not committed
events. A committed event is replayed rather than overwritten or allocated twice.
"""
    def __init__(self, root, state_root):
        self.root, self.state_root = Path(root).resolve(), Path(state_root).resolve()
        if (self.root == self.state_root or self.root.is_relative_to(self.state_root)
                or self.state_root.is_relative_to(self.root)):
            raise ContractError('Offline revision storage must be outside live repository State')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.events = self.root / 'events'
        if self.events.is_symlink():
            raise ContractError('Revision event directory cannot be a symlink')
        self.events.mkdir(exist_ok=True, mode=0o700)
        self._root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self._events_identity = (self.events.stat().st_dev, self.events.stat().st_ino)
        descriptor = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size:
                raise ContractError('Revision lock must be an empty regular private file')
            self._lock_identity = (metadata.st_dev, metadata.st_ino)
            os.fsync(descriptor)
        finally: os.close(descriptor)
        # Persist directory entries, not only later event files. New nested
        # parents are fsynced up to the filesystem root as a conservative bound.
        for directory in (self.events, self.root, *self.root.parents):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)

    def _storage_check(self):
        try:
            if (self.root.is_symlink() or self.events.is_symlink()
                    or self.root.resolve() != self.root or self.events.resolve() != self.events
                    or (self.root / '.lock').is_symlink()
                    or ((self.root / '.lock').stat().st_dev, (self.root / '.lock').stat().st_ino) != self._lock_identity
                    or (self.root.stat().st_dev, self.root.stat().st_ino) != self._root_identity
                    or (self.events.stat().st_dev, self.events.stat().st_ino) != self._events_identity):
                raise ContractError('Offline revision storage was moved or redirected')
        except OSError as exc:
            raise ContractError('Offline revision storage is unavailable or redirected') from exc

    @contextmanager
    def _lock(self):
        self._storage_check()
        root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = descriptor = None
        try:
            if (os.fstat(root).st_dev, os.fstat(root).st_ino) != self._root_identity:
                raise ContractError('Revision root changed while locking')
            directory = os.open('events', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            if (os.fstat(directory).st_dev, os.fstat(directory).st_ino) != self._events_identity:
                raise ContractError('Revision event directory changed while locking')
            descriptor = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ContractError('Revision lock must be a regular private file')
            if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != self._lock_identity:
                raise ContractError('Revision serialization lock was replaced')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._storage_check()
            yield directory
            self._storage_check()
        except OSError as exc:
            raise ContractError('Offline revision storage operation failed closed') from exc
        finally:
            if descriptor is not None: os.close(descriptor)
            if directory is not None: os.close(directory)
            os.close(root)

    def _read(self, directory):
        values, total, previous = [], 0, ZERO
        names = sorted(name for name in os.listdir(directory) if name.endswith('.json'))
        if len(names) > MAX_EVENTS:
            raise ContractError('Complete revision journal exceeds event bound')
        for sequence, name in enumerate(names, 1):
            match = re.fullmatch(r'([0-9]{8})-([0-9a-f]{64})\.json', name)
            if match is None or int(match[1]) != sequence:
                raise ContractError('Revision journal has a gap, fork or malformed event name')
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_EVENT_BYTES:
                raise ContractError('Revision event must be a bounded regular immutable file')
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_ino != metadata.st_ino or opened.st_dev != metadata.st_dev:
                    raise ContractError('Revision event changed while reading')
                raw = stream.read(MAX_EVENT_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_EVENT_BYTES or total > MAX_JOURNAL_BYTES:
                raise ContractError('Complete revision journal exceeds byte bound')
            value = loads(raw)
            if (not isinstance(value, dict) or set(value) != {'version', 'sequence', 'previous_sha256', 'event'}
                    or value['version'] != VERSION or type(value['sequence']) is not int
                    or value['sequence'] != sequence or value['previous_sha256'] != previous
                    or raw != canonical(value) + b'\n' or json_hash(value) != match[2]):
                raise ContractError('Immutable revision history or hash chain changed')
            values.append(value)
            previous = match[2]
        return self._derive(values, previous)

    def _derive(self, events, head):
        proposals, entries, allocations, campaigns = {}, {}, {}, {}
        for wrapped in events:
            event = wrapped['event']
            if (not isinstance(event, dict) or event.get('offline_only') is not True
                    or event.get('funding_authorized') is not False or event.get('runtime_admission_supported') is not False):
                raise ContractError('Revision event cannot confer live admission or funding')
            kind, identity = event.get('kind'), event.get('revision_id')
            if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity):
                raise ContractError('Invalid revision identity')
            if kind == 'proposed':
                if set(event) != {'kind', 'revision_id', 'proposal', 'offline_only', 'funding_authorized', 'runtime_admission_supported'}:
                    raise ContractError('Invalid proposal event schema')
                proposal = event['proposal']
                if not isinstance(proposal, dict) or set(proposal) != {'origin', 'original_contract', 'original_task_template',
                        'policy_sha256', 'evidence_sha256', 'cycle', 'ceiling'}:
                    raise ContractError('Invalid immutable admission proposal')
                origin = proposal['origin']
                cycle = OfflineComponentCycle.restore(proposal['cycle'])
                snapshot = cycle.snapshot()
                context = snapshot['context']
                if (snapshot['records'] or snapshot['status'] != 'awaiting_request'
                        or json_hash(context['scripture_policy']) != proposal['policy_sha256']
                        or json_hash(context['evidence']) != proposal['evidence_sha256']
                        or not _same(complete_cycle_ceiling(context['processing']), proposal['ceiling'])):
                    raise ContractError('Revision processing context or complete budget proof changed')
                expected_id = json_hash({'origin': origin, 'policy_sha256': proposal['policy_sha256'],
                                        'evidence_sha256': proposal['evidence_sha256']})
                self._verify_projection(proposal, identity, context)
                key = (origin['campaign_id'], origin['entry_id'])
                if identity != expected_id or identity in proposals or key in entries:
                    raise ContractError('Duplicate or conflicting successor for original attention entry')
                entries[key] = identity
                proposals[identity] = {'proposal': proposal, 'status': 'proposed'}
                allocations[origin['campaign_id']] = allocations.get(origin['campaign_id'], Decimal(0)) + _money(proposal['ceiling']['total_usd'])
                if (origin['campaign_id'] in campaigns and campaigns[origin['campaign_id']] != origin['campaign_contract_sha256']):
                    raise ContractError('Revision journal changed its original campaign authority')
                campaigns[origin['campaign_id']] = origin['campaign_contract_sha256']
                if allocations[origin['campaign_id']] > _money(origin['budget_usd']):
                    raise ContractError('Revision journal oversubscribes its original envelope')
            elif kind in ('prepared_offline', 'cancelled'):
                expected = {'kind', 'revision_id', 'proposal_sha256', 'offline_only', 'funding_authorized', 'runtime_admission_supported'}
                if kind == 'cancelled': expected.add('reason')
                if set(event) != expected or identity not in proposals:
                    raise ContractError('Revision successor lacks its exact immutable proposal')
                prior = proposals[identity]
                if (event['proposal_sha256'] != json_hash(prior['proposal'])
                        or prior['status'] == 'cancelled' or (kind == 'prepared_offline' and prior['status'] != 'proposed')):
                    raise ContractError('Revision successor rewrites a terminal or prepared state')
                if kind == 'cancelled' and (not isinstance(event['reason'], str) or not 0 < len(event['reason']) <= 2048):
                    raise ContractError('Invalid offline cancellation reason')
                prior['status'] = kind
            else:
                raise ContractError('Unknown admission revision event')
        return {'events': events, 'head': head, 'proposals': proposals, 'entries': entries,
                'projected_allocations': allocations}

    @staticmethod
    def _verify_projection(proposal, identity, context):
        """A valid protocol in isolation cannot substitute cheaper/newer models."""
        origin, original, template = (proposal['origin'], proposal['original_contract'],
                                       proposal['original_task_template'])
        fields = {'campaign_id', 'entry_id', 'request_sha256', 'campaign_contract_sha256', 'entry_sha256',
                  'attention_event_sha256', 'source_sha256', 'record_sha256', 'task_template_sha256',
                  'models_sha256', 'budget_usd'}
        if (not isinstance(origin, dict) or set(origin) != fields or not isinstance(original, dict)
                or not isinstance(template, dict) or json_hash(original) != origin['campaign_contract_sha256']
                or json_hash(template) != origin['task_template_sha256']
                or json_hash(original.get('models')) != origin['models_sha256']
                or original.get('id') != origin['campaign_id'] or template.get('campaign') != origin['campaign_id']
                or template.get('id') != origin['entry_id']
                or not _same(original.get('budget_usd'), origin['budget_usd'])
                or json_hash(context['source']) != origin['source_sha256']):
            raise ContractError('Original funding/model/task anchors changed in revision projection')
        if (any(not isinstance(origin[key], str) or not origin[key] for key in ('campaign_id', 'entry_id'))
                or any(not isinstance(origin[key], str) or not re.fullmatch(r'[0-9a-f]{64}', origin[key])
                       for key in fields if key.endswith('_sha256'))):
            raise ContractError('Malformed original revision identities or provenance hashes')
        campaign = copy.deepcopy(original)
        campaign['scripture_quotes'] = context['scripture_policy']
        task = copy.deepcopy(template)
        task.update(id=digest('component-revision:' + identity)[:32], models=copy.deepcopy(original['models']))
        processing = context['processing']
        selected = {name: original['models'][name] for name in {task['model'], task['review_model']}}
        if (processing['origin_campaign_sha256'] != json_hash(campaign)
                or processing['origin_task_sha256'] != json_hash(task)
                or processing['task_id'] != task['id'] or processing['campaign_id'] != original['id']
                or not _same(processing['models'], selected)
                or processing['generation_model'] != task['model'] or processing['review_model'] != task['review_model']
                or processing['language'] != task['language']
                or not _same(processing['language_settings'], original['language_settings'][task['language']])
                or not _same(processing['glossary'], original['glossaries'].get(task['language'], {}))
                or any(not _same(processing[key], original[key]) for key in
                       ('quality_threshold', 'max_output_tokens', 'review_output_tokens'))
                or not processing['prompts']['generation'].startswith(original['prompts']['translation'] + '\n\n')
                or not processing['prompts']['review'].startswith(original['prompts']['review'] + '\n\n')):
            raise ContractError('Offline execution projection changed original frozen models, prompts or limits')

    def _append(self, view, event, fault, directory, precommit=None):
        if len(view['events']) >= MAX_EVENTS:
            raise ContractError('Revision journal is full; history cannot be reset')
        value = {'version': VERSION, 'sequence': len(view['events']) + 1,
                 'previous_sha256': view['head'], 'event': event}
        raw = canonical(value) + b'\n'
        if len(raw) > MAX_EVENT_BYTES or sum(len(canonical(item)) + 1 for item in view['events']) + len(raw) > MAX_JOURNAL_BYTES:
            raise ContractError('Complete revision event exceeds its immutable bound')
        destination = f'{value["sequence"]:08d}-{json_hash(value)}.json'
        temporary = '.tmp-' + uuid4().hex
        self._storage_check()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if fault: fault('before_commit')
            self._storage_check()
            if precommit: precommit()
            os.link(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
            if fault: fault('after_commit')
            self._storage_check()
            if precommit: precommit()
        finally:
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass

    def _check_engine(self, engine):
        if Path(engine.state.root).resolve() != self.state_root:
            raise ContractError('Revision store belongs to a different State view')

    def _head(self, view, expected, *, idempotent=False):
        if expected is None:
            return
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ContractError('Invalid expected revision journal head')
        if expected == view['head']:
            return
        # Lost acknowledgement can legitimately retry from an earlier retained
        # ancestor. A missing externally retained head is not such a retry: it
        # can indicate deletion of a cancellation/history tail and must hold.
        if idempotent and expected in {ZERO, *(json_hash(event) for event in view['events'])}:
            return
        raise ContractError('Stale or missing revision journal head; re-read before changing history')

    def _budget(self, view, observed, extra=Decimal(0)):
        campaign = observed['campaign']
        projected = view['projected_allocations'].get(campaign['id'], Decimal(0)) + extra
        if _money(observed['consumed_usd']) + projected > _money(campaign['budget_usd']):
            raise ContractError('Complete projected cycles exceed the original manual envelope')

    @staticmethod
    def _event(kind, identity, **fields):
        return {'kind': kind, 'revision_id': identity, **fields, 'offline_only': True,
                'funding_authorized': False, 'runtime_admission_supported': False}

    def propose(self, engine, campaign_id, entry_id, scripture_policy, evidence, protocol_root,
                *, expected_head=None, fault=None):
        self._check_engine(engine)
        with self._lock() as directory:
            view = self._read(directory)
            observed = inspect_never_paid_hold(engine, campaign_id, entry_id)
            source = observed['source']
            validate_component_evidence(source, evidence, scripture_policy, require_fresh=True)
            identity = json_hash({'origin': observed['origin'], 'policy_sha256': json_hash(scripture_policy),
                                  'evidence_sha256': json_hash(evidence)})
            if identity in view['proposals']:
                self._head(view, expected_head, idempotent=True)
                self._budget(view, observed)
                return copy.deepcopy({'revision_id': identity, **view['proposals'][identity]})
            self._head(view, expected_head)
            if (campaign_id, entry_id) in view['entries']:
                raise ContractError('Original attention entry already has a distinct offline successor')
            # This projection is never written as a campaign/task. The original
            # campaign's money, model names, prices and output limits are retained.
            original_contract = manual_admission.contract(observed['campaign'])
            campaign = copy.deepcopy(original_contract)
            campaign['scripture_quotes'] = copy.deepcopy(scripture_policy)
            task = copy.deepcopy(observed['entry']['provenance']['task'])
            task.update(id=digest('component-revision:' + identity)[:32], models=copy.deepcopy(campaign['models']))
            processing = processing_contract(protocol_root, campaign, task)
            cycle = OfflineComponentCycle(source, evidence, scripture_policy, processing)
            ceiling = complete_cycle_ceiling(processing)
            proposal = {'origin': observed['origin'], 'original_contract': copy.deepcopy(original_contract),
                        'original_task_template': copy.deepcopy(observed['entry']['provenance']['task']),
                        'policy_sha256': json_hash(scripture_policy),
                        'evidence_sha256': json_hash(evidence), 'cycle': cycle.snapshot(), 'ceiling': ceiling}
            if fault: fault('before_recheck')
            def recheck():
                fresh = inspect_never_paid_hold(engine, campaign_id, entry_id)
                if not _same(fresh['origin'], observed['origin']):
                    raise ContractError('Original hold changed before revision commit')
                self._budget(view, fresh, _money(ceiling['total_usd']))
            recheck()
            self._append(view, self._event('proposed', identity, proposal=proposal), fault, directory, recheck)
            return {'revision_id': identity, 'proposal': copy.deepcopy(proposal), 'status': 'proposed'}

    def prepare(self, engine, revision_id, *, expected_head=None, fault=None):
        self._check_engine(engine)
        with self._lock() as directory:
            view = self._read(directory)
            item = view['proposals'].get(revision_id)
            if not item or item['status'] == 'cancelled':
                raise ContractError('No uncancelled offline revision to prepare')
            proposal, origin = item['proposal'], item['proposal']['origin']
            observed = inspect_never_paid_hold(engine, origin['campaign_id'], origin['entry_id'])
            if not _same(observed['origin'], origin):
                raise ContractError('Original attention or source/publication history changed')
            self._budget(view, observed)
            cycle = OfflineComponentCycle.restore(proposal['cycle'])
            context = cycle.snapshot()['context']
            validate_component_evidence(context['source'], context['evidence'], context['scripture_policy'], require_fresh=True)
            if item['status'] == 'prepared_offline':
                self._head(view, expected_head, idempotent=True)
                fresh = inspect_never_paid_hold(engine, origin['campaign_id'], origin['entry_id'])
                if not _same(fresh['origin'], origin):
                    raise ContractError('Original hold changed while restoring offline preparation')
                self._budget(view, fresh)
                return cycle
            self._head(view, expected_head)
            if fault: fault('before_recheck')
            def recheck():
                fresh = inspect_never_paid_hold(engine, origin['campaign_id'], origin['entry_id'])
                if not _same(fresh['origin'], origin):
                    raise ContractError('Original hold changed before offline prepare commit')
                self._budget(view, fresh)
            recheck()
            self._append(view, self._event('prepared_offline', revision_id,
                proposal_sha256=json_hash(proposal)), fault, directory, recheck)
            return cycle

    def cancel(self, revision_id, reason, *, expected_head=None, fault=None):
        if not isinstance(reason, str) or not 0 < len(reason) <= 2048:
            raise ContractError('A bounded cancellation reason is required')
        with self._lock() as directory:
            view = self._read(directory); item = view['proposals'].get(revision_id)
            if not item: raise ContractError('Unknown offline revision')
            if item['status'] == 'cancelled':
                self._head(view, expected_head, idempotent=True)
                return copy.deepcopy(item)
            self._head(view, expected_head)
            self._append(view, self._event('cancelled', revision_id,
                proposal_sha256=json_hash(item['proposal']), reason=reason), fault, directory)
            return {'proposal': copy.deepcopy(item['proposal']), 'status': 'cancelled'}

    def inspect(self):
        with self._lock() as directory:
            view = self._read(directory)
            # JSON-safe public view; allocations remain projections, not funding.
            view['projected_allocations'] = {key: str(value) for key, value in view['projected_allocations'].items()}
            view['entries'] = [{'campaign_id': key[0], 'entry_id': key[1], 'revision_id': value}
                               for key, value in view['entries'].items()]
            return copy.deepcopy(view)
