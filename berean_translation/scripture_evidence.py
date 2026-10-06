"""Versioned trusted Scripture evidence and exact-substring output claims.

Source alignment is deliberately conservative. Semantic equivalence of a partial
KJV quotation and the chosen target substring remains an independent-review gate.
No source HTML, printed reference, human publication or old campaign is rewritten.
"""
from __future__ import annotations
import copy
import re
from collections import Counter
from datetime import datetime, timezone
from .common import ContractError, canonical, digest, json_hash, read_json, now
from .html import Fragment, decimal_digits
from .reference_notation import reference_mentions
from .scripture_books import CORE_BOOK_NAMES
from .scripture_provider import GetBibleMCP, ScriptureProviderError
from .scripture_scope import ScopeFragment, ScopeProofError, StructuralScope

VERSION = '1'
SELECTION_NORMALIZATION_VERSION = '2'
SELECTION_NORMALIZATION_VERSIONS = ('1', '2')
SOURCE_ASSOCIATION_VERSION = '2'
MAX_UNCLAIMED_ALIGNMENT_WORK = 2_000_000
DEFAULT_MAX_EVIDENCE_BYTES = 500_000
MAX_SELECTION_AUDIT_BYTES = 32768
APPROVED_EDITIONS = {'en':'kjv','af':'aov','ar':'arabicsv','de':'luther1545',
    'el':'moderngreek','es':'sse','fr':'martin','he':'modernhebrew','it':'giovanni',
    'ko':'korean','nb':'bibelselskap','nl':'statenvertaling','pt':'almeida',
    'ru':'synodal','sv':'swekarlxii1873','zh-Hans':'cus',
    'bn':None,'hi':None,'id':None,'sw':None,'ur':None}
BOOK_NUMBERS = {row[0]: i + 1 for i, row in enumerate(CORE_BOOK_NAMES)}
WORDS = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
QUOTES = re.compile(r'“([^”]+)”|"([^"\n]+)"|(?<!\w)‘(.+?)’(?!\w)|«([^»]+)»|„([^“]+)“|「([^」]+)」|『([^』]+)』')
ELLIPSIS = re.compile(r'(\.(?:\s*\.){2,}|…)')


class ScriptureAttention(ContractError):
    """No paid request may be created from unresolved evidence."""
    def __init__(self, reason, detail):
        self.reason = reason
        super().__init__(f'Scripture attention: {reason}: {detail}')


def frozen_policy(campaign):
    if 'scripture_quotes' not in campaign:
        return None
    value = campaign['scripture_quotes']
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ContractError('Unsupported frozen Scripture quotation contract')
    if value.get('selection_normalization_version') not in (None, *SELECTION_NORMALIZATION_VERSIONS):
        raise ContractError('Unsupported frozen Scripture selection normalization contract')
    if value.get('source_association_version') not in (None, '1', SOURCE_ASSOCIATION_VERSION):
        raise ContractError('Unsupported frozen Scripture source association contract')
    return value


def policy(root, *, max_evidence_bytes=DEFAULT_MAX_EVIDENCE_BYTES):
    path = root / 'data/scripture-translations.json'
    mapping = read_json(path)
    provenance = read_json(root / 'docs/third-party/getbible-provenance.json')
    if (not mapping or not provenance or digest(path.read_bytes()) != provenance['file_sha256']
            or {tag:entry.get('abbreviation') for tag,entry in mapping['locales'].items()} != APPROVED_EDITIONS):
        raise ScriptureAttention('edition_map_changed', 'The pinned approved website map failed validation')
    return {'version': VERSION, 'api_version': 'v2', 'max_evidence_bytes': max_evidence_bytes,
            'edition_map': mapping, 'edition_map_provenance': provenance,
            'edition_map_sha256': json_hash(mapping), 'max_selection_audit_bytes': MAX_SELECTION_AUDIT_BYTES,
            'selection_normalization_version': SELECTION_NORMALIZATION_VERSION,
            'source_association_version': SOURCE_ASSOCIATION_VERSION,
            'prompt_addendum': (root / 'prompts/scripture-quotes-v1.txt').read_text(encoding='utf-8')}


def _words(value):
    return [m.group().replace('’', "'").casefold() for m in WORDS.finditer(value)]


def _reference(key):
    match = re.fullmatch(r'(.+) ([1-9][0-9]*):([0-9,\-]+)', key)
    if not match or match[1] not in BOOK_NUMBERS:
        raise ScriptureAttention('unsupported_reference', key)
    verses = []
    for value in match[3].split(','):
        ends = [int(x) for x in value.split('-')]
        if len(ends) > 2 or min(ends) < 1 or ends[-1] < ends[0] or ends[-1] > 200:
            raise ScriptureAttention('unsupported_reference', key)
        verses.extend(range(ends[0], ends[-1] + 1))
    if verses != sorted(set(verses)):
        raise ScriptureAttention('ambiguous_reference', key)
    return BOOK_NUMBERS[match[1]], int(match[2]), verses


def _unique_alignment(quote, verses):
    """Align source word spans uniquely, preserving ordered ellipsis fragments."""
    split = ELLIPSIS.split(quote)
    fragments = [value for i,value in enumerate(split) if i % 2 == 0 and _words(value)]
    if not fragments:
        return None
    corpus, coordinates = [], []
    for verse in verses:
        for match in WORDS.finditer(verse['text']):
            corpus.append(match.group().replace('’', "'").casefold())
            coordinates.append({'verse':verse['verse'], 'start':match.start(), 'end':match.end()})
    alignments, previous = [], -1
    for fragment in fragments:
        tokens = _words(fragment)
        positions = [i for i in range(len(corpus)-len(tokens)+1) if corpus[i:i+len(tokens)] == tokens]
        if len(positions) != 1 or positions[0] <= previous:
            return None
        start, end = positions[0], positions[0] + len(tokens)
        # Short phrases are too weak unless they constitute an entire verse.
        if len(tokens) < 3 and not any(tokens == _words(v['text']) for v in verses):
            return None
        previous = end - 1
        alignments.append({'source_text': fragment.strip(), 'word_start': start, 'word_end': end,
                           'start':coordinates[start], 'end':coordinates[end-1]})
    complete = (len(fragments) == 1 and len(split) == 1
                and _words(quote) == corpus)
    return {'kind':'complete' if complete else 'partial', 'fragments':alignments,
            'ellipsis': [value for i,value in enumerate(split) if i % 2],
            'leading_ellipsis': bool(split and not _words(split[0])),
            'trailing_ellipsis': bool(split and not _words(split[-1]))}


def _quote_spans(text, block):
    spans = [(m.start(group), m.end(group), m[group]) for m in QUOTES.finditer(text)
             for group in range(1,8) if m[group] is not None]
    if not spans and '/blockquote[' in block:
        mentions = reference_mentions(text, 'eng')
        if len(mentions) == 1:
            left,right,_ = mentions[0]
            before,after = text[:left].strip(' \n\t()—–-'),text[right:].strip(' \n\t()—–-.;')
            if before and not after:
                start = text.index(before)
                spans = [(start,start+len(before),before)]
    return spans


def _selected(evidence, numbers):
    values = {v['verse']:v for v in evidence['result']['data']['verses']}
    if any(number not in values for number in numbers):
        raise ScriptureAttention('missing_verse', 'The selected edition does not contain every cited verse')
    return [copy.deepcopy(values[number]) for number in numbers]


def _archive_chapter(evidence):
    result = evidence['result']
    cache = result.get('cache') or {}
    if cache.get('cacheable') is not True or not cache.get('expires_at'):
        raise ScriptureAttention('unarchivable_response', 'Provider cache policy does not permit freezing this response')
    try:
        expiry = datetime.fromisoformat(cache['expires_at'].replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError) as exc:
        raise ScriptureAttention('invalid_expiry','Provider expiry is malformed') from exc
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ScriptureAttention('evidence_expired','Provider response has no remaining freshness')
    return evidence


def build_evidence(source, language_tag, frozen_policy, provider=None):
    if frozen_policy.get('version') != VERSION:
        raise ScriptureAttention('unknown_contract', str(frozen_policy.get('version')))
    association = frozen_policy.get('source_association_version')
    if association == SOURCE_ASSOCIATION_VERSION:
        return _build_associated_evidence(source, language_tag, frozen_policy, provider)
    if association not in (None, '1'):
        raise ScriptureAttention('unknown_contract', str(association))
    mapping = frozen_policy['edition_map']
    if json_hash(mapping) != frozen_policy['edition_map_sha256']:
        raise ScriptureAttention('edition_map_changed', 'Frozen map hash differs')
    edition = mapping['locales'].get(language_tag)
    if not edition:
        raise ScriptureAttention('missing_edition', language_tag)
    article_id = source['article']['id']
    blocks = {key:''.join(parts) for key,parts in Fragment(source['html'], article_id).text_by_block.items()}
    bundle = {'version': VERSION, 'source_sha256':json_hash(source), 'article_id':article_id,
              'language_tag':language_tag, 'edition':copy.deepcopy(edition),
              'edition_map_sha256':frozen_policy['edition_map_sha256'],
              'edition_map_provenance':copy.deepcopy(frozen_policy['edition_map_provenance']),
              'created_at':now(), 'lookups':{}, 'quotes':[], 'references':[],
              'classification_limit':'Uncited/unmarked quotations require independent reviewer detection; never synthesize them.'}
    provider = provider or GetBibleMCP()
    def chapter(abbreviation, book, number):
        identity = f'{abbreviation}/{book}/{number}'
        if identity not in bundle['lookups']:
            try:
                bundle['lookups'][identity] = _archive_chapter(provider.chapter(abbreviation, book, number))
            except ScriptureProviderError as exc:
                raise ScriptureAttention('fetch_failed', str(exc)) from exc
        if len(canonical(bundle)) > frozen_policy['max_evidence_bytes']:
            raise ScriptureAttention('evidence_size_limit', 'Complete evidence exceeds the frozen bound; no text was truncated')
        return identity, bundle['lookups'][identity]
    for block,text in blocks.items():
        mentions = reference_mentions(text, 'eng')
        spans = _quote_spans(text, block)
        if not mentions:
            continue  # Ordinary quotation/prose; independent review still checks unattributed Scripture.
        # Unsupported bare or malformed references alongside quotations cannot be ignored.
        if spans and (len(mentions) != 1):
            raise ScriptureAttention('ambiguous_quote_reference', block)
        for left,right,reference in mentions:
            reference_record = {'block':block, 'start':left, 'end':right,
                                'printed_text':text[left:right], 'identity':reference,
                                'classification':'reference_only'}
            bundle['references'].append(reference_record)
            if not spans:
                # Reference-only prose is not a Bible replacement. Check for an
                # unmarked complete verse before deciding it is only an allusion.
                try:
                    book,number,numbers = _reference(reference)
                except ScriptureAttention:
                    continue
                english_id,english = chapter('kjv', book, number)
                english_verses = _selected(english, numbers)
                tokens = list(WORDS.finditer(text))
                normalized = _words(text)
                wanted = _words(' '.join(v['text'] for v in english_verses))
                matches = [i for i in range(len(tokens)-len(wanted)+1) if normalized[i:i+len(wanted)] == wanted]
                if len(matches) == 1:
                    begin = tokens[matches[0]].start()
                    finish = tokens[matches[0]+len(wanted)-1].end()
                    spans = [(begin,finish,text[begin:finish])]
                elif re.search(r'\b(?:written|saith|scripture says|Bible says)\b',text,re.I):
                    raise ScriptureAttention('unmarked_quote_scope', block)
                else:
                    continue
            if edition.get('status') != 'available' or not edition.get('abbreviation'):
                raise ScriptureAttention('missing_edition', f'{language_tag}: {edition.get("review_note", "")}')
            book,number,numbers = _reference(reference)
            # Chapter/verse identities are not interchangeable between editions.
            # Psalm numbering/title conventions are known to differ; no guessed offset.
            if book == 19 and edition['abbreviation'] != 'kjv':
                raise ScriptureAttention('unverified_versification', f'{reference} / {edition["abbreviation"]}')
            english_id,english = chapter('kjv', book, number)
            english_verses = _selected(english, numbers)
            for start,end,quote in spans:
                alignment = _unique_alignment(quote, english_verses)
                if alignment is None:
                    whole = _unique_alignment(quote, english['result']['data']['verses'])
                    reason = 'printed_reference_mismatch' if whole else 'ambiguous_source_quote'
                    raise ScriptureAttention(reason, f'{reference}: {quote[:160]}')
                target_id,target = chapter(edition['abbreviation'],book,number)
                target_verses = _selected(target,numbers)
                reference_record['classification'] = 'scripture_quotation'
                bundle['quotes'].append({'id':f'q{len(bundle["quotes"])+1}', 'block':block,
                    'source_start':start, 'source_end':end, 'source_quote':quote,
                    'delimited':any(m.start(group) == start and m.end(group) == end for m in QUOTES.finditer(text)
                                     for group in range(1,8) if m[group] is not None),
                    'reference':reference, 'printed_reference':reference_record['printed_text'],
                    'book':book, 'chapter':number, 'verses':numbers,
                    'english_lookup':english_id,'target_lookup':target_id,
                    'english_verses':english_verses,'target_verses':target_verses,
                    'alignment':alignment,'correspondence':'same numeric addresses; semantic correspondence requires independent review'})
    if len(canonical(bundle)) > frozen_policy['max_evidence_bytes']:
        raise ScriptureAttention('evidence_size_limit','Complete evidence exceeds its frozen bound; no text was truncated')
    return bundle


def _build_associated_evidence(source, language_tag, contract, provider):
    """New requests only: independently scoped quotations, never block joins."""
    from .scripture_association import (associate, AssociationError, structure_hash,
                                        delimiter_topology, unclaimed_marked_spans)
    mapping = contract['edition_map']
    if json_hash(mapping) != contract['edition_map_sha256']:
        raise ScriptureAttention('edition_map_changed', 'Frozen map hash differs')
    edition = mapping['locales'].get(language_tag)
    if not edition:
        raise ScriptureAttention('missing_edition', language_tag)
    try:
        parsed, references, scopes, ordinary = associate(source['html'], source['article']['id'])
    except AssociationError as exc:
        raise ScriptureAttention(exc.reason, str(exc)) from exc
    except ScopeProofError as exc:
        raise ScriptureAttention('unmarked_quote_scope', str(exc)) from exc
    bundle = {'version': VERSION, 'source_association_version': SOURCE_ASSOCIATION_VERSION,
              'source_sha256': json_hash(source), 'article_id': source['article']['id'],
              'source_structure_sha256': structure_hash(parsed),
              'source_delimiter_topology': {block: delimiter_topology(parsed.blocks[block])
                                            for block in dict.fromkeys(q['block'] for q in scopes)},
              'language_tag': language_tag, 'edition': copy.deepcopy(edition),
              'edition_map_sha256': contract['edition_map_sha256'],
              'edition_map_provenance': copy.deepcopy(contract['edition_map_provenance']),
              'created_at': now(), 'lookups': {}, 'quotes': [], 'references': references,
              'classification_limit': 'Only explicit scopes establish Scripture quotations; uncited/unmarked quotations and allusions require independent reviewer detection.'}
    if scopes and (edition.get('status') != 'available' or not edition.get('abbreviation')):
        raise ScriptureAttention('missing_edition', f'{language_tag}: {edition.get("review_note", "")}')
    for scope in scopes:
        if re.search(r'[\[\]{}]', scope['source_quote']):
            raise ScriptureAttention('source_quote_annotation', f'{scope["reference"]}: {scope["source_quote"][:160]}')
    provider = provider or GetBibleMCP()
    def chapter(abbreviation, book, number):
        identity = f'{abbreviation}/{book}/{number}'
        if identity not in bundle['lookups']:
            try:
                bundle['lookups'][identity] = _archive_chapter(provider.chapter(abbreviation, book, number))
            except ScriptureProviderError as exc:
                raise ScriptureAttention('fetch_failed', str(exc)) from exc
        if len(canonical(bundle)) > contract['max_evidence_bytes']:
            raise ScriptureAttention('evidence_size_limit', 'Complete evidence exceeds its frozen bound; no text was truncated')
        return identity, bundle['lookups'][identity]
    for scope in scopes:
        reference, quote = scope['reference'], scope['source_quote']
        book, number, numbers = _reference(reference)
        # Brackets are authored annotations, never disposable punctuation.
        if re.search(r'[\[\]{}]', quote):
            raise ScriptureAttention('source_quote_annotation', f'{reference}: {quote[:160]}')
        if book == 19 and edition['abbreviation'] != 'kjv':
            raise ScriptureAttention('unverified_versification', f'{reference} / {edition["abbreviation"]}')
        english_id, english = chapter('kjv', book, number)
        english_verses = _selected(english, numbers)
        alignment = _unique_alignment(quote, english_verses)
        if alignment is None:
            whole = _unique_alignment(quote, english['result']['data']['verses'])
            reason = 'printed_reference_mismatch' if whole else 'ambiguous_source_quote'
            raise ScriptureAttention(reason, f'{reference}: {quote[:160]}')
        target_id, target = chapter(edition['abbreviation'], book, number)
        bundle['quotes'].append({**scope, 'id': f'q{len(bundle["quotes"])+1}',
            'book': book, 'chapter': number, 'verses': numbers,
            'english_lookup': english_id, 'target_lookup': target_id,
            'english_verses': english_verses, 'target_verses': _selected(target, numbers),
            'alignment': alignment,
            'correspondence': 'same numeric addresses; semantic correspondence requires independent review'})
    # Retain English evidence for explicit allusions without discovering a
    # quotation by an arbitrary matching substring inside their prose.
    for ref in references:
        if ref['classification'] != 'reference_only':
            continue
        try:
            book, number, numbers = _reference(ref['identity'])
        except ScriptureAttention:
            continue
        _, english = chapter('kjv', book, number)
        _selected(english, numbers)
    # An owned reference must not hide a different genuine marked fragment.
    # Already-fetched English chapters provide only a fail-closed backstop:
    # never infer a missing printed citation or synthesize an output selection.
    english_chapters = [(identity, english['result']['data']['verses'])
                        for identity, english in bundle['lookups'].items() if identity.startswith('kjv/')]
    work = 0
    checked, blocks = set(), {}
    for ref in references:
        identity = (ref['block'], ref['identity'])
        if identity in checked:
            continue
        checked.add(identity)
        try:
            book, number, numbers = _reference(ref['identity'])
        except ScriptureAttention:
            continue
        english = bundle['lookups'][f'kjv/{book}/{number}']
        verses = _selected(english, numbers)
        wanted_text = ' '.join(v['text'] for v in verses)
        wanted = _words(wanted_text)
        text = parsed.blocks[ref['block']]
        if ref['block'] not in blocks:
            tokens = list(WORDS.finditer(text))
            blocks[ref['block']] = (tokens, [m.group().replace('’', "'").casefold() for m in tokens])
        tokens, normalized = blocks[ref['block']]
        work += (max(1, len(wanted)) * len(normalized) + len(wanted) + len(normalized)
                 + 3 * (len(text) + len(wanted_text)))
        if work > MAX_UNCLAIMED_ALIGNMENT_WORK:
            raise ScriptureAttention('source_association_limit', 'Complete cited-verse coverage exceeds its versioned work bound')
        if not wanted:
            continue
        for index in range(len(normalized)-len(wanted)+1):
            if normalized[index:index+len(wanted)] != wanted:
                continue
            begin, end = tokens[index].start(), tokens[index+len(wanted)-1].end()
            if not any(q['block'] == ref['block'] and q['source_start'] <= begin
                       and end <= q['source_end'] for q in scopes):
                raise ScriptureAttention('unmarked_quote_scope',
                    f'{ref["identity"]}: a complete cited verse occurs outside every proved quotation scope')
    for span in unclaimed_marked_spans(parsed, scopes):
        tokens = len(_words(span['text']))
        for identity, verses in english_chapters:
            corpus_tokens = sum(len(_words(v['text'])) for v in verses)
            # The product conservatively bounds slice comparisons across all
            # ellipsis fragments; character/token terms charge normalization,
            # tokenization and coordinate construction as well.
            work += (max(1, tokens) * corpus_tokens + tokens + corpus_tokens
                     + 3 * (len(span['text']) + sum(len(v['text']) for v in verses)))
            if work > MAX_UNCLAIMED_ALIGNMENT_WORK:
                raise ScriptureAttention('source_association_limit', 'Unclaimed quotation alignment exceeds its versioned work bound')
            if _unique_alignment(span['text'], verses) is not None:
                raise ScriptureAttention('unassociated_source_quote',
                    f'{span["block"]}: marked source text matches {identity} without its own proved citation: {span["text"][:160]}')
    if len(canonical(bundle)) > contract['max_evidence_bytes']:
        raise ScriptureAttention('evidence_size_limit', 'Complete evidence exceeds its frozen bound; no text was truncated')
    return bundle


def freeze_scripture_evidence(engine, source, language, *, frozen_policy=None, provider=None, language_tag=None):
    """Acceptance hook, AFTER human exclusion, BEFORE reservation/paid requests."""
    record = engine.state.record(language, source['article']['id'])
    publication = record.get('published') or {}
    if (publication.get('human_reviewed') or publication.get('edit_issue')
            or engine.human_protected(language, source['article']['id'])):
        raise ScriptureAttention('human_controlled', 'Human work is excluded from every Scripture AI path')
    frozen_policy = frozen_policy or policy(engine.config.root)
    provider = provider or getattr(engine, 'scripture_provider', None)
    boundary = getattr(engine, 'continue_work', None)
    if boundary is not None:
        delegate = provider or GetBibleMCP()
        class BoundedPrefetch:
            def chapter(self, *args):
                if not boundary():
                    raise ScriptureAttention('prefetch_wait_budget', 'Continue evidence collection in a later worker window')
                return delegate.chapter(*args)
        provider = BoundedPrefetch()
    bundle = build_evidence(source, language_tag or engine.config.languages[language]['tag'], frozen_policy, provider)
    sha = json_hash(bundle)
    path = f'state/scripture/{sha}.json'
    existing = engine.state.read(path)
    if existing is not None and existing != bundle:
        raise ScriptureAttention('evidence_changed', path)
    if existing is None:
        engine.state.write(path,bundle)
    return {'scripture_evidence_path':path,'scripture_evidence_sha256':sha}


def load_evidence(state, task, *, require_fresh=False):
    path,sha = task.get('scripture_evidence_path'),task.get('scripture_evidence_sha256')
    if not isinstance(sha,str) or path != f'state/scripture/{sha}.json':
        raise ScriptureAttention('missing_evidence','A versioned task must have frozen evidence')
    evidence = state.read(path)
    if not evidence or json_hash(evidence) != sha or evidence['source_sha256'] != json_hash(state.source(task)):
        raise ScriptureAttention('evidence_changed','Evidence or its English snapshot is missing/changed')
    contract = frozen_policy(state.read(f'state/campaigns/{task["campaign"]}.json'))
    version = evidence.get('source_association_version', '1')
    if (version not in ('1', SOURCE_ASSOCIATION_VERSION) or contract is None
            or version != contract.get('source_association_version', '1')):
        raise ScriptureAttention('evidence_changed', 'Evidence and frozen source association contracts disagree')
    if require_fresh:
        for lookup in evidence['lookups'].values():
            try:
                expiry = datetime.fromisoformat(lookup['result']['cache']['expires_at'].replace('Z','+00:00'))
            except (KeyError,ValueError,TypeError) as exc:
                raise ScriptureAttention('invalid_expiry','Provider expiry is missing or malformed') from exc
            if expiry.tzinfo is None:
                raise ScriptureAttention('invalid_expiry','Provider expiry requires a timezone')
            if expiry <= datetime.now(timezone.utc):
                raise ScriptureAttention('evidence_expired','Create fresh evidence in a new campaign; never change frozen history')
    return evidence


PART_SCHEMA = {'type':'object','additionalProperties':False,
    'properties':{key:{'type':'integer'} for key in ('verse','start','end')},
    'required':['verse','start','end']}
SELECTION_SCHEMA = {'type':'array','items':{'type':'object','additionalProperties':False,
    'properties':{'quote_id':{'type':'string'},'block':{'type':'string'},
        'start':{'type':'integer'},'end':{'type':'integer'},
        'fragments':{'type':'array','items':{'type':'array','items':PART_SCHEMA}}},
    'required':['quote_id','block','start','end','fragments']}}


def _selection_text(quote, selection, *, allow_outer_whitespace=False):
    fragments = selection.get('fragments')
    alignment = quote['alignment']
    if not isinstance(fragments,list) or len(fragments) != len(alignment['fragments']):
        raise ScriptureAttention('selection_shape','Source ellipsis fragments must be preserved one for one')
    target = {v['verse']:v['text'] for v in quote['target_verses']}
    rendered, previous = [], None
    for fragment in fragments:
        if not isinstance(fragment,list) or not fragment:
            raise ScriptureAttention('selection_shape','Empty target selection')
        pieces = []
        for part in fragment:
            if (not isinstance(part,dict) or set(part) != {'verse','start','end'}
                    or any(type(part[key]) is not int for key in part)):
                raise ScriptureAttention('selection_shape','Expected exact integer verse/text offsets')
            verse,start,end = part['verse'],part['start'],part['end']
            text = target.get(verse)
            if text is None or not 0 <= start < end <= len(text):
                raise ScriptureAttention('selection_outside_evidence','Target span is outside the approved cited verses')
            if previous and (verse < previous[0] or (verse == previous[0] and start < previous[1])):
                raise ScriptureAttention('selection_order','Target spans may not overlap or reverse verse order')
            # No omission within one fragment; omissions require a source ellipsis.
            if pieces:
                omitted_boundary = (bool(target[previous[0]][previous[1]:].strip() or text[:start].strip())
                                    if allow_outer_whitespace else
                                    previous[1] != len(target[previous[0]]) or start != 0)
                if verse != previous[0] + 1 or omitted_boundary:
                    raise ScriptureAttention('unauthorized_omission','Discontinuous target fragments require a source ellipsis')
            previous = (verse,end)
            pieces.append(text[start:end].strip())
        rendered.append(' '.join(pieces))
    if alignment['kind'] == 'complete':
        expected = ' '.join(v['text'].strip() for v in quote['target_verses'])
        if rendered != [expected]:
            raise ScriptureAttention('incomplete_verse_selection','A complete source verse requires its complete target verse')
    # Every separator comes from the printed source; no new omission is invented.
    separators = alignment['ellipsis']
    output = ''
    offset = 0
    if alignment['leading_ellipsis']:
        output = separators[0] + ' '
        offset = 1
    output += rendered[0]
    for index,fragment in enumerate(rendered[1:]):
        output += ' ' + separators[offset+index] + ' ' + fragment
    if alignment['trailing_ellipsis']:
        output += ' ' + separators[-1]
    return output


def _output_quote_spans(text, block):
    """Ignore only delimiter padding, never punctuation or internal whitespace.

    In particular, French guillemets conventionally enclose padding spaces.
    The selected verse text already strips its outer whitespace; its scope must
    use the same boundary without changing any candidate or source bytes.
    """
    for start,end,value in _quote_spans(text,block):
        left = len(value) - len(value.lstrip())
        right = len(value.rstrip())
        if left < right:
            yield start + left, start + right, value[left:right]


def _bounded_selection_input(selections, maximum):
    # Numeric claims are untrusted metadata. The serialized input size and
    # shape bound parsing work; candidate text length is not an integer limit.
    try:
        within_bound = len(canonical(selections)) <= maximum
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ScriptureAttention('selection_size_limit','Selection input exceeds its frozen resource bound') from exc
    if not within_bound:
        raise ScriptureAttention('selection_size_limit','Selection input exceeds its frozen resource bound')


def _structural_scope(evidence, candidate, source):
    from .scripture_association import AssociationError
    try:
        if evidence.get('source_association_version') == SOURCE_ASSOCIATION_VERSION:
            from .scripture_association import AssociationScope
            return AssociationScope(evidence, source, candidate)
        return StructuralScope(evidence,source,candidate)
    except AssociationError as exc:
        raise ScriptureAttention(exc.reason, str(exc)) from exc
    except ScopeProofError as exc:
        raise ScriptureAttention('quote_scope_changed',str(exc)) from exc


def _prove_scope(scope, quote, expected):
    try:
        return scope.prove(quote,expected)
    except ScopeProofError as exc:
        raise ScriptureAttention('quote_scope_changed',str(exc)) from exc


def _selection_blocks(evidence, candidate, source, expanded):
    if evidence.get('source_association_version', '1') not in ('1', SOURCE_ASSOCIATION_VERSION):
        raise ScriptureAttention('unknown_contract', 'Unsupported evidence source association contract')
    associated = evidence.get('source_association_version') == SOURCE_ASSOCIATION_VERSION
    scope = (_structural_scope(evidence,candidate,source) if associated or (expanded
             and any(not quote.get('delimited') for quote in evidence['quotes'])) else None)
    try:
        parsed = (scope.target if scope is not None else
                  (ScopeFragment if expanded else Fragment)(candidate['html'],evidence['article_id']))
    except ScopeProofError as exc:
        raise ScriptureAttention('quote_scope_changed',str(exc)) from exc
    return {key:''.join(parts) for key,parts in parsed.text_by_block.items()},scope


def check_selections(evidence, candidate, selections, *, require_exact_citations=False,
                     normalization_version='1', source=None, max_selection_bytes=MAX_SELECTION_AUDIT_BYTES):
    expanded = normalization_version == '2'
    associated = evidence.get('source_association_version') == SOURCE_ASSOCIATION_VERSION
    if expanded:
        _bounded_selection_input(selections,max_selection_bytes)
    if not isinstance(selections,list) or len(selections) != len(evidence['quotes']):
        raise ScriptureAttention('missing_selections','Every identified Scripture quotation needs one exact output claim')
    blocks,scope = _selection_blocks(evidence,candidate,source,expanded)
    # Existing numeric gates do not know every target language's book names.
    # Only provider-attested names and the existing reviewed aliases qualify.
    quoted_blocks = {quote['block'] for quote in evidence['quotes']}
    for block_path in quoted_blocks:
        if associated:
            from .scripture_association import mentions
            actual = Counter(key for _,_,key in mentions(blocks.get(block_path, ''), evidence))
        elif expanded:
            from .scripture_citations import quotation_reference_mentions
            actual = Counter(key for _,_,key in quotation_reference_mentions(
                evidence, blocks.get(block_path, '')))
        else:
            text = decimal_digits(blocks.get(block_path, ''))
            for quote in evidence['quotes']:
                if quote['block'] != block_path:
                    continue
                native = evidence['lookups'][quote['target_lookup']]['result']['data']['book_name']
                canonical_book = CORE_BOOK_NAMES[quote['book']-1][0]
                text = re.sub(re.escape(native) + r'(?=\s*[0-9])', canonical_book+' ', text, flags=re.I)
            language = {'de':'deu','he':'heb'}.get(evidence['language_tag'],'eng')
            actual = Counter(key for _,_,key in reference_mentions(text,language))
        exact_citations = require_exact_citations or expanded
        required = Counter(item['identity'] for item in evidence['references']
                           if item['block'] == block_path
                           and (exact_citations or item['classification'] == 'scripture_quotation'))
        if (any(actual[key] != count for key,count in required.items())
                or (exact_citations and actual != required)):
            raise ScriptureAttention('citation_identity_changed','Quoted Scripture must retain the original book/chapter/verse identity and count')
    claims = {}
    intervals = {}
    for selection in selections:
        if (not isinstance(selection,dict) or set(selection) != {'quote_id','block','start','end','fragments'}
                or not isinstance(selection['quote_id'],str) or not isinstance(selection['block'],str)
                or selection['quote_id'] in claims):
            raise ScriptureAttention('selection_shape','Invalid or duplicated quote selection')
        claims[selection['quote_id']] = selection
    for quote in evidence['quotes']:
        selection = claims.get(quote['id'])
        if selection is None or selection['block'] != quote['block']:
            raise ScriptureAttention('selection_location','Quotation must remain in its original structural block')
        start,end = selection['start'],selection['end']
        block = blocks.get(selection['block'],'')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(block):
            raise ScriptureAttention('selection_location','Invalid candidate character offsets')
        if not associated and quote.get('delimited') and not any(a == start and b == end for a,b,_ in _output_quote_spans(block,selection['block'])):
            raise ScriptureAttention('quote_scope_changed','Selection must cover the complete delimited output quotation')
        expected = _selection_text(quote,selection,allow_outer_whitespace=expanded)
        if associated and (start, end) != scope.proofs[quote['id']][:2]:
            raise ScriptureAttention('quote_scope_changed', 'Selection must cover its complete original quotation scope')
        if associated and block[start:end] != expected:
            raise ScriptureAttention('quote_words_changed','Candidate Scripture words must exactly equal the retrieved target selections')
        if scope is not None and (associated or not quote.get('delimited')):
            if _prove_scope(scope,quote,expected) != (start,end):
                raise ScriptureAttention('quote_scope_changed','Selection must cover its complete original structural quotation')
        if block[start:end] != expected:
            raise ScriptureAttention('quote_words_changed','Candidate Scripture words must exactly equal the retrieved target selections')
        for left,right in intervals.setdefault(selection['block'],[]):
            if start < right and left < end:
                raise ScriptureAttention('selection_overlap','Different quotations cannot claim the same output')
        intervals[selection['block']].append((start,end))
    return True


def repair_complete_selections(evidence, candidate, selections, *, normalization_version='1',
                               source=None, max_selection_bytes=MAX_SELECTION_AUDIT_BYTES):
    """Prove metadata for complete quotes without editing or guessing any words.

    The provider must still identify every original quote, structural block and
    ordered cited verse. Only numeric offsets may be repaired, and only when the
    full frozen text occurs once in the entire block and fills one delimited
    quotation. Version 2 also permits the frozen structural shapes proved by
    StructuralScope, with one-to-one article occurrence accounting against
    frozen scopes. Partial/ellipsis selections are deliberately ineligible.
    This is not an independent review.
    """
    expanded = normalization_version == '2'
    if expanded:
        _bounded_selection_input(selections,max_selection_bytes)
    if not isinstance(selections,list) or len(selections) != len(evidence['quotes']):
        raise ScriptureAttention('missing_selections','Every identified Scripture quotation needs one exact output claim')
    claims = {}
    for selection in selections:
        if (not isinstance(selection,dict) or set(selection) != {'quote_id','block','start','end','fragments'}
                or not isinstance(selection['quote_id'],str) or not isinstance(selection['block'],str)
                or selection['quote_id'] in claims):
            raise ScriptureAttention('selection_shape','Invalid or duplicated quote selection')
        claims[selection['quote_id']] = selection
    blocks,scope = _selection_blocks(evidence,candidate,source,expanded)
    repaired = []
    for quote in evidence['quotes']:
        selection = claims.get(quote['id'])
        if selection is None or selection['block'] != quote['block']:
            raise ScriptureAttention('selection_location','Quotation must remain in its original structural block')
        alignment = quote['alignment']
        if (alignment['kind'] != 'complete' or len(alignment['fragments']) != 1
                or alignment['ellipsis'] or alignment['leading_ellipsis'] or alignment['trailing_ellipsis']
                or (not expanded and not quote.get('delimited'))):
            detail = ('Only complete independently scoped quotations permit offset repair' if expanded
                      else 'Only complete delimited quotations permit offset repair')
            raise ScriptureAttention('selection_repair_scope',detail)
        block = blocks.get(quote['block'],'')
        # Version 1 retains its original byte-count ceiling. Version 2 bounds
        # serialized input/parser work and proves the true offsets independently.
        start,end = selection['start'],selection['end']
        if (type(start) is not int or type(end) is not int
                or (not expanded and not 0 <= start < end <= len(block.encode('utf-8')))):
            raise ScriptureAttention('selection_location','Invalid bounded candidate character offsets')
        fragments = selection['fragments']
        verses = quote['target_verses']
        if (not isinstance(fragments,list) or len(fragments) != 1 or not isinstance(fragments[0],list)
                or len(fragments[0]) != len(verses)):
            raise ScriptureAttention('selection_shape','Complete quotation repair requires every original verse exactly once')
        for part,verse in zip(fragments[0],verses):
            if (not isinstance(part,dict) or set(part) != {'verse','start','end'}
                    or any(type(part[key]) is not int for key in part)):
                raise ScriptureAttention('selection_shape','Expected exact integer verse/text offsets')
            if part['verse'] != verse['verse']:
                raise ScriptureAttention('selection_repair_identity','Quotation repair cannot change or reorder selected verses')
            if (not expanded and (part['start'] != 0
                    or not 0 < part['end'] <= len(verse['text'].encode('utf-8')))):
                raise ScriptureAttention('selection_outside_evidence','Complete quotation repair requires bounded whole-verse claims')
        replacement = {**selection,'fragments':[[{'verse':v['verse'],'start':0,'end':len(v['text'])} for v in verses]]}
        expected = _selection_text(quote,replacement)
        position = block.find(expected)
        if position < 0:
            raise ScriptureAttention('quote_words_changed','Complete frozen quotation is absent; metadata repair cannot change words')
        if block.find(expected,position+1) >= 0:
            raise ScriptureAttention('selection_repair_ambiguous','Complete frozen quotation has multiple output locations')
        if scope is not None and not quote.get('delimited'):
            position,proved_end = _prove_scope(scope,quote,expected)
            if proved_end != position+len(expected):
                raise ScriptureAttention('quote_scope_changed','Structural proof disagrees with exact target text')
        replacement.update(start=position,end=position+len(expected))
        repaired.append(replacement)
    # Reuse all strict word, delimiter, identity, location and overlap gates.
    check_selections(evidence,candidate,repaired,require_exact_citations=True,
                     normalization_version=normalization_version,source=source,
                     max_selection_bytes=max_selection_bytes)
    return repaired


def normalize_scripture_candidate(state, task, result):
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    contract = frozen_policy(campaign)
    if not contract:
        return result
    evidence = load_evidence(state,task)
    if not isinstance(result,dict) or set(result) != {'html','title','subtitle','section','scripture_selections'}:
        raise ScriptureAttention('selection_shape','Versioned translation response requires Scripture selections')
    candidate = {key:result[key] for key in ('html','title','subtitle','section')}
    selections = result['scripture_selections']
    version = contract.get('selection_normalization_version')
    options = {'normalization_version':version,'source':state.source(task),
               'max_selection_bytes':contract['max_selection_audit_bytes']}
    normalization = None
    try:
        check_selections(evidence,candidate,selections,**options)
    except ScriptureAttention as exc:
        if version not in SELECTION_NORMALIZATION_VERSIONS:
            raise
        selections = repair_complete_selections(evidence,candidate,selections,**options)
        normalization = {'version':version,
                         'raw_result_sha256':json_hash(result),
                         'original_selections':copy.deepcopy(result['scripture_selections']),
                         'original_failure':exc.reason,
                         'independent_review_required':True}
    audit = {'version':VERSION,'evidence_sha256':task['scripture_evidence_sha256'],
             'candidate_sha256':json_hash(candidate),'selections':selections}
    if version in SELECTION_NORMALIZATION_VERSIONS:
        # Keep this outside the optional correction record so removing that
        # record cannot relabel repaired claims as the original input. Adoption
        # may assemble this input from a saved candidate and validated claims.
        audit['input_result_sha256'] = json_hash(result)
    if normalization is not None:
        audit['normalization'] = normalization
    if len(canonical(audit)) > contract['max_selection_audit_bytes']:
        raise ScriptureAttention('selection_size_limit','Complete selection audit exceeds its frozen bound')
    state.write(f'state/tasks/{task["id"]}/scripture-selections.json',audit)
    return candidate


def validate_scripture_candidate(state, task, candidate):
    campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
    contract = frozen_policy(campaign)
    if not contract:
        return
    evidence = load_evidence(state,task)
    selections = state.read(f'state/tasks/{task["id"]}/scripture-selections.json')
    if (not selections or selections.get('candidate_sha256') != json_hash(candidate)
            or selections.get('evidence_sha256') != task['scripture_evidence_sha256']):
        raise ScriptureAttention('selection_changed','Candidate and Scripture selection audit disagree')
    if len(canonical(selections)) > contract['max_selection_audit_bytes']:
        raise ScriptureAttention('selection_size_limit','Complete selection audit exceeds its frozen bound')
    normalization = selections.get('normalization')
    version = contract.get('selection_normalization_version')
    options = {'normalization_version':version,'source':state.source(task),
               'max_selection_bytes':contract['max_selection_audit_bytes']}
    if version in SELECTION_NORMALIZATION_VERSIONS:
        original = (normalization.get('original_selections') if isinstance(normalization,dict)
                    else selections.get('selections'))
        if selections.get('input_result_sha256') != json_hash({**candidate,'scripture_selections':original}):
            raise ScriptureAttention('selection_changed','Original normalization input provenance is missing/changed')
        generation = 'correct' if task.get('stage') in ('correct','review2') else 'translate'
        archived = state.read(f'state/tasks/{task["id"]}/results/{generation}.json',{})
        if archived and json_hash(archived.get('result')) != selections['input_result_sha256']:
            raise ScriptureAttention('selection_changed','Selection audit disagrees with the archived provider result')
    if normalization is not None:
        if (not isinstance(normalization,dict)
                or set(normalization) != {'version','raw_result_sha256','original_selections','original_failure','independent_review_required'}
                or version not in SELECTION_NORMALIZATION_VERSIONS
                or normalization.get('version') != version
                or normalization.get('independent_review_required') is not True
                or normalization.get('raw_result_sha256') != json_hash({**candidate,'scripture_selections':normalization.get('original_selections')})):
            raise ScriptureAttention('selection_changed','Normalized selection provenance is missing/changed')
        original = normalization['original_selections']
        try:
            check_selections(evidence,candidate,original,**options)
        except ScriptureAttention as exc:
            if normalization['original_failure'] != exc.reason:
                raise ScriptureAttention('selection_changed','Normalized selection failure provenance changed') from exc
        else:
            raise ScriptureAttention('selection_changed','Selection normalization requires a recorded original failure')
        if repair_complete_selections(evidence,candidate,original,**options) != selections['selections']:
            raise ScriptureAttention('selection_changed','Normalized selections disagree with their deterministic proof')
    check_selections(evidence,candidate,selections['selections'],**options)


def adopt_scripture_selection_audit(state, task, previous_task=None):
    """Reuse only validated prior selections, or prove an empty detected set.

    Return False when a candidate needs repair to produce actual selections.
    This never invents selected spans and never changes an old audit record.
    """
    evidence = load_evidence(state,task)
    candidate = state.candidate(task)
    if not isinstance(candidate,dict):
        return False
    if not evidence['quotes']:
        selections = []
    else:
        if previous_task is None:
            return False
        try:
            prior = load_evidence(state,previous_task)
            validate_scripture_candidate(state,previous_task,candidate)
        except (ContractError,KeyError,TypeError):
            return False
        # Retrieval timestamps/cache envelopes may change. The complete source,
        # quote alignment, selected verse words and edition identity may not.
        keys = ('version','source_association_version','source_sha256','article_id','language_tag','edition',
                'edition_map_sha256','quotes','references')
        if any(prior.get(key) != evidence.get(key) for key in keys):
            return False
        audit = state.read(f'state/tasks/{previous_task["id"]}/scripture-selections.json')
        campaign = state.read(f'state/campaigns/{task["campaign"]}.json')
        if (audit.get('normalization') and (frozen_policy(campaign) or {}).get('selection_normalization_version')
                not in SELECTION_NORMALIZATION_VERSIONS):
            return False
        # Keep the raw provider claims visible to a new independent reviewer
        # when the adopted audit required deterministic offset normalization.
        selections = copy.deepcopy(audit.get('normalization',{}).get('original_selections',audit['selections']))
    normalize_scripture_candidate(state,task,{**candidate,'scripture_selections':selections})
    return True
