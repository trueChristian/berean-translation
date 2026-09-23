"""Validate every source article from an immutable checkout; no OpenAI calls."""
from __future__ import annotations
import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from .common import ContractError, canonical, safe_path
from .config import Config
from .source import SourceClient


def validate_checkout(config, checkout: Path, revision: str) -> dict:
    """Check every article from a clean, immutable source checkout without AI calls.

    CI first discovers the real upstream over HTTPS, then checks out exactly that
    revision. This second check covers the HTML contract, not just catalogue JSON.
    Source files are read as data; no source-repository scripts are executed.
    """
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ContractError('Source revision must be a complete commit SHA')
    checkout = checkout.resolve()
    try:
        actual = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                         stderr=subprocess.PIPE, text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain',
                                         '--untracked-files=all', '--', 'index.json', 'catalogue.json',
                                         'manifest.json', 'content/articles'], stderr=subprocess.PIPE,
                                        text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError('A readable Git source checkout is required') from exc
    if actual != revision or dirty:
        raise ContractError('Source checkout must be clean and match the exact selected revision')
    selected = copy.copy(config)
    selected.runtime = copy.deepcopy(config.runtime)
    selected.runtime['source_branch'] = revision
    repository = selected.runtime['source_repository']
    prefix = f'https://raw.githubusercontent.com/{repository}/{revision}/'

    def read_checkout(url):
        if url == f'https://api.github.com/repos/{repository}/commits/{revision}':
            return canonical({'sha': actual})
        if not url.startswith(prefix):
            raise ContractError('Unexpected source location in checkout validation')
        return safe_path(checkout, url[len(prefix):]).read_bytes()

    # Isolate the cache so a prior HTTP discovery cannot conceal modified files.
    with tempfile.TemporaryDirectory(prefix='berean-source-check-') as cache:
        selected.root = Path(cache)
        source = SourceClient(selected, fetch=read_checkout)
        inventory = source.discover()
        for article_id in inventory['articles']:
            try:
                source.snapshot(article_id)
            except ContractError as exc:
                raise ContractError(f'Source article {article_id}: {exc}') from exc
    return {'revision': revision, 'issues': len(inventory['issues']),
            'articles': len(inventory['articles']), 'article_snapshots_verified': len(inventory['articles'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args(argv)
    try:
        report = validate_checkout(Config(args.root.resolve()), args.checkout, args.revision)
    except (ContractError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
