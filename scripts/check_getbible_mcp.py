"""Explicit anonymous, read-only endpoint smoke; never run by offline tests."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.common import json_hash


def main():
    provider = GetBibleMCP()
    query = provider.query('kjv','John 4:15-16')
    print('PASS query_verses kjv v2 John 4:15-16', query['result_sha256'])
    for edition in ('kjv','luther1545'):
        chapter = provider.chapter(edition,43,4)
        print('PASS get_scripture',edition,'v2 John 4',
              'provider_sha1='+chapter['result']['hash'],
              'result_sha256='+chapter['result_sha256'])


if __name__ == '__main__':
    main()
