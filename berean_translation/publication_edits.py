"""Hash-verified accepted copies protect editable publication working files."""
from pathlib import Path
from .common import ContractError, digest, json_hash
from .html import split_article


def observed_files(state, publication):
    result = {}
    for field in ('html_path', 'metadata_path'):
        try:
            path = state.path(publication[field])
            result[field] = digest(path.read_bytes()) if path.exists() else None
        except (ContractError, OSError) as exc:
            result[field] = {'unavailable':type(exc).__name__}
    return result


def candidate(payload):
    if (not isinstance(payload, dict) or set(payload) != {'html', 'metadata'}
            or not isinstance(payload['html'], str)
            or not isinstance(payload['metadata'], dict)
            or set(payload['metadata']) != {'title', 'subtitle', 'section'}):
        raise ContractError('Invalid accepted publication snapshot')
    body, tail = split_article(payload['html'])
    return {'html': body, **payload['metadata']}, tail, payload['html']


def verify(payload, publication):
    candidate(payload)
    if (digest(payload['html']) != publication['html_sha256']
            or json_hash(payload['metadata']) != publication['metadata_sha256']):
        raise ContractError('Accepted publication snapshot does not match its recorded hashes')
    return payload


def accepted_payload(state, publication):
    path = publication.get('accepted_snapshot')
    if not isinstance(path, str) or path != f'state/publications/{Path(path).stem}.json':
        raise ContractError('Missing accepted publication snapshot')
    payload = state.read(path)
    if json_hash(payload) != Path(path).stem:
        raise ContractError('Accepted publication snapshot is missing or changed')
    return verify(payload, publication)


def remember(state, publication, payload):
    verify(payload, publication)
    path = f'state/publications/{json_hash(payload)}.json'
    existing = state.read(path)
    if existing is not None and existing != payload:
        raise ContractError('Accepted publication snapshot cannot be overwritten')
    if existing is None:
        state.write(path, payload)
    publication['accepted_snapshot'] = path


def recover_accepted(state, publication, gitstore, history=()):
    if publication.get('accepted_snapshot'):
        return accepted_payload(state, publication)
    # Original AI publications have an immutable successful task candidate.
    saved = state.read(f'state/tasks/{publication["task"]}/candidate.json')
    if isinstance(saved, dict) and set(saved) == {'html', 'title', 'subtitle', 'section'}:
        payload = {'html': saved['html'].strip() + '\n\n' + publication['notice_html'] + '\n',
                   'metadata': {key: saved[key] for key in ('title', 'subtitle', 'section')}}
        if (digest(payload['html']) == publication['html_sha256']
                and json_hash(payload['metadata']) == publication['metadata_sha256']):
            return payload
    # Legacy human-reviewed files predate snapshots. Their recorded human commit
    # is a provenance source, but its bytes still have to match both accepted hashes.
    if hasattr(gitstore, 'matching_publication'):
        return verify(gitstore.matching_publication(publication, history), publication)
    raise ContractError('Cannot recover a hash-verified last accepted publication; retain the prior deployment')
