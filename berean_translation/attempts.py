"""Immutable, bounded response evidence, including provider and parse failures."""
from __future__ import annotations
from .common import ContractError, digest, now
from .batch_telemetry import request_failure


def evidence(row, maximum_bytes):
    response = row.get('response') if isinstance(row, dict) else None
    body = response.get('body') if isinstance(response, dict) else None
    body = body if isinstance(body, dict) else {}
    choices = body.get('choices')
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else {}
    all_choices = [value for value in choices if isinstance(value, dict)] if isinstance(choices, list) else []
    any_filter = any(value.get('finish_reason') == 'content_filter' for value in all_choices)
    any_refusal = any(isinstance(value.get('message'), dict) and value['message'].get('refusal')
                      for value in all_choices)
    message = choice.get('message')
    message = message if isinstance(message, dict) else {}
    finish = choice.get('finish_reason')
    outcome = ('content_filter' if any_filter else
               'provider_refusal' if any_refusal else
               'truncated' if finish == 'length' else
               'structured_response' if finish == 'stop' else 'invalid_response')
    diagnostic = request_failure(row) if isinstance(row, dict) else None
    raw_errors = [row.get('error') if isinstance(row, dict) else None, body.get('error')]
    raw_codes = [str(error.get('code', '')).lower() for error in raw_errors if isinstance(error, dict)]
    policy_error = any(code in ('content_filter', 'content_policy_violation', 'safety_violation', 'moderation_blocked')
                       for code in raw_codes)
    if diagnostic:
        code = str(diagnostic.get('code', '')).lower()
        outcome = 'content_filter' if policy_error or 'content_filter' in code else 'provider_error'
    # Allowlist provider response fields. Never retain headers or SDK exception
    # bodies. Full returned text is retained only within the configured bound.
    result = {'outcome': outcome, 'finish_reason': finish if isinstance(finish, str) else None,
              'model': body.get('model') if isinstance(body.get('model'), str) else None,
              'usage': body.get('usage') if isinstance(body.get('usage'), dict) else {},
              'provider_failure': diagnostic}
    for key in ('content', 'refusal'):
        value = message.get(key)
        if isinstance(value, str):
            raw = value.encode('utf-8', errors='replace')
            result[key + '_sha256'] = digest(raw)
            result[key + '_bytes'] = len(raw)
            result[key] = raw[:maximum_bytes].decode('utf-8', errors='replace')
            result[key + '_evidence_truncated'] = len(raw) > maximum_bytes
    return result


def archive(state, task, row, maximum_bytes):
    result = evidence(row, maximum_bytes)
    path = f'state/tasks/{task["id"]}/attempts/{task["stage"]}.json'
    prior = state.read(path)
    if prior:
        if prior.get('response') != result:
            raise ContractError('Conflicting result cannot overwrite immutable attempt evidence')
        return prior['response']
    state.write(path, {'stage': task['stage'], 'received_at': now(),
                      'translation_attempts': task['translation_attempts'],
                      'review_attempts': task['review_attempts'], 'response': result})
    return result


def decision(state, task, stage, outcome, reason=None, findings=None):
    """Keep the gate's decision even when a later stage replaces task.findings."""
    path = f'state/tasks/{task["id"]}/decisions/{stage}.json'
    value = {'stage':stage, 'outcome':outcome, 'reason':reason, 'findings':findings or []}
    previous = state.read(path)
    if previous is not None:
        if previous != value:
            raise ContractError('Stage decision cannot overwrite prior audit evidence')
        return
    state.write(path, value)
