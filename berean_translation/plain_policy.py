"""Frozen ordinary translation policy, including never-paid manual migrations.

New work freezes this policy. Paid historical requests retain their original
bytes and funding while current response evaluation uses the owner's policy.
A migrated never-paid manual entry owns a separately frozen prompt overlay;
its original campaign, request, budget and model settings stay intact.
"""
from __future__ import annotations

import copy

from .common import ContractError, json_hash

FIELD = 'plain_translation_policy_version'
VERSION = 1


def is_plain(settings):
    """Validate an explicit policy marker; absence preserves historical behavior."""
    if FIELD not in settings:
        return False
    if type(settings[FIELD]) is not int or settings[FIELD] != VERSION:
        raise ContractError('Unsupported plain translation policy version')
    return True


def enabled(config):
    runtime = config.runtime if hasattr(config, 'runtime') else config
    return is_plain(runtime)


def frozen_fields(config):
    return {FIELD: VERSION} if enabled(config) else {}


def effective_campaign(state, task, campaign):
    """Return the frozen request view without changing recorded paid history."""
    migration_hash = task.get('plain_policy_migration_sha256')
    if migration_hash is None:
        is_plain(campaign)
        return campaign
    from . import manual_admission
    ledger = state.read(manual_admission.path(campaign['id']))
    if ledger is None:
        raise ContractError('Plain manual migration has no admission ledger')
    manual_admission.validate(state, campaign, ledger)
    entry = ledger['entries'].get(task.get('id'), {})
    migration = entry.get('plain_migration')
    if (not isinstance(migration, dict) or migration.get('sha256') != migration_hash
            or not is_plain(task)
            or migration.get('original_campaign_sha256') != json_hash(manual_admission.contract(campaign))):
        raise ContractError('Plain manual task changed its frozen migration')
    result = copy.deepcopy(campaign)
    result.update(copy.deepcopy(migration['policy']))
    result.pop('scripture_quotes', None)
    return result
