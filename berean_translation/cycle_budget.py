"""Read-only cost ceilings for one generation and its independent review.

These ceilings are for newly admitted continuation work. Historical campaigns
keep their existing request and reservation contracts. The caller must freeze the
returned plan and enforce its candidate limit before preparing the review.
"""
from __future__ import annotations

from decimal import Decimal

from .common import ContractError, canonical
from .requests import build_request, reserve_cost


DEFAULT_MAX_CANDIDATE_BYTES = 120000


def _candidate_limit(max_candidate_bytes):
    if type(max_candidate_bytes) is not int or max_candidate_bytes <= 0:
        raise ContractError('Canonical candidate byte ceiling must be a positive integer')
    return max_candidate_bytes


def enforce_candidate_bound(candidate, max_candidate_bytes):
    """Check the complete canonical result, including every metadata field.

    Never shorten an article to meet its reservation. Raising here leaves the
    caller responsible for retaining the rejected result as historical evidence.
    """
    _candidate_limit(max_candidate_bytes)
    try:
        size = len(canonical(candidate))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError('Candidate cannot be represented as canonical UTF-8 JSON') from exc
    if size > max_candidate_bytes:
        raise ContractError(
            f'Full canonical candidate exceeds its frozen byte ceiling '
            f'({size} > {max_candidate_bytes}); candidate was not truncated')
    return size


class _PlanningState:
    """The request builder's read-only view; virtual inputs never reach disk."""

    def __init__(self, state, campaign, candidate):
        self._state, self._campaign, self._candidate = state, campaign, candidate
        self.planning = True

    def source(self, task):
        return self._state.source(task)

    def read(self, path, default=None):
        if path == f'state/campaigns/{self._campaign["id"]}.json':
            return self._campaign
        return self._state.read(path, default)

    def candidate(self, task):
        return self._candidate


def plan_cycle(config, state, task, campaign,
               max_candidate_bytes=DEFAULT_MAX_CANDIDATE_BYTES):
    """Bound a full continuation cycle using only its frozen request settings.

    ``task`` may be an unstaged generation task or an advanced child whose plan
    is being verified, provided its original findings/rejection prompt fields
    are supplied. A downstream predecessor, when supplied, is always the repair
    input: the child's candidate changes after generation. The campaign is
    supplied directly so admission never persists speculative work.

    The generation request is exact. For review, build_request supplies an exact
    skeleton with ``translation: null``, including the source, prompt, response
    schema, model fields and 4,096-token framing allowance. A canonical candidate
    is already JSON: its UTF-8 contains no unescaped ASCII control bytes. Once
    inserted into the payload, the outer JSON encoding of user-message content
    can therefore at most double each candidate byte (quote and backslash are
    escaped; other UTF-8 bytes are unchanged). Replace null's four bytes with
    twice the frozen candidate ceiling. This bounds build_request's byte-based
    input estimate, including nested JSON escaping, for every admissible result.

    Existing reserve_cost applies worst-category cache pricing, whole-request
    long-context tiers, all output tokens and upward microdollar rounding.
    """
    _candidate_limit(max_candidate_bytes)
    if task['campaign'] != campaign['id']:
        raise ContractError('Cycle task and frozen campaign identities disagree')
    initial_stages = {'translate': 'translate', 'review1': 'translate',
                      'correct': 'correct', 'review2': 'correct'}
    if task.get('stage') not in initial_stages:
        raise ContractError('Cycle planning requires a generation or independent review stage')
    generation = {**task, 'stage': initial_stages[task['stage']]}
    candidate = None
    if generation['stage'] == 'correct':
        predecessor_id = task.get('downstream_previous_task')
        if predecessor_id:
            predecessor = state.read(f'state/tasks/{predecessor_id}/task.json')
            if not predecessor or predecessor.get('id') != predecessor_id:
                raise ContractError('Cycle repair predecessor is missing or changed')
            candidate = state.candidate(predecessor)
        else:
            candidate = state.candidate(task)
        if candidate is None:
            raise ContractError('Cycle repair input candidate is missing')

    _, repair_reserved, _ = build_request(
        config, _PlanningState(state, campaign, candidate), generation)
    review = {**generation, 'stage': 'review2' if generation['stage'] == 'correct' else 'review1'}
    line, _, skeleton_bound = build_request(
        config, _PlanningState(state, campaign, None), review)
    review_bound = skeleton_bound + max(4, 2 * max_candidate_bytes) - 4
    if campaign.get('scripture_quotes'):
        review_bound += 2 * campaign['scripture_quotes']['max_selection_audit_bytes'] - 4
    review_model = task['models'][task['review_model']]
    output_limit = line['body']['max_completion_tokens']
    if review_bound + output_limit > review_model['context_tokens']:
        raise ContractError(
            'Conservative full-cycle review token bound exceeds the selected model context '
            f'({review_bound} input + {output_limit} output > {review_model["context_tokens"]})')
    review_reserved = reserve_cost(review_model, review_bound, output_limit)
    total = Decimal(str(repair_reserved)) + Decimal(str(review_reserved))
    return {'repair_reserved_usd': repair_reserved,
            'review_reserved_usd': review_reserved,
            'total_reserved_usd': float(total),
            'max_candidate_bytes': max_candidate_bytes}
