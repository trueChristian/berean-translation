"""Finite successor selection; history is evidence, never resettable counters."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from html import unescape

from .common import ContractError, json_hash

VERSION = 2
DEFAULT_POLICY = {'version': VERSION, 'max_cycles': 3, 'max_strategy_cycles': 2,
                  'cooldown_seconds': 3600, 'max_candidate_bytes': 120000}


def policy(max_candidate_bytes=120000):
    result = {**DEFAULT_POLICY, 'max_candidate_bytes': max_candidate_bytes}
    validate_policy(result)
    return result


def validate_policy(value):
    if value is None:
        return None  # An immutable request without this field retains version 1.
    if (not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY)
            or any(type(v) is not int for v in value.values())
            or any(value[k] != DEFAULT_POLICY[k] for k in DEFAULT_POLICY if k != 'max_candidate_bytes')
            or not 1024 <= value['max_candidate_bytes'] <= 1000000):
        raise ContractError('Continuation requires version 2, three total cycles, two per strategy, one-hour cooldown and a bounded candidate size')
    return value


def strategy(campaign, language):
    """Only applicable quality/execution settings identify a repair strategy.

    Pricing, unrelated registry entries, run IDs and repository revisions cannot
    manufacture a new strategy. Old campaigns used their translation prompt.
    """
    models = {}
    for role in ('model', 'review_model'):
        model = campaign['models'][campaign[role]]
        models[role] = {key: model.get(key) for key in ('api_model', 'reasoning_effort')}
    value = {'models': models,
        'repair_prompt': campaign['prompts'].get('repair', campaign['prompts']['translation']),
        'review_prompt': campaign['prompts']['review'],
        'language': {key: campaign['language_settings'][language][key] for key in ('name', 'tag', 'guidance')},
        'glossary': campaign['glossaries'].get(language, {}),
        'byline_contract': ('source' if campaign.get('prompt_version') in (None, '1.0.0', '1.0.1') else 'source_context'),
        'max_output_tokens': min(campaign['max_output_tokens'], campaign['models'][campaign['model']]['max_output_tokens']),
        'review_output_tokens': min(campaign['review_output_tokens'], campaign['models'][campaign['review_model']]['max_output_tokens']),
        'quality_threshold': campaign['quality_threshold']}
    if 'review_contract_version' in campaign:
        from .review_contract import frozen_version
        value['review_contract_version'] = frozen_version(campaign)
    return json_hash(value)


def task_id(campaign, selection):
    from .common import digest
    return digest(campaign['id'] + ':' + selection['language'] + ':' + selection['article_id'])[:32]


def lineages(entries):
    """Validate a single append-only chain across manual and standing funding."""
    grouped = {}
    for campaign, item in entries:
        key = item.get('recovery_key')
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ContractError('Invalid downstream source lineage identity')
        continuation = campaign['downstream_request'].get('continuation_policy')
        validate_policy(continuation)
        details = item.get('continuation')
        if continuation is None:
            if details is not None:
                raise ContractError('Legacy downstream selections cannot gain continuation authority')
            cycle = 1
        else:
            if (not isinstance(details, dict) or set(details) != {'cycle', 'strategy_sha256'}
                    or type(details['cycle']) is not int
                    or not 1 <= details['cycle'] <= continuation['max_cycles']
                    or details['strategy_sha256'] != strategy(campaign, item['language'])):
                raise ContractError('Downstream continuation cycle or strategy changed')
            cycle = details['cycle']
        grouped.setdefault(key, []).append({'campaign': campaign, 'selection': item,
            'cycle': cycle, 'task_id': task_id(campaign, item),
            'strategy_sha256': strategy(campaign, item['language'])})
    for history in grouped.values():
        history.sort(key=lambda entry: entry['cycle'])
        strategies = {}
        for ordinal, entry in enumerate(history, 1):
            if entry['cycle'] != ordinal:
                raise ContractError('Duplicate or missing downstream lineage cycle')
            if ordinal > 1 and entry['selection']['previous_task_id'] != history[ordinal - 2]['task_id']:
                raise ContractError('A downstream cycle requires the exact immediate predecessor; forks are forbidden')
            chosen = entry['strategy_sha256']
            strategies[chosen] = strategies.get(chosen, 0) + 1
            if strategies[chosen] > DEFAULT_POLICY['max_strategy_cycles']:
                raise ContractError('Downstream strategy cycle limit exceeded')
    return grouped


def normalize(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(text)).casefold()))


def anchors(task, source):
    """Quoted source evidence, not reviewer prose or scores, supports progress.

    Ambiguous/elliptical quotations cannot establish that a defect disappeared.
    Keep that case visible for attention instead of treating wording drift as
    permission to continue buying reviews.
    """
    source_text = normalize(unescape(re.sub(r'<[^>]*>', ' ', source['html'])) + ' ' +
                            ' '.join(str(source['article'].get(k) or '') for k in ('title', 'subtitle', 'section')))
    raw_findings = task.get('findings')
    if (not isinstance(raw_findings, list) or any(
            not isinstance(finding, dict) or any(
                not isinstance(finding.get(key), str) for key in
                ('severity', 'location', 'source_quote', 'translation_quote', 'suggested_fix'))
            for finding in raw_findings)):
        return None
    findings = [f for f in raw_findings if f['severity'] in ('major', 'critical')]
    if not findings:
        return None
    result = []
    for finding in findings:
        raw = finding.get('source_quote', '')
        value = normalize(raw)
        if not value or '...' in raw or '…' in raw or value not in source_text:
            return None
        result.append(value)
    return result


def next_reason(state, task, history, settings, next_strategy, *, automatic=False, at=None):
    """No paid work, mutations or policy switches occur during selection."""
    if task.get('failure_kind') == 'incomplete_review':
        return 'review_incomplete_requires_owner_attention'
    if (task.get('stage') in ('review1', 'review2') and task.get('failure_kind') != 'quality_rejection'
            and 'Final review failed' not in task.get('failure', '')):
        return 'continuation_review_only_requires_attention'
    if not history:
        return 'lineage_evidence_missing_requires_attention' if task.get('downstream_recovery') else None
    if len(history) >= settings['max_cycles']:
        return 'continuation_cycles_exhausted_requires_attention'
    last = history[-1]
    if task['id'] != last['task_id']:
        return 'lineage_predecessor_changed_requires_attention'
    if sum(entry['strategy_sha256'] == next_strategy for entry in history) >= settings['max_strategy_cycles']:
        return 'continuation_strategy_exhausted_requires_attention'
    if automatic:
        try:
            finished = datetime.fromisoformat(task['finished_at'])
            if finished.tzinfo is None:
                raise ValueError('missing timezone')
        except (KeyError, TypeError, ValueError):
            return 'missing_completion_time_requires_attention'
        if ((at or datetime.now(timezone.utc)) - finished).total_seconds() < settings['cooldown_seconds']:
            return 'continuation_cooldown'
    if task.get('failure_kind') == 'candidate_size_limit':
        old_bound = last['selection'].get('cycle_budget', {}).get('max_candidate_bytes', 0)
        if not automatic and settings['max_candidate_bytes'] > old_bound:
            return None  # An explicit larger manual bound, still one counted cycle.
        return 'continuation_candidate_size_requires_attention'
    if (task.get('failure_kind') != 'quality_rejection'
            and not (task.get('failure_kind') == 'invalid_result' and task.get('stage') in ('correct', 'translate'))):
        return 'continuation_technical_failure_requires_attention'
    # A material changed strategy gets one bounded opportunity, never a reset of
    # the lifetime/strategy limits. This includes PR13's stronger repair prompt.
    if next_strategy != last['strategy_sha256']:
        return None
    candidate_hash = json_hash(state.candidate(task))
    if any(candidate_hash == entry['selection']['candidate_sha256'] for entry in history):
        return 'continuation_no_candidate_progress_requires_attention'
    if task.get('failure_kind') == 'quality_rejection':
        previous = state.read(f'state/tasks/{last["selection"]["previous_task_id"]}/task.json')
        current_anchors = anchors(task, state.source(task))
        previous_anchors = anchors(previous, state.source(task))
        if current_anchors is None or previous_anchors is None:
            return 'continuation_progress_uncertain_requires_attention'
        if any(a in b or b in a for a in current_anchors for b in previous_anchors):
            return 'continuation_repeated_findings_requires_attention'
    elif task.get('failure_kind') not in ('invalid_result',):
        # Technical review failures need review-only handling, not an automatic
        # rewrite. Unknown provider/submission outcomes are never repair signals.
        return 'continuation_technical_failure_requires_attention'
    return None
