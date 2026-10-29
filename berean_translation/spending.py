"""Provider-managed spending policy; repository cost records are informational.

The owner removed all repository monetary limits on 2026-10-29. Historical
authorizations and estimates remain immutable evidence, never renewed balances.
This policy does not relax content, identity, cancellation or submission checks.
"""
from .common import ContractError, json_hash, money

POLICY = {
    'version': 1,
    'authorization': 'owner-2026-10-29-provider-managed-spending',
    'budget_enforcement': 'none',
    'applies_to': 'new-and-existing-work',
}
POLICY_ID = POLICY['authorization']
POLICY_SHA256 = json_hash(POLICY)
POLICY_PATH = 'state/policies/provider-managed-spending.json'


def external(campaign):
    """Identify new records without reinterpreting historical frozen fields."""
    value = campaign.get('spending_policy')
    if value not in (None, POLICY_ID):
        raise ContractError('Unknown spending policy provenance')
    return value == POLICY_ID


def validate(state):
    existing = state.read(POLICY_PATH)
    if existing is not None and existing != POLICY:
        raise ContractError('Spending policy audit record changed')


def budget_only(task):
    return task.get('status') == 'budget_blocked' or task.get('failure') in (
        'Request exceeds its frozen complete-stage reservation',
        'Request exceeds its frozen complete-cycle reservation',
    )


def record(engine):
    """Checkpoint the owner instruction before resuming paid processing."""
    existing = engine.state.read(POLICY_PATH)
    if existing is not None:
        if existing != POLICY:
            raise ContractError('Spending policy audit record changed')
        return
    engine.state.write(POLICY_PATH, POLICY)
    engine.checkpoint('runtime: record owner-managed spending policy without monetary limits')


def historical_budget(request):
    """Preserve optional legacy inputs as data, without imposing any ceiling."""
    value = request.get('budget_usd')
    return None if value is None else float(money(value))


def current_hold(hold):
    """Retired monetary holds remain on disk but cannot block current admission."""
    if hold.get('reason') in ('automatic_budget_blocked', 'continuation_cycle_budget_blocked'):
        return {}
    return hold
