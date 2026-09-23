"""Validate current English content and compute fingerprints locally; no AI calls."""
from __future__ import annotations
import argparse
import copy
import json
import sys
from pathlib import Path
from .common import ContractError
from .config import Config
from .source import SourceClient


def validate_checkout(config, checkout: Path, revision: str | None = None) -> dict:
    """Read one source checkout; editors never supply or maintain content hashes."""
    selected = copy.copy(config)
    selected.runtime = copy.deepcopy(config.runtime)
    if revision is not None:
        import re
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ContractError('Source revision must be a complete commit SHA')
        selected.runtime['source_branch'] = revision
    inventory = SourceClient(selected, checkout=checkout).discover()
    return {'revision': inventory['revision'], 'issues': len(inventory['issues']),
            'articles': len(inventory['articles']), 'article_snapshots_verified': len(inventory['articles']),
            'fingerprint_origin': inventory['fingerprint_origin']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--revision', help='Optional internal consistency assertion, not a maintainer requirement')
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
