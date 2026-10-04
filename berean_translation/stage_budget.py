"""Conservative full-stage admission for newly authorized autonomous work.

Old task/campaign reservation contracts remain untouched. Unknown future JSON is
bounded in canonical UTF-8 bytes and is never shortened to fit an envelope.
"""
from decimal import Decimal
from .common import ContractError, canonical
from .requests import build_request, reserve_cost

STAGES = ('translate', 'review1', 'correct', 'review2')


class PlanningState:
    def __init__(self, state, campaign, source, candidate):
        self.state, self.campaign, self.snapshot, self.value = state, campaign, source, candidate
        self.planning = True
        self.writes = {}

    def read(self, path, default=None):
        if path == f'state/campaigns/{self.campaign["id"]}.json':
            return self.campaign
        return self.state.read(path, default)

    def write(self, path, value):
        if not path.endswith('/scripture-selections.json'):
            raise ContractError('Read-only admission cannot write runtime state')
        self.writes[path] = value

    def source(self, task):
        return self.snapshot

    def candidate(self, task):
        return self.value


def plan(config, state, task, campaign, source, candidate=None):
    start = task['stage']
    if start not in STAGES:
        raise ContractError('Unknown autonomous initial stage')
    candidate_bytes = max(120000, len(canonical(candidate)) if candidate is not None else 0)
    if candidate_bytes > config.runtime['max_result_bytes']:
        raise ContractError('Saved candidate exceeds complete-result safety limit')
    findings_bytes = 32768
    estimates = {}
    for stage in STAGES[STAGES.index(start):]:
        # Saved review2 resumes that exact review. A known repair input remains
        # exact; generation output and future findings need worst-case bounds.
        known_candidate = candidate is not None and stage in ('review1', 'correct') and (
            start == stage or start == 'review1')
        if start == 'review2':
            known_candidate = True
        current = {**task, 'stage': stage}
        unknown_findings = stage == 'correct' and start in ('translate', 'review1')
        if unknown_findings:
            current['findings'] = []
        view = PlanningState(state, campaign, source, candidate if known_candidate else None)
        line, cost, bound = build_request(config, view, current)
        if stage != 'translate' and not known_candidate:
            bound += 2 * candidate_bytes - 4
        if unknown_findings:
            bound += 2 * findings_bytes - 2
        if stage.startswith('review') and campaign.get('scripture_quotes'):
            bound += 2 * campaign['scripture_quotes']['max_selection_audit_bytes'] - 4
        model = task['models'][task['review_model'] if stage.startswith('review') else task['model']]
        output = line['body']['max_completion_tokens']
        if bound + output > model['context_tokens']:
            raise ContractError('Complete remaining stage chain exceeds conservative model context')
        estimates[stage] = reserve_cost(model, bound, output)
    return {'version': 1, 'initial_stage': start, 'max_candidate_bytes': candidate_bytes,
            'max_findings_bytes': findings_bytes, 'stages_usd': estimates,
            'total_reserved_usd': float(sum((Decimal(str(v)) for v in estimates.values()), Decimal(0)))}


def enforce(task, candidate=None):
    budget = task.get('stage_budget')
    if budget is None:
        return
    if candidate is not None and len(canonical(candidate)) > budget['max_candidate_bytes']:
        raise ContractError('Full candidate exceeds its frozen byte ceiling; no text was truncated')
    if len(canonical(task.get('findings', []))) > budget['max_findings_bytes']:
        raise ContractError('Full review findings exceed their frozen byte ceiling; no findings were truncated')
