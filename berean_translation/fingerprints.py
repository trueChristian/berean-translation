"""Translation-owned fingerprints computed from current English content.

No generated source manifest is read. The v1 fingerprint recipe is retained so
existing publications do not become stale solely because ownership moved here.
"""
from __future__ import annotations
from html.parser import HTMLParser
import re
from .common import digest, json_hash

VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
BLOCK = {'p','h1','h2','h3','h4','h5','h6','li','blockquote','div','section','article','figure',
         'figcaption','br','td','th','tr','dt','dd'}


class _Fingerprint(HTMLParser):
    """Fingerprint already validated HTML without serializing or editing it."""
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.structure = []
        self.text = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'img':
            attributes['src'] = str(attributes['src']).rsplit('/', 1)[-1]
        self.structure.append(['start', tag, sorted(attributes.items())])
        if tag in BLOCK:
            self.text.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.structure.append(['end', tag])
        if tag in BLOCK:
            self.text.append('\n')

    def handle_data(self, data):
        self.text.append(data)


def article_fingerprints(article: dict, raw: bytes) -> dict:
    """Compute compatibility locally; image pixels and grouping IDs are excluded."""
    parsed = _Fingerprint(raw.decode('utf-8'))
    metadata = {key: article.get(key) for key in (
        'title','subtitle','section','byline','publication_note','source_labels')}
    metadata['images'] = [{key: image.get(key) for key in ('alt','caption','credit')}
                          for image in article['images']]
    return {
        'html_sha256': digest(raw),
        'text_sha256': digest(re.sub(r'[ \t\r\n\f]+', ' ', ''.join(parsed.text)).strip()),
        'structure_sha256': json_hash(parsed.structure),
        'metadata_sha256': json_hash(article),
        'translation_metadata_sha256': json_hash(metadata),
    }
