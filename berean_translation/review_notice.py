"""Presentation never grants editorial authority; recorded human commits do."""
from .common import ContractError
from .html import Fragment


class EditorialFragment(Fragment):
    def handle_starttag(self, tag, attrs):
        if len(dict(attrs)) != len(attrs):
            raise ContractError('Duplicate HTML attribute')
        if tag == 'img' and 'alt' not in dict(attrs):
            attrs = [*attrs, ('alt', '')]  # Metadata default only; never rewrite the editor's bytes.
        # The presentation notice uses the safe accessibility role attribute.
        super().handle_starttag(tag, [(key, value) for key, value in attrs if key not in ('role', 'hreflang')])


def validate_human_content(candidate, tail, article_id):
    if (set(candidate) != {'html', 'title', 'subtitle', 'section'}
            or any(candidate[key] is not None and not isinstance(candidate[key], str)
                   for key in ('title', 'subtitle', 'section'))):
        raise ContractError('Human publication metadata must contain text or null values')
    parsed = EditorialFragment(candidate['html'], article_id)
    if tail:
        EditorialFragment(f'<article data-article-id="{article_id}">{tail}</article>', article_id)
    return parsed


def validate_recorded_notice(publication: dict, tail: str) -> None:
    if publication['human_reviewed'] and not publication.get('human_review'):
        raise ContractError('Human-reviewed publication is missing human commit attribution')
    if publication['human_reviewed']:
        if publication.get('human_review_notice_html', '') != tail:
            raise ContractError('Human-authored presentation has not been synchronized')
    elif tail != publication['notice_html']:
        raise ContractError('AI publication notice changed without human attribution')
