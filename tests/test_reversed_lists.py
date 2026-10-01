"""Offline preservation and safety checks for HTML reversed ordered lists."""
import unittest
import tempfile
from pathlib import Path
from berean_translation.common import ContractError
from berean_translation.html import Fragment, validate_translation

ARTICLE_ID = '11111111-1111-4111-8111-111111111111'


def article(body):
    return f'<article data-article-id="{ARTICLE_ID}">{body}</article>'


class ReversedListTests(unittest.TestCase):
    def validate(self, source_body, translated_body):
        source = {'article':{'id':ARTICLE_ID,'title':'A countdown','subtitle':None,'section':''},
                  'html':article(source_body)}
        candidate = {'html':article(translated_body),'title':'A countdown','subtitle':None,'section':''}
        return validate_translation(source,candidate)

    def test_conforming_boolean_forms_preserve_exact_attribute_values(self):
        for attribute,value in (('reversed',None), ('reversed=""',''),
                                ('reversed="reversed"','reversed'), ('reversed="ReVeRsEd"','ReVeRsEd')):
            with self.subTest(attribute=attribute):
                result = self.validate(f'<ol {attribute} start="3"><li>Three</li><li>Two</li></ol>',
                                       f'<ol {attribute} start="3"><li>Drie</li><li>Twee</li></ol>')
                signature = next(item for item in result.signature if item[:2] == ('start','ol'))
                self.assertIn(('reversed',value),signature[2])

    def test_reversed_list_direction_start_order_and_nesting_cannot_change(self):
        source = '<ol reversed start="3"><li>Three<ol><li>Detail</li></ol></li><li>Two</li></ol>'
        for translated in (source.replace(' reversed',''),
                           source.replace('start="3"','start="1"'),
                           source.replace(' reversed',' reversed=""'),
                           source.replace('<ol><li>Detail</li></ol>','<ul><li>Detail</li></ul>'),
                           source.replace('<li>Two</li>','<li>Two</li><li>One</li>')):
            with self.subTest(translated=translated), self.assertRaisesRegex(ContractError,'HTML structure'):
                self.validate(source,translated)

    def test_reversed_cannot_be_added_to_an_ascending_list(self):
        with self.assertRaisesRegex(ContractError,'HTML structure'):
            self.validate('<ol><li>First</li></ol>','<ol reversed><li>Eerste</li></ol>')

    def test_reversed_is_forbidden_on_other_elements(self):
        for tag in ('ul','li','p','div','table','article','img'):
            with self.subTest(tag=tag), self.assertRaises(ContractError):
                if tag == 'article':
                    Fragment(f'<article data-article-id="{ARTICLE_ID}" reversed><p>Text</p></article>',ARTICLE_ID)
                else:
                    Fragment(article(f'<{tag} reversed>Text</{tag}>'),ARTICLE_ID)

    def test_nonconforming_values_and_duplicates_remain_rejected(self):
        for attrs in ('reversed="false"','reversed="true"','reversed="1"','reversed=" reversed "',
                      'reversed reversed','reversed="" reversed="reversed"'):
            with self.subTest(attrs=attrs), self.assertRaises(ContractError):
                Fragment(article(f'<ol {attrs}><li>Text</li></ol>'),ARTICLE_ID)

    def test_other_boolean_and_unsafe_attributes_remain_rejected(self):
        for attrs in ('disabled','hidden','start','class','data-anything','onclick',
                      'onclick="alert(1)"','style="color:red"','srcdoc="text"'):
            with self.subTest(attrs=attrs), self.assertRaises(ContractError):
                Fragment(article(f'<ol reversed {attrs}><li>Text</li></ol>'),ARTICLE_ID)

    def test_source_scan_keeps_reversed_and_unaffected_articles_without_api_work(self):
        from support import A, B, setup
        with tempfile.TemporaryDirectory() as directory:
            config,state,upstream,provider,git,engine = setup(Path(directory))
            path = f'content/articles/{A}.html'
            countdown = '<ol start="5" reversed><li>Five.</li><li>Four.</li></ol>'
            upstream.contents[path] = upstream.contents[path].replace('</article>',countdown+'</article>')
            upstream.rebuild()
            result = engine.discover()
            self.assertEqual(set(result['articles']),{A,B})
            self.assertIn(countdown,upstream.client.snapshot(A)['html'])
            self.assertEqual(upstream.client.snapshot(B)['html'],upstream.contents[f'content/articles/{B}.html'])
            self.assertEqual(provider.create_calls,0)
            self.assertEqual(state.tasks(),[])


if __name__ == '__main__':
    unittest.main()
