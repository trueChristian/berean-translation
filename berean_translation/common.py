"""Strict JSON, safe paths and small durable filesystem operations."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


class ContractError(ValueError):
    """An input, source, translation, or state violates the repository contract."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def json_hash(value: Any) -> str:
    return digest(canonical(value))


def loads(text: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    def invalid(value):
        raise ContractError(f'Invalid JSON number: {value}')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as exc:
        raise ContractError(f'Invalid JSON: {exc}') from exc


def read_json(path: Path, default: Any = None) -> Any:
    return loads(path.read_bytes()) if path.exists() else default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n')


def uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError('not canonical')
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError(f'Invalid UUID: {value!r}') from exc
    return value


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise ContractError('Invalid relative path')
    part = Path(relative)
    if part.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ContractError(f'Unsafe path: {relative}')
    target = root / part
    if not target.resolve().is_relative_to(root.resolve()):
        raise ContractError(f'Escaping path: {relative}')
    for current in (target, *target.parents):
        if current == root.parent:
            break
        if current.is_symlink():
            raise ContractError(f'Symlink not allowed: {relative}')
    return target


def positive_money(value: str | float, maximum: float = 500.0) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ContractError('Budget must be a positive number in USD') from exc
    if not math.isfinite(number) or not 0 < number <= maximum:
        raise ContractError(f'Budget must be between zero and {maximum} USD')
    return number


def csv_values(value: str) -> list[str]:
    result = [x.strip() for x in re.split(r'[,\s]+', value.strip()) if x.strip()]
    if not result or len(result) != len(set(result)):
        raise ContractError('Selection must be nonempty and contain no duplicates')
    return result
