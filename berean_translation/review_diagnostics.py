"""Read-only reporting of complete, preserved legacy negative review overflow.

This projection never makes a review acceptable, changes a terminal decision, or
grants recovery eligibility. Every finding, including those past the gate's
limit, must validate before the negative review is described as usable evidence.
"""
from __future__ import annotations

from collections import Counter
from html import escape

from .common import ContractError, canonical, digest, loads
from .review_contract import MAX_FINDINGS, validate_review


def preserved_review_diagnostics(config, state, *, tasks=None):
    """Return bounded diagnostics for the latest task of each published/work pair.

    The parsed result and complete raw attempt must agree. Task findings and
    stage decisions are deliberately not used as substitutes: a legacy stage
    decision may have discarded all findings when it rejected the list length.
    """
    maximum_bytes = config.runtime['max_result_bytes']
    by_id = {task['id']: task for task in (state.tasks() if tasks is None else tasks)}
    diagnostics = []
    for record in sorted(state.records(), key=lambda r: (r['language'], r['article_id'])):
        task = by_id.get(record.get('latest_task'))
        if (not task or task.get('status') != 'not_ready'
                or task.get('failure_kind') != 'invalid_result'
                or task.get('stage') not in ('review1', 'review2')
                or (task.get('language'), task.get('article_id')) != (record['language'], record['article_id'])):
            continue
        base = f'state/tasks/{task["id"]}'
        result_path = f'{base}/results/{task["stage"]}.json'
        raw_path = f'{base}/attempts/{task["stage"]}.json'
        try:
            campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
            # Only an absent marker identifies the historical three-field contract.
            if not isinstance(campaign, dict) or 'review_contract_version' in campaign:
                continue
            archived = state.read(result_path)
            review = archived.get('result') if isinstance(archived, dict) else None
            if not isinstance(review, dict) or len(canonical(review)) > maximum_bytes:
                continue
            findings = review.get('findings')
            if (not isinstance(findings, list) or len(findings) <= MAX_FINDINGS
                    or review.get('passed') is not False):
                continue
            # This diagnostic is the sole exception to list-length validation;
            # the canonical byte bound above still applies to the entire result.
            validate_review(review, enforce_limit=False)
            attempt = state.read(raw_path)
            response = attempt.get('response') if isinstance(attempt, dict) else None
            if (not isinstance(response, dict) or attempt.get('stage') != task['stage']
                    or response.get('outcome') != 'structured_response'
                    or response.get('finish_reason') != 'stop'
                    or response.get('content_evidence_truncated') is not False
                    or response.get('refusal') or response.get('provider_failure')):
                continue
            content = response.get('content')
            if not isinstance(content, str):
                continue
            raw = content.encode('utf-8')
            if (len(raw) > maximum_bytes or response.get('content_bytes') != len(raw)
                    or response.get('content_sha256') != digest(raw) or loads(raw) != review):
                continue
        except (ContractError, OSError, KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            # Damaged, unsupported or incomplete evidence is never upgraded by
            # a report. Its original failure remains available in task history.
            continue
        counts = Counter(finding['severity'] for finding in findings)
        diagnostics.append({
            'classification': 'preserved_negative_review_overflow',
            'language': task['language'], 'article_id': task['article_id'],
            'task_id': task['id'], 'stage': task['stage'],
            'status': task['status'], 'failure_kind': task['failure_kind'],
            'failure': task.get('failure', ''), 'score': review['score'],
            'findings_count': len(findings),
            'severity_counts': {severity: counts[severity] for severity in ('critical', 'major', 'minor')},
            'result_path': result_path, 'raw_path': raw_path, 'task_path': f'{base}/task.json',
        })
    return diagnostics


def _cell(value):
    return escape(str(value), quote=False).replace('|', '&#124;').replace('`', '&#96;').replace('\n', ' ').replace('\r', ' ')


def render_review_diagnostics(diagnostics):
    """Render a STATUS section without mutating any durable work evidence."""
    if not diagnostics:
        return []
    rows = ['', '## Preserved overlong negative reviews', '',
            f'These legacy negative responses completed normally but exceeded the {MAX_FINDINGS}-finding acceptance limit. '
            'Every finding\'s fields in the full bounded result have been validated for diagnostic reporting. '
            'The original failure, terminal decision, existing publication, and recovery eligibility are unchanged. '
            'This report does not authorize another paid attempt.', '',
            '| Language / article | Original task outcome | Preserved negative review | Severity counts | Full evidence |',
            '| --- | --- | --- | --- | --- |']
    for item in diagnostics:
        counts = item['severity_counts']
        rows.append(f'| {_cell(item["language"])} / {_cell(item["article_id"])} | '
                    f'{_cell(item["status"])} / {_cell(item["failure_kind"])}: {_cell(item["failure"])} | '
                    f'Score {item["score"]}/100; {item["findings_count"]} findings; passed=false | '
                    f'{counts["critical"]} critical, {counts["major"]} major, {counts["minor"]} minor | '
                    f'[Full result]({item["result_path"]}) · [Raw attempt]({item["raw_path"]}) · '
                    f'[Task]({item["task_path"]}) |')
    return rows
