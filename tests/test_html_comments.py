"""Preserve source audit comments without treating them as text or instructions."""
from __future__ import annotations
import unittest
from berean_translation.common import ContractError
from berean_translation.html import Fragment, reference_numbers, split_article, validate_translation

ARTICLE_ID = '11111111-1111-4111-8111-111111111111'
COMMENT = '<!-- Source layout note: the repeated pull quote is not duplicated. John 9:9 -->'


class SourceCommentTests(unittest.TestCase):
    def setUp(self):
        self.html = f'<article data-article-id="{ARTICLE_ID}"><p>Faith. John 3:16.</p>{COMMENT}<p>Conclusion.</p></article>'
        self.source = {'html': self.html, 'article': {'id': ARTICLE_ID, 'title': 'Faith', 'subtitle': None, 'section': None}}
        self.candidate = {'html': self.html, 'title': 'Faith', 'subtitle': None, 'section': None}

    def test_source_comment_is_preserved_but_is_not_article_text(self):
        parsed = validate_translation(self.source, self.candidate)
        self.assertNotIn('Source layout note', parsed.text)
        self.assertEqual(reference_numbers(parsed.text), reference_numbers('John 3:16'))
        self.assertIn(('comment', COMMENT[4:-3]), parsed.signature)

    def test_translating_removing_moving_or_inserting_comments_is_rejected(self):
        candidates = [self.html.replace('Source layout note', 'Changed note'),
                      self.html.replace(COMMENT, ''),
                      self.html.replace(COMMENT, '').replace('</article>', COMMENT + '</article>'),
                      self.html.replace('</article>', '<!-- Added note --></article>')]
        for html in candidates:
            with self.subTest(html=html), self.assertRaises(ContractError):
                validate_translation(self.source, {**self.candidate, 'html': html})

    def test_comments_outside_the_article_are_rejected(self):
        for html in (COMMENT + self.html, self.html + COMMENT):
            with self.assertRaises(ContractError):
                Fragment(html, ARTICLE_ID)

    def test_comment_lookalike_closing_tag_does_not_split_article(self):
        body = self.html.replace(COMMENT, '<!-- Literal </article> is inert metadata. -->')
        notice = '<aside data-translation-notice="ai">Translation notice</aside>'
        self.assertEqual(split_article(body + '\n' + notice), (body, notice))
        Fragment(body, ARTICLE_ID)

    def test_attribute_lookalike_closing_tag_does_not_split_article(self):
        body = self.html.replace('<p>', '<p title="literal </article>">', 1)
        self.assertEqual(split_article(body), (body, ''))

    def test_malformed_comment_cannot_hide_markup(self):
        for comment in ('<!-- invalid -- comment -->', '<!-- invalid\x00comment -->'):
            with self.assertRaises(ContractError):
                Fragment(self.html.replace(COMMENT, comment), ARTICLE_ID)

    def test_missing_real_article_end_is_rejected(self):
        html = self.html.replace(COMMENT, '<!-- </article> -->').removesuffix('</article>')
        with self.assertRaises(ContractError):
            split_article(html)

    def test_nested_article_is_still_forbidden(self):
        with self.assertRaises(ContractError):
            Fragment(self.html.replace(COMMENT, '<article></article>'), ARTICLE_ID)


if __name__ == '__main__':
    unittest.main()
