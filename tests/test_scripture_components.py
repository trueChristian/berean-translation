"""Rebuilt offline component controls; these are not provider-evidence fixtures.

All short verses and component envelopes below are deliberately synthetic test
inputs. JOEL_RECORDED_* preserve the two previously recorded verse strings only:
they have no fetched-at timestamp, cache envelope, chapter proof, or provider
response provenance. A structural result never grants runtime admission.

These tests were reconstructed after the earlier executor's files were lost.
They do not represent or recover the historical 855-test run.
"""
import copy
from functools import partial
import unittest
from unittest.mock import patch

from berean_translation.common import ContractError, canonical as canonical_json, json_hash
from berean_translation import scripture_components as components

# Every successful control opts in explicitly to the rebuilt versioned API.
decode_component_reference = partial(components.decode_component_reference, version='1')
validate_components = partial(components.validate_components, version='1')
validate_component_pair = partial(components.validate_component_pair, version='1')
from berean_translation.scripture_evidence import ScriptureAttention, _reference


JOEL_RECORDED_KJV = (
    'And I will restore to you the years that the locust hath eaten, the '
    'cankerworm, and the caterpiller, and the palmerworm, my great army which '
    'I sent among you.'
)
JOEL_RECORDED_PORTUGUESE = (
    'E restituir-vos-hei os annos que comeu o gafanhoto, a locusta, e o pulgão '
    'e a aruga, o meu grande exercito que enviei contra vós.'
)


def canonical(verse, start=0, end=None):
    end = len(verse) if end is None else end
    return {'kind': 'canonical', 'start': start, 'end': end,
            'text': verse[start:end]}


def omit(verse, start, end):
    return {'kind': 'omit', 'start': start, 'end': end,
            'text': verse[start:end], 'reason': 'excerpt_boundary'}


def insert(at, authored_text='[word]', identity='a1'):
    return {'kind': 'insert', 'id': identity, 'at': at,
            'authored_text': authored_text}


def replace(verse, start, end, authored_text='[word]', identity='a1'):
    return {'kind': 'replace', 'id': identity, 'start': start, 'end': end,
            'text': verse[start:end], 'authored_text': authored_text}


def punctuation(at, authored_text='!', identity='a1'):
    return {'kind': 'punctuation', 'id': identity, 'at': at,
            'authored_text': authored_text}


def side(verse, quote=None, operations=None):
    return {'verse': verse, 'quote': verse if quote is None else quote,
            'operations': [canonical(verse)] if operations is None else operations}


def inserted_side(verse='Alpha beta.', authored_text='[word]', identity='a1'):
    return side(verse, authored_text + verse,
                [insert(0, authored_text, identity), canonical(verse)])


def replaced_side(verse='Alpha beta.', authored_text='[word]', identity='a1'):
    end = verse.index(' ')
    return side(verse, authored_text + verse[end:],
                [replace(verse, 0, end, authored_text, identity),
                 canonical(verse, end)])


class ComponentTestCase(unittest.TestCase):
    def validate(self, value, **kwargs):
        return validate_components(value['verse'], value['quote'],
                                   value['operations'], **kwargs)

    def assert_invalid(self, value, **kwargs):
        with self.assertRaises(ContractError):
            self.validate(value, **kwargs)

    def assert_pair_invalid(self, source, target, **kwargs):
        with self.assertRaises(ContractError):
            validate_component_pair(source, target,
                                    printed_reference='John 1:1', **kwargs)

    def assert_review_only(self, result):
        self.assertIs(result['independent_review_required'], True)
        self.assertIs(result['runtime_admission_supported'], False)


class ComponentReferenceTests(ComponentTestCase):
    def test_standard_reference_decodes(self):
        result = decode_component_reference('John 3:16')
        self.assertEqual(result, {
            'printed_reference': 'John 3:16', 'lookup_reference': 'John 3:16',
            'book': 43, 'chapter': 3, 'verses': [16], 'partial_marker': None,
            'component_version': '1',
        })

    def test_rom_alias_and_printed_suffix_have_separate_lookup_identity(self):
        for printed in ('Rom. 8:28', 'Rom 8:28', 'Romans 8:28'):
            with self.subTest(printed=printed):
                result = decode_component_reference(printed)
                self.assertEqual(result['printed_reference'], printed)
                self.assertEqual(result['lookup_reference'], 'Romans 8:28')
                self.assertEqual((result['book'], result['chapter'], result['verses']),
                                 (45, 8, [28]))
        result = decode_component_reference('Joel 2:25a')
        self.assertEqual(result['printed_reference'], 'Joel 2:25a')
        self.assertEqual(result['lookup_reference'], 'Joel 2:25')
        self.assertEqual(result['partial_marker'], 'a')
        self.assertEqual((result['book'], result['chapter'], result['verses']),
                         (29, 2, [25]))

    def test_suffix_does_not_claim_a_verse_extent(self):
        for suffix in ('a', 'b', 'z'):
            result = decode_component_reference('Joel 2:25' + suffix)
            with self.subTest(suffix=suffix):
                self.assertEqual(result['lookup_reference'], 'Joel 2:25')
                self.assertEqual(result['partial_marker'], suffix)
                self.assertNotIn('start', result)
                self.assertNotIn('end', result)

    def test_ranges_and_lists_decode_without_enabling_multiverse_pairs(self):
        for printed, verses in (('John 3:16-18', [16, 17, 18]),
                                ('John 3:16,18', [16, 18])):
            with self.subTest(printed=printed):
                self.assertEqual(decode_component_reference(printed)['verses'], verses)
                with self.assertRaisesRegex(ScriptureAttention, 'component_multi_verse'):
                    validate_component_pair(side('Alpha.'), side('Uno.'),
                                            printed_reference=printed)

    def test_old_reference_decoder_still_rejects_joel_suffix(self):
        with self.assertRaises(ScriptureAttention):
            _reference('Joel 2:25a')
        self.assertEqual(_reference('Joel 2:25'), (29, 2, [25]))

    def test_malformed_references_fail_closed(self):
        malformed = (
            '', ' ', 'John', 'John 3', 'John 3:', 'John :16',
            'John 0:16', 'John -3:16', 'John 3:0', 'John 3:-1',
            'John 03:16', 'John 3:016', 'John 3:16junk', 'John 3:16ab',
            'John 3:16 a', 'John 3:16,', 'John 3:16,,17',
            'John 3:16-15', 'John 3:16-17-18', 'John 3:16;17',
            'John 3:16/17', 'John 3:16:17', 'John 3:16.5',
            'John 3:16 and Romans 8:28', 'prefix John 3:16',
            'John 3:16 suffix', 'Unknown 1:1', '4 John 1:1',
            'John 3:16,16', 'John 3:17,16', 'John 3:16-18,17',
            'John 3:201', 'John 3:16a-17', 'John 3:16-17a',
            'John 3:16\x00', 'John 3:16\nRomans 8:28',
            'John 3:16\u200b', 'John\u200d 3:16',
        )
        for printed in malformed:
            with self.subTest(printed=printed), self.assertRaises(ContractError):
                decode_component_reference(printed)

    def test_reference_types_are_strict(self):
        for printed in (None, 1, True, [], {}, b'John 3:16'):
            with self.subTest(printed=printed), self.assertRaises(ContractError):
                decode_component_reference(printed)

    def test_version_opt_in_cannot_be_omitted(self):
        with self.assertRaises(TypeError):
            components.decode_component_reference('John 1:1')
        with self.assertRaises(TypeError):
            components.validate_components('Alpha.', 'Alpha.', [canonical('Alpha.')])
        with self.assertRaises(TypeError):
            components.validate_component_pair(side('Alpha.'), side('Uno.'),
                                               printed_reference='John 1:1')

    def test_unknown_versions_fail_for_every_entry_point(self):
        value = side('Alpha beta.')
        for version in ('', '0', '2', 1, True, None, [], {}):
            with self.subTest(version=version):
                with self.assertRaises(ContractError):
                    decode_component_reference('John 1:1', version=version)
                self.assert_invalid(value, version=version)
                self.assert_pair_invalid(value, value, version=version)


class ComponentPartitionTests(ComponentTestCase):
    def test_whole_synthetic_verse(self):
        value = side('Alpha beta gamma.')
        result = self.validate(value)
        self.assertEqual(result['component_version'], '1')
        self.assertEqual(result['verse_sha256'], json_hash(value['verse']))
        self.assertEqual(result['quote_sha256'], json_hash(value['quote']))
        self.assertEqual(result['components_sha256'], json_hash(value['operations']))
        self.assertEqual(result['shape'], [{'kind': 'canonical', 'has_words': True}])
        self.assertIs(result['independent_review_required'], True)

    def test_canonical_punctuation_tail_can_be_its_own_segment(self):
        verse = 'Alpha beta.'
        value = side(verse, operations=[canonical(verse, 0, len(verse)-1),
                                        canonical(verse, len(verse)-1)])
        self.assertTrue(self.validate(value))

    def test_leading_and_trailing_boundary_omissions(self):
        verse = 'Alpha beta gamma.'
        value = side(verse, 'beta', [omit(verse, 0, 6), canonical(verse, 6, 10),
                                    omit(verse, 10, len(verse))])
        self.assertTrue(self.validate(value))

    def test_an_interior_omission_is_not_an_excerpt_boundary(self):
        verse = 'Alpha beta gamma.'
        self.assert_invalid(side(verse, 'Alpha gamma.', [canonical(verse, 0, 6),
            omit(verse, 6, 11), canonical(verse, 11)]))

    def test_boundary_omissions_cannot_cut_inside_words(self):
        verse = 'Alpha beta.'
        self.assert_invalid(side(verse, verse[2:], [omit(verse, 0, 2),
                                                  canonical(verse, 2)]))
        self.assert_invalid(side(verse, verse[:8], [canonical(verse, 0, 8),
                                                   omit(verse, 8, len(verse))]))

    def test_omission_must_declare_exact_boundary_reason(self):
        verse = 'Alpha beta.'
        for reason in ('', None, 'ellipsis', 'editorial', 'excerpt', True):
            value = side(verse, 'beta.', [omit(verse, 0, 6), canonical(verse, 6)])
            value['operations'][0]['reason'] = reason
            with self.subTest(reason=reason):
                self.assert_invalid(value)

    def test_omission_cannot_remove_the_entire_verse(self):
        verse = 'Alpha beta.'
        self.assert_invalid(side(verse, '', [omit(verse, 0, len(verse))]))

    def test_offsets_are_codepoints_not_utf8_bytes(self):
        verse = 'Élan 🌿 café.'
        prefix = 'Élan 🌿 '
        value = side(verse, 'café.', [omit(verse, 0, len(prefix)),
                                   canonical(verse, len(prefix))])
        self.assertTrue(self.validate(value))
        value['operations'][1]['start'] = len(prefix.encode('utf-8'))
        self.assert_invalid(value)

    def test_offsets_are_codepoints_not_utf16_units(self):
        verse = '🌿 Alpha.'
        value = side(verse, 'Alpha.', [omit(verse, 0, 2), canonical(verse, 2)])
        self.assertTrue(self.validate(value))
        value['operations'][0]['end'] = 3
        self.assert_invalid(value)

    def test_quote_reconstruction_is_exact_without_normalization(self):
        value = side('Alpha café.')
        for quote in ('alpha café.', 'Alpha cafe.', 'Alpha cafe\u0301.',
                      'Alpha  café.', 'Alpha café', ' Alpha café.',
                      'Alpha café. ', 'Alpha café. extra'):
            with self.subTest(quote=quote):
                self.assert_invalid({**value, 'quote': quote})

    def test_declared_consumed_text_must_match_exact_slice(self):
        verse = 'Alpha beta.'
        values = (side(verse),
                  side(verse, 'beta.', [omit(verse, 0, 6), canonical(verse, 6)]),
                  replaced_side(verse))
        for value in values:
            value['operations'][0]['text'] = value['operations'][0]['text'].upper()
            with self.subTest(kind=value['operations'][0]['kind']):
                self.assert_invalid(value)

    def test_nonstring_consumed_text_fails(self):
        for bad in (None, True, 1, [], {}):
            value = side('Alpha beta.')
            value['operations'][0]['text'] = bad
            with self.subTest(bad=bad):
                self.assert_invalid(value)

    def test_gap_overlap_reverse_order_and_uncovered_tail_fail(self):
        verse = 'Alpha beta.'
        cases = (
            [canonical(verse, 1)],
            [canonical(verse, 0, len(verse)-1)],
            [canonical(verse, 0, 5), canonical(verse, 6)],
            [canonical(verse, 0, 7), canonical(verse, 6)],
            [canonical(verse, 6), canonical(verse, 0, 6)],
            [canonical(verse), canonical(verse)],
        )
        for operations in cases:
            with self.subTest(operations=operations):
                self.assert_invalid(side(verse, operations=operations))

    def test_zero_width_and_reversed_consuming_spans_fail(self):
        verse = 'Alpha beta.'
        for start, end in ((0, 0), (5, 5), (6, 2), (-1, 5), (0, 999)):
            operation = {'kind': 'canonical', 'start': start, 'end': end,
                         'text': verse[start:end]}
            with self.subTest(start=start, end=end):
                self.assert_invalid(side(verse, operations=[operation]))

    def test_integer_offsets_do_not_accept_booleans_or_coercion(self):
        for field in ('start', 'end'):
            for bad in (True, False, 0.0, '0', None, [], {}):
                value = side('Alpha beta.')
                value['operations'][0][field] = bad
                with self.subTest(field=field, bad=bad):
                    self.assert_invalid(value)

    def test_verse_quote_operations_types_are_strict(self):
        for field in ('verse', 'quote'):
            for bad in (None, True, 7, [], {}, b'Alpha beta.'):
                with self.subTest(field=field, bad=bad):
                    self.assert_invalid({**side('Alpha beta.'), field: bad})
        for operations in (None, (), {}, 'canonical', True, [None], [7], [[]]):
            with self.subTest(operations=operations):
                self.assert_invalid(side('Alpha beta.', operations=operations)
                                    if operations is not None else
                                    {**side('Alpha beta.'), 'operations': None})

    def test_empty_inputs_fail(self):
        for value in (side(''), side('Alpha', ''), side('Alpha', operations=[])):
            with self.subTest(value=value):
                self.assert_invalid(value)


class ComponentAuthoredTests(ComponentTestCase):
    def test_synthetic_subword_insert_is_structural_input(self):
        value = inserted_side('man', '[wo]')
        self.assertTrue(self.validate(value))

    def test_insert_at_internal_cursor(self):
        verse = 'Alpha beta.'
        self.assertTrue(self.validate(side(verse, 'Alpha [new]beta.', [
            canonical(verse, 0, 6), insert(6, '[new]'), canonical(verse, 6)])))

    def test_insert_at_end_cursor(self):
        verse = 'Alpha.'
        self.assertTrue(self.validate(side(verse, verse + '[word]', [
            canonical(verse), insert(len(verse))])))

    def test_insert_at_mismatched_cursor_fails(self):
        for at in (-1, 1, 5, 100, True, 0.0, '0', None):
            value = inserted_side()
            value['operations'][0]['at'] = at
            with self.subTest(at=at):
                self.assert_invalid(value)

    def test_insert_after_consuming_same_location_fails(self):
        verse = 'Alpha beta.'
        self.assert_invalid(side(verse, '[word]' + verse,
                                 [canonical(verse), insert(0)]))

    def test_whole_word_replacement(self):
        self.assertTrue(self.validate(replaced_side()))

    def test_subword_replacements_fail(self):
        verse = 'woman stands.'
        for start, end in ((0, 2), (2, 5), (1, 4), (0, 4)):
            operations = ([canonical(verse, 0, start)] if start else [])
            operations += [replace(verse, start, end)]
            operations += [canonical(verse, end)]
            quote = verse[:start] + '[word]' + verse[end:]
            with self.subTest(start=start, end=end):
                self.assert_invalid(side(verse, quote, operations))

    def test_unicode_hyphens_keep_replacement_tokens_joined(self):
        for joiner in ('-', '\u058a', '\u05be', '\u1400', '\u1806',
                       '\u2010', '\u2011', '\u2012', '\u2013', '\u2014',
                       '\u2e17', '\u2e1a', '\u2e3a', '\u2e3b', '\ufe63', '\uff0d'):
            verse = 'alpha' + joiner + 'beta.'
            with self.subTest(joiner=repr(joiner)):
                self.assert_invalid(side(verse, '[word]' + verse[5:], [
                    replace(verse, 0, 5), canonical(verse, 5)]))

    def test_entire_hyphenated_or_format_joined_replacements_remain_unsupported(self):
        for joiner in ('-', '\u2010', '\u2011', '\u200d', '\u00ad'):
            token = 'alpha' + joiner + 'beta'
            verse = token + ' stays.'
            with self.subTest(joiner=repr(joiner)):
                self.assert_invalid(side(verse, '[word] stays.', [
                    replace(verse, 0, len(token)), canonical(verse, len(token))]))

    def test_apostrophe_joined_replacements_cannot_split_the_word(self):
        for joiner in ("'", '’'):
            verse = 'alpha' + joiner + 'beta stays.'
            with self.subTest(joiner=joiner):
                self.assert_invalid(side(verse, '[word]' + verse[5:], [
                    replace(verse, 0, 5), canonical(verse, 5)]))

    def test_format_joined_tokens_cannot_be_replaced_in_part(self):
        for joiner in ('\u00ad', '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff'):
            verse = 'alpha' + joiner + 'beta.'
            with self.subTest(joiner=repr(joiner)):
                self.assert_invalid(side(verse, '[word]' + verse[5:], [
                    replace(verse, 0, 5), canonical(verse, 5)]))

    def test_combining_mark_is_part_of_the_replaced_word(self):
        verse = 'cafe\u0301 stays.'
        self.assert_invalid(side(verse, '[word]' + verse[4:], [
            replace(verse, 0, 4), canonical(verse, 4)]))
        self.assertTrue(self.validate(side(verse, '[word]' + verse[5:], [
            replace(verse, 0, 5), canonical(verse, 5)])))

    def test_replacement_must_consume_lexical_material(self):
        for verse, start, end in (('Alpha beta.', 5, 6), ('Alpha.', 5, 6)):
            operations = [canonical(verse, 0, start), replace(verse, start, end)]
            if end < len(verse):
                operations.append(canonical(verse, end))
            with self.subTest(verse=verse, start=start):
                self.assert_invalid(side(verse, verse[:start] + '[word]' + verse[end:],
                                         operations))

    def test_authored_payload_requires_visible_bracketed_material(self):
        for payload in ('', 'word', '[]', '[ ]', '[\t]', '[\n]', '[\x00]',
                        '[\u00ad]', '[\u200b]', '[\u200c]', '[\u200d]',
                        '[\u2060]', '[\ufeff]', '[\u0301]',
                        None, 1, True, [], {}):
            value = inserted_side()
            value['operations'][0]['authored_text'] = payload
            value['quote'] = str(payload) + value['verse']
            with self.subTest(payload=payload):
                self.assert_invalid(value)

    def test_authored_ids_are_nonempty_strings(self):
        for identity in ('', 'a0', 'a01', '1', 'p1', 'a-1', ' a1', 'a1 ',
                         'a1\u200b', None, 1, True, [], {}):
            with self.subTest(identity=identity):
                self.assert_invalid(inserted_side(identity=identity))

    def test_duplicate_authored_ids_fail(self):
        verse = 'Alpha beta.'
        self.assert_invalid(side(verse, '[one][two]' + verse, [
            insert(0, '[one]', 'a1'), insert(0, '[two]', 'a1'), canonical(verse)]))

    def test_visible_payload_cannot_hide_control_or_format_characters(self):
        for hidden in ('\x00', '\t', '\n', '\u00ad', '\u200b', '\u200d',
                       '\u202e', '\u2066', '\ufeff'):
            with self.subTest(hidden=repr(hidden)):
                self.assert_invalid(inserted_side(authored_text='[wo' + hidden + 'rd]'))

    def test_nested_or_unclosed_authored_brackets_fail(self):
        for payload in ('[word', 'word]', '[[word]]', '[one][two]', '[{word}]',
                        '[word] prose', 'prose [word]'):
            with self.subTest(payload=payload):
                self.assert_invalid(inserted_side(authored_text=payload))

    def test_punctuation_can_be_authored_at_exact_cursor(self):
        verse = 'Alpha beta'
        self.assertTrue(self.validate(side(verse, verse + '!', [
            canonical(verse), punctuation(len(verse))])))

    def test_punctuation_cannot_launder_lexical_or_invisible_payloads(self):
        verse = 'Alpha beta'
        for payload in ('word', '1', '[word]', ' ', '\u200b', '\u0301', '',
                        None, 1, True, [], {}):
            with self.subTest(payload=payload):
                self.assert_invalid(side(verse, verse + str(payload), [
                    canonical(verse), punctuation(len(verse), payload)]))


class ComponentPairTests(ComponentTestCase):
    def test_synthetic_bilingual_canonical_control(self):
        result = validate_component_pair(side('Alpha beta.'), side('Uno dos.'),
                                         printed_reference='Rom. 8:28')
        self.assert_review_only(result)
        self.assertEqual(result['reference']['printed_reference'], 'Rom. 8:28')
        self.assertEqual(result['reference']['lookup_reference'], 'Romans 8:28')
        for key in ('source', 'target'):
            self.assertIs(result[key]['independent_review_required'], True)

    def test_subword_insert_pair_still_cannot_admit_runtime(self):
        result = validate_component_pair(inserted_side('man', '[wo]'),
            inserted_side('homem', '[mulher]'), printed_reference='John 1:1')
        self.assert_review_only(result)

    def test_translated_insert_payload_preserves_id_and_kind(self):
        source = inserted_side('Alpha beta.', '[word]', 'a1')
        target = inserted_side('Uno dos.', '[palavra]', 'a1')
        self.assert_review_only(validate_component_pair(source, target,
                                                       printed_reference='John 1:1'))

    def test_translated_replacement_preserves_id_and_kind(self):
        self.assert_review_only(validate_component_pair(replaced_side(),
            replaced_side('Uno dos.', '[palavra]'), printed_reference='John 1:1'))

    def test_authored_id_change_fails(self):
        self.assert_pair_invalid(inserted_side(), inserted_side(identity='a2'))

    def test_authored_kind_change_fails(self):
        self.assert_pair_invalid(inserted_side(), replaced_side())

    def test_added_or_missing_authored_component_fails(self):
        self.assert_pair_invalid(inserted_side(), side('Alpha beta.'))
        self.assert_pair_invalid(side('Alpha beta.'), inserted_side())

    def test_authored_order_change_fails(self):
        verse = 'Alpha beta.'
        source = side(verse, '[one][two]' + verse, [
            insert(0, '[one]', 'a1'), insert(0, '[two]', 'a2'), canonical(verse)])
        target = side(verse, '[two][one]' + verse, [
            insert(0, '[two]', 'a2'), insert(0, '[one]', 'a1'), canonical(verse)])
        self.assert_pair_invalid(source, target)

    def test_punctuation_payload_must_remain_exactly_unchanged(self):
        verse = 'Alpha beta'
        source = side(verse, verse + '!', [canonical(verse), punctuation(len(verse))])
        target = side(verse, verse + '?', [canonical(verse), punctuation(len(verse), '?')])
        self.assert_pair_invalid(source, target)
        self.assertTrue(validate_component_pair(source, copy.deepcopy(source),
                                               printed_reference='John 1:1'))

    def test_lexical_canonical_segment_cannot_be_laundered_to_whitespace(self):
        source = side('Alpha beta', 'Alpha [word]beta', [
            canonical('Alpha beta', 0, 6), insert(6), canonical('Alpha beta', 6)])
        target = side('Uno  ', 'Uno [word] ', [
            canonical('Uno  ', 0, 4), insert(4), canonical('Uno  ', 4)])
        self.assertTrue(self.validate(source))
        self.assertTrue(self.validate(target))
        self.assert_pair_invalid(source, target)

    def test_lexical_canonical_segment_cannot_be_laundered_to_punctuation(self):
        source = side('Alpha beta', 'Alpha [word]beta', [
            canonical('Alpha beta', 0, 6), insert(6), canonical('Alpha beta', 6)])
        target = side('Uno .', 'Uno [word].', [
            canonical('Uno .', 0, 4), insert(4), canonical('Uno .', 4)])
        self.assert_pair_invalid(source, target)

    def test_normal_canonical_punctuation_tail_is_allowed(self):
        source = side('Alpha beta.', 'Alpha [word].', [
            canonical('Alpha beta.', 0, 6), replace('Alpha beta.', 6, 10),
            canonical('Alpha beta.', 10)])
        target = side('Uno dos.', 'Uno [palavra].', [
            canonical('Uno dos.', 0, 4), replace('Uno dos.', 4, 7, '[palavra]'),
            canonical('Uno dos.', 7)])
        self.assertTrue(validate_component_pair(source, target,
                                               printed_reference='John 1:1'))

    def test_pair_sides_have_exact_schema(self):
        valid = side('Alpha beta.')
        for field in ('verse', 'quote', 'operations'):
            changed = copy.deepcopy(valid)
            del changed[field]
            with self.subTest(missing=field):
                self.assert_pair_invalid(changed, valid)
                self.assert_pair_invalid(valid, changed)
        for extra in ('provenance', 'approved', 'runtime_admitted', 'version', 'id'):
            changed = {**valid, extra: True}
            with self.subTest(extra=extra):
                self.assert_pair_invalid(changed, valid)
                self.assert_pair_invalid(valid, changed)
        for changed in (None, [], '', True):
            with self.subTest(changed=changed):
                self.assert_pair_invalid(changed, valid)
                self.assert_pair_invalid(valid, changed)

    def test_pair_does_not_mutate_input_sides(self):
        source, target = inserted_side(), inserted_side('Uno dos.', '[palavra]')
        before = copy.deepcopy((source, target))
        validate_component_pair(source, target, printed_reference='John 1:1')
        self.assertEqual((source, target), before)


class RecordedJoelStringControls(ComponentTestCase):
    def test_recorded_verse_strings_can_be_partitioned_without_provider_claim(self):
        for verse in (JOEL_RECORDED_KJV, JOEL_RECORDED_PORTUGUESE):
            with self.subTest(verse=verse):
                self.assertTrue(self.validate(side(verse)))
        result = validate_component_pair(side(JOEL_RECORDED_KJV),
            side(JOEL_RECORDED_PORTUGUESE), printed_reference='Joel 2:25')
        self.assert_review_only(result)

    def test_portuguese_joel_partial_hyphenated_replacement_remains_held(self):
        verse = JOEL_RECORDED_PORTUGUESE
        start = verse.index('restituir')
        end = start + len('restituir')
        target = side(verse, verse[:start] + '[restaurar]' + verse[end:], [
            canonical(verse, 0, start), replace(verse, start, end, '[restaurar]'),
            canonical(verse, end)])
        with self.assertRaisesRegex(ScriptureAttention, 'morphology_or_subword_replacement'):
            self.validate(target)
        source_verse = JOEL_RECORDED_KJV
        source_start = source_verse.index('I will restore')
        source_end = source_start + len('I will restore')
        source = side(source_verse,
            source_verse[:source_start] + '[restore]' + source_verse[source_end:], [
                canonical(source_verse, 0, source_start),
                replace(source_verse, source_start, source_end, '[restore]'),
                canonical(source_verse, source_end)])
        self.assertTrue(self.validate(source))
        with self.assertRaisesRegex(ScriptureAttention, 'morphology_or_subword_replacement'):
            validate_component_pair(source, target, printed_reference='Joel 2:25a')


class ComponentSchemaAndBoundsTests(ComponentTestCase):
    def test_operation_extras_fail_for_every_kind(self):
        verse = 'Alpha beta.'
        values = (
            side(verse), inserted_side(), replaced_side(),
            side(verse, 'beta.', [omit(verse, 0, 6), canonical(verse, 6)]),
            side(verse, verse + '!', [canonical(verse), punctuation(len(verse))]),
        )
        for value in values:
            for index in range(len(value['operations'])):
                changed = copy.deepcopy(value)
                changed['operations'][index]['unexpected'] = 'no'
                with self.subTest(kind=value['operations'][index]['kind']):
                    self.assert_invalid(changed)

    def test_required_operation_fields_cannot_be_omitted(self):
        verse = 'Alpha beta.'
        values = (side(verse), inserted_side(), replaced_side(),
                  side(verse, 'beta.', [omit(verse, 0, 6), canonical(verse, 6)]),
                  side(verse, verse + '!', [canonical(verse), punctuation(len(verse))]))
        for value in values:
            for index, operation in enumerate(value['operations']):
                for key in operation:
                    changed = copy.deepcopy(value)
                    del changed['operations'][index][key]
                    with self.subTest(kind=operation['kind'], key=key):
                        self.assert_invalid(changed)

    def test_unknown_operation_kinds_fail(self):
        for kind in ('', 'delete', 'rewrite', 'Canonical', None, True, 1, [], {}):
            value = side('Alpha beta.')
            value['operations'][0]['kind'] = kind
            with self.subTest(kind=kind):
                self.assert_invalid(value)

    def test_component_count_has_a_hard_nontruncating_bound(self):
        for count in (components.MAX_COMPONENTS, components.MAX_COMPONENTS + 1):
            verse = 'A' * count
            value = side(verse, operations=[canonical(verse, i, i+1)
                                             for i in range(count)])
            with self.subTest(count=count):
                if count == components.MAX_COMPONENTS:
                    self.assertEqual(len(self.validate(value)['shape']), count)
                else:
                    self.assert_invalid(value)

    def test_total_serialized_bytes_have_a_hard_nontruncating_bound(self):
        limit = components.MAX_COMPONENT_BYTES
        low, high = 1, limit
        while low + 1 < high:
            middle = (low + high) // 2
            if len(canonical_json(side('A' * middle))) <= limit:
                low = middle
            else:
                high = middle
        value = side('A' * low)
        self.assertLessEqual(len(canonical_json(value)), limit)
        self.assertTrue(self.validate(value))
        over = side('A' * high)
        self.assertGreater(len(canonical_json(over)), limit)
        with self.assertRaisesRegex(ScriptureAttention, 'component_size_limit'):
            self.validate(over)

    def test_serialized_byte_bound_counts_multibyte_unicode(self):
        value = side('é' * (components.MAX_COMPONENT_BYTES // 4))
        self.assertLess(len(value['verse']), components.MAX_COMPONENT_BYTES)
        self.assertGreater(len(canonical_json(value)), components.MAX_COMPONENT_BYTES)
        with self.assertRaisesRegex(ScriptureAttention, 'component_size_limit'):
            self.validate(value)

    def test_reference_resource_bound_rejects_long_input(self):
        with self.assertRaises(ContractError):
            decode_component_reference('John ' + '1' * 256 + ':1')

    def test_unserializable_and_invalid_unicode_inputs_fail_closed(self):
        for bad in (float('nan'), float('inf'), set(), object(), '\ud800'):
            value = side('Alpha beta.')
            value['operations'][0]['text'] = bad
            with self.subTest(bad=repr(bad)):
                self.assert_invalid(value)
        recursive = []
        recursive.append(recursive)
        value = side('Alpha beta.')
        value['operations'][0]['text'] = recursive
        self.assert_invalid(value)

    def test_public_primitives_do_not_open_network_connections(self):
        with patch('socket.create_connection', side_effect=AssertionError('offline only')):
            decode_component_reference('Rom. 8:28')
            self.validate(inserted_side())
            validate_component_pair(inserted_side(), inserted_side('Uno dos.', '[palavra]'),
                                    printed_reference='John 1:1')


if __name__ == '__main__':
    unittest.main()
