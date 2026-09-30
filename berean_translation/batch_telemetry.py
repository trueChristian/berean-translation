"""Allowlisted provider diagnostics and explicit provider/local batch clocks.

Provider lifecycle values stay in their original Unix-second representation.
Local ISO timestamps record when the worker observed or collected a result;
they must never stand in for an absent provider timestamp.
"""
from __future__ import annotations

REMOTE_STATUSES = {'validating','in_progress','finalizing','completed','failed',
                   'expired','cancelling','cancelled'}
REMOTE_TIMESTAMPS = ('created_at','in_progress_at','expires_at','finalizing_at',
                     'completed_at','failed_at','expired_at','cancelling_at','cancelled_at')

# Messages and arbitrary error codes can contain request text or credentials.
# Only these fixed codes/hints may be copied into durable diagnostics.
ERROR_HINTS = {
    'batch_expired':'The provider processing window expired; successful rows remain usable. Any retry needs an explicit new request.',
    'batch_cancelled':'The provider cancelled this request; no automatic replacement will be submitted.',
    'request_timeout':'The provider timed out this request; any retry needs an explicit new request.',
    'rate_limit_exceeded':'Check the provider project rate limits before requesting a retry.',
    'insufficient_quota':'Check the provider project billing and quota before requesting a retry.',
    'invalid_api_key':'Check the repository Actions secret and provider project access.',
    'model_not_found':'Check the pinned model name and provider project model access.',
    'context_length_exceeded':'Check the source size and configured model context/output limits.',
    'token_limit_exceeded':'Check the provider batch token limit before requesting a retry.',
    'invalid_json_line':'Check the indicated line in the persisted batch input file.',
    'duplicate_custom_id':'Check request identities in the persisted batch input file.',
    'invalid_request_error':'Check the persisted request parameters against the selected model.',
    'invalid_value':'Check the persisted request parameters against the selected model.',
    'unsupported_parameter':'Check the persisted request parameters against the selected model.',
    'server_error':'The provider reported a server error; any retry needs an explicit new request.',
    'internal_error':'The provider reported an internal error; any retry needs an explicit new request.',
}


def provider_error(error):
    error = error if isinstance(error,dict) else {}
    raw_code = error.get('code')
    code = raw_code if isinstance(raw_code,str) and raw_code in ERROR_HINTS else 'provider_error'
    result = {'code':code,'action':ERROR_HINTS.get(code,
        'Inspect this batch in the provider dashboard and its persisted input; no automatic replacement will be submitted.')}
    line = error.get('line')
    if type(line) is int and line > 0:
        result['line'] = line
    return result


def observe_batch(batch, remote, at):
    """Retain only bounded, structured provider facts; never invent old facts."""
    status = remote.get('status')
    batch['remote_status'] = status if isinstance(status,str) and status in REMOTE_STATUSES else 'unknown'
    batch['remote_observed_at'] = at
    for field in REMOTE_TIMESTAMPS:
        value = remote.get(field)
        if type(value) is int and value >= 0:
            batch['remote_'+field] = value
        elif field in remote and value is None:
            batch.setdefault('remote_'+field,None)
    counts = remote.get('request_counts')
    if isinstance(counts,dict):
        batch['remote_request_counts'] = {key:counts[key] for key in ('total','completed','failed')
            if type(counts.get(key)) is int and counts[key] >= 0}
    if 'errors' in remote:
        errors = remote.get('errors') or {}
        data = errors.get('data',[]) if isinstance(errors,dict) else []
        batch['remote_errors'] = [provider_error(error) for error in data[:20]] if isinstance(data,list) else []
        if isinstance(data,list) and len(data) > 20:
            batch['remote_errors_omitted'] = len(data)-20
        else:
            batch.pop('remote_errors_omitted',None)


def request_failure(row):
    """Return a safe, actionable diagnostic for an unsuccessful provider row."""
    if row.get('error'):
        return provider_error(row['error'])
    response = row.get('response')
    if not isinstance(response,dict):
        return {'code':'missing_response','action':'The provider returned no response for this request; inspect the batch before requesting a retry.'}
    status = response.get('status_code')
    if type(status) is int and status == 200:
        return None
    body = response.get('body')
    error = body.get('error') if isinstance(body,dict) else None
    result = provider_error(error)
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    return result


def failure_reason(diagnostic):
    status = diagnostic.get('http_status')
    return (f'Batch request failed: {diagnostic["code"]}' +
            (f' (HTTP {status})' if status else '') + '. ' + diagnostic['action'])
