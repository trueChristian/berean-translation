"""Inventory unexpected working files without publishing or interpreting them."""
from __future__ import annotations
import hashlib
import os
import stat
from pathlib import Path
from .common import ContractError

PATH = 'state/content-isolation.json'


def inventory(root):
    base = root / 'content'
    result = {}
    def observe(path):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {'kind': 'symlink'}
            return
        try:
            digest = hashlib.sha256()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    result[relative] = {'kind': 'unsupported_file_type'}
                    return
                for block in iter(lambda: stream.read(65536), b''):
                    digest.update(block)
            result[relative] = {'kind': 'file', 'sha256': digest.hexdigest()}
        except OSError as exc:
            result[relative] = {'kind': 'unavailable', 'error_type': type(exc).__name__}
    if base.is_symlink() or base.is_file():
        observe(base)
    elif base.exists():
        def unavailable(error):
            path = Path(error.filename)
            result[path.relative_to(root).as_posix()] = {'kind':'unavailable', 'error_type':type(error).__name__}
        for directory, names, files in os.walk(base, followlinks=False, onerror=unavailable):
            parent = Path(directory)
            for name in list(names):
                path = parent / name
                if path.is_symlink():
                    observe(path)
                    names.remove(name)
            for name in files:
                observe(parent / name)
    return dict(sorted(result.items()))


def expected_paths(state):
    return {publication[field] for record in state.records()
            if (publication := record.get('published'))
            for field in ('html_path', 'metadata_path')}


def unexpected(state, observed=None):
    expected = expected_paths(state)
    return {path: value for path, value in (inventory(state.root) if observed is None else observed).items()
            if path not in expected}


def synchronize(state):
    files = unexpected(state)
    value = {'format_version': '1', 'files': files}
    if files or state.read(PATH) is not None:
        if state.read(PATH) != value:
            state.write(PATH, value)


def validate(state, observed):
    files = unexpected(state, observed)
    recorded = state.read(PATH)
    if recorded is None and not files:
        return
    if (not isinstance(recorded, dict) or set(recorded) != {'format_version','files'}
            or recorded.get('format_version') != '1' or not isinstance(recorded.get('files'), dict)
            or any(recorded['files'].get(path) != value for path, value in files.items())):
        raise ContractError('Unexpected content files changed; run human-edit synchronization to isolate them')
    # Absent recorded paths are harmless: an automatic checkpoint deliberately
    # excludes untracked copies. They are never accepted/exported records.
