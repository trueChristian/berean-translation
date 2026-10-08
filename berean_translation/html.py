"""Strict fragment preservation and application-owned translation notices."""
from __future__ import annotations
import html
import re
from collections import Counter, defaultdict
from html.parser import HTMLParser
from itertools import zip_longest
from urllib.parse import urlsplit
from .common import ContractError

VOID = {'br', 'img', 'hr', 'wbr'}
ALLOWED = {'article','p','h1','h2','h3','h4','h5','h6','em','strong','b','i','u','s','mark',
           'blockquote','ul','ol','li','sup','sub','figure','figcaption','span','div','a',
           'section','aside','footer','small','cite','q','abbr','time','dl','dt','dd','table','thead','tbody','tr','td',
           'th','caption','pre','code', *VOID}
BLOCKS = {'p','h1','h2','h3','h4','h5','h6','figcaption','li','blockquote','dt','dd','aside','footer'}
TEXT_BLOCKS = BLOCKS | {'article','div','section','figure','ul','ol','dl','table',
                        'thead','tbody','tr','td','th','caption','pre'}


class Fragment(HTMLParser):
    def __init__(self, text: str, article_id: str):
        super().__init__(convert_charrefs=True)
        self.article_id = article_id
        self.stack = []
        self.signature = []
        self.signature_paths = []
        self.path_stack = []
        self.child_counts = [Counter()]
        self.text_parts = []
        self.text_by_block = defaultdict(list)
        self.images = []
        self.nonempty_blocks = []
        self.block_stack = []
        self.roots = 0
        self.feed(text)
        self.close()
        if self.stack or self.roots != 1:
            raise ContractError('Expected one complete outer article element')

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            raise ContractError(f'Forbidden HTML element: {tag}')
        pairs = dict(attrs)
        if len(pairs) != len(attrs):
            raise ContractError('Duplicate HTML attribute')
        if not self.stack:
            if tag != 'article' or self.roots:
                raise ContractError('Content outside the single article element')
            if pairs.get('data-article-id') != self.article_id:
                raise ContractError('Article UUID mismatch')
            self.roots += 1
        elif tag == 'article':
            raise ContractError('Nested article element')
        for key, value in attrs:
            if key == 'reversed':
                # HTML's ordered-list boolean attribute may be minimized,
                # empty, or repeat its name. It is never a general permission
                # for valueless attributes, nor valid on another element.
                if tag != 'ol' or (value is not None and value.lower() not in ('', 'reversed')):
                    raise ContractError('reversed must be a boolean attribute on an ordered list')
                # Keep the exact value (including None) in the immutable
                # signature below; translated lists cannot change direction.
                continue
            if value is None or key.startswith('on') or key in ('style','srcdoc','srcset'):
                raise ContractError(f'Forbidden or valueless HTML attribute: {key}')
            if not (key in ('id','class','alt','title','href','src','lang','dir','width','height',
                            'colspan','rowspan','scope','start','type','datetime','loading','decoding')
                    or key.startswith('data-') or key.startswith('aria-')):
                raise ContractError(f'Unsupported HTML attribute: {key}')
            if key == 'scope' and (tag != 'th' or value not in ('row', 'col', 'rowgroup', 'colgroup')):
                raise ContractError('Invalid table header scope')
            if key in ('href','src'):
                if any(ord(c) < 32 for c in value) or '\\' in value:
                    raise ContractError('Unsafe URL')
                parsed = urlsplit(value)
                if parsed.scheme not in ('','https','http','mailto') or value.startswith('//'):
                    raise ContractError('Unsafe URL scheme')
        if tag == 'img':
            if not re.fullmatch(r'/images/articles/[a-f0-9-]+-\d+\.(?:jpg|jpeg|png|webp|gif)', pairs.get('src','')):
                raise ContractError('Image must reference a shared canonical article asset')
            if 'alt' not in pairs:
                raise ContractError('Image is missing its alt attribute')
            self.images.append(pairs)
        # Textual accessibility attributes can be translated, but not added/removed.
        signature_attrs = [(key, '<translated>' if key in ('alt','title') else value) for key,value in attrs]
        self.child_counts[-1][tag] += 1
        path = (self.path_stack[-1] if self.path_stack else '') + f'/{tag}[{self.child_counts[-1][tag]}]'
        self.signature.append(('start', tag, tuple(sorted(signature_attrs))))
        self.signature_paths.append(path)
        if tag in BLOCKS:
            self.block_stack.append([len(self.signature), False])
        if tag not in VOID:
            self.stack.append(tag)
            self.path_stack.append(path)
            self.child_counts.append(Counter())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise ContractError(f'Malformed HTML closing tag: {tag}')
        self.stack.pop()
        self.signature.append(('end', tag))
        self.signature_paths.append(self.path_stack.pop())
        self.child_counts.pop()
        if tag in BLOCKS:
            self.nonempty_blocks.append(tuple(self.block_stack.pop()))

    def handle_data(self, data):
        if not self.stack and data.strip():
            raise ContractError('Text outside article')
        self.text_parts.append(data)
        if self.stack:
            # Scope clocks and references to the nearest structural text
            # container, including div/section/table cells. Inline emphasis
            # remains transparent, but sibling containers cannot share a clock.
            path = next(path for tag,path in zip(reversed(self.stack), reversed(self.path_stack))
                        if tag in TEXT_BLOCKS)
            self.text_by_block[path].append(data)
        if data.strip():
            for block in self.block_stack:
                block[1] = True

    def handle_comment(self, data):
        if not self.stack:
            raise ContractError('Comments must remain inside the source article')
        if '--' in data or '\x00' in data or data.startswith(('>', '->')) or data.endswith('-'):
            raise ContractError('Malformed HTML comment')
        # Existing archive audit notes are inert metadata, never translated text
        # or instructions. Their exact content and position are immutable.
        self.signature.append(('comment', data))
        self.child_counts[-1]['comment()'] += 1
        self.signature_paths.append(self.path_stack[-1] + f'/comment()[{self.child_counts[-1]["comment()"]}]')

    def handle_decl(self, decl):
        raise ContractError('Document declarations are forbidden')

    def handle_pi(self, data):
        raise ContractError('Processing instructions are forbidden')

    @property
    def text(self):
        return ' '.join(self.text_parts)


def validate_translation(source: dict, candidate: dict, *, language: str | None = None) -> Fragment:
    if not isinstance(candidate, dict) or set(candidate) != {'html','title','subtitle','section'}:
        raise ContractError('Translation must contain exactly html, title, subtitle, section')
    if not isinstance(candidate['html'], str):
        raise ContractError('HTML must be a string')
    original = Fragment(source['html'], source['article']['id'])
    translated = Fragment(candidate['html'], source['article']['id'])
    if original.signature != translated.signature:
        for index,(left,right) in enumerate(zip_longest(original.signature, translated.signature)):
            if left != right:
                left_path = original.signature_paths[index] if left is not None else '<end>'
                right_path = translated.signature_paths[index] if right is not None else '<end>'
                raise ContractError('HTML structure, IDs, links, or immutable attributes changed; '
                                    f'first difference at signature[{index}]: '
                                    f'source {left_path} {repr(left)[:240]}; '
                                    f'translation {right_path} {repr(right)[:240]}')
    if original.nonempty_blocks != translated.nonempty_blocks:
        raise ContractError('A substantive block was emptied or inserted')
    if not translated.text.strip():
        raise ContractError('Translation has no text')
    for left, right in zip(original.images, translated.images):
        if bool(left['alt'].strip()) != bool(right['alt'].strip()):
            raise ContractError('Image alt text was omitted or invented')
    for key in ('title','subtitle','section'):
        expected = source['article'].get(key)
        actual = candidate[key]
        if expected in (None, ''):
            if actual != expected:
                raise ContractError(f'Absent {key} must remain absent')
        elif not isinstance(actual, str) or not actual.strip():
            raise ContractError(f'Translated {key} must be nonempty text')
    return translated


def split_article(text: str) -> tuple[str, str]:
    # Parse the boundary: a closing-tag string inside a comment or attribute is
    # not the article's end and must not be mistaken for a review-notice boundary.
    offsets = [0]
    for line in text.split('\n')[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)

    class Boundary(HTMLParser):
        depth = 0
        end = None

        def handle_starttag(self, tag, attrs):
            if tag == 'article':
                self.depth += 1

        def handle_endtag(self, tag):
            if tag == 'article' and self.depth:
                self.depth -= 1
                if self.depth == 0 and self.end is None:
                    line, column = self.getpos()
                    start = offsets[line - 1] + column
                    self.end = text.index('>', start) + 1

    parser = Boundary(convert_charrefs=False)
    parser.feed(text)
    parser.close()
    if parser.end is None:
        raise ContractError('Missing article closing tag')
    return text[:parser.end].strip(), text[parser.end:].strip()


def notice(language: dict, article_id: str, model: str, english_route: str) -> str:
    url = english_route.format(article_id=article_id)
    if not url.startswith('/') or url.startswith('//') or any(x in url for x in ('..','\\','"','<','>')):
        raise ContractError('Invalid English article route')
    return (f'<aside class="translation-notice" data-translation-notice="ai" '
            f'lang="{html.escape(language["tag"], quote=True)}" dir="{language["dir"]}" role="note">\n'
            f'  <p>{html.escape(language["notice"].format(model=model))} '
            f'<a href="{html.escape(url, quote=True)}" data-source-article-id="{article_id}" '
            f'hreflang="en">{html.escape(language["english_label"])}</a></p>\n</aside>')


def human_notice(language: dict, article_id: str, english_route: str) -> str:
    settings = {**language, 'notice':language['reviewed_notice']}
    return notice(settings, article_id, '', english_route).replace(
        'data-translation-notice="ai"', 'data-translation-notice="human-reviewed"', 1)


def rewrite_export_urls(text: str, base: str, english_route: str, article_id: str) -> str:
    if not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*', base):
        raise ContractError('Base must be / or a safe slash-delimited path ending in /')
    route = english_route.format(article_id=article_id)
    replacements = []
    offsets = [0]
    for line in text.split('\n')[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)

    class URLs(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            raw = self.get_starttag_text()
            key = None
            if tag == 'img' and values.get('src','').startswith('/images/articles/'):
                key, old = 'src', values['src']
            elif tag == 'a' and values.get('data-source-article-id') == article_id and values.get('href') == route:
                key, old = 'href', values['href']
            if key:
                pattern = re.compile(r"(?<![\w:-])" + key + r"\s*=\s*([\"'])(.*?)\1", re.I | re.S)
                match = pattern.search(raw)
                if not match:
                    raise ContractError('Asset attribute must use a quoted value')
                line, column = self.getpos()
                start = offsets[line-1] + column + match.start(2)
                end = offsets[line-1] + column + match.end(2)
                replacements.append((start,end,html.escape(base.rstrip('/')+old,quote=True)))
        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag,attrs)

    parser = URLs(convert_charrefs=True)
    parser.feed(text)
    parser.close()
    for start,end,value in reversed(replacements):
        text = text[:start] + value + text[end:]
    return text
