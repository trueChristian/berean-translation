"""Bounded source refreshes for pairs with an existing AI publication only."""
from __future__ import annotations
from collections import defaultdict
from .common import ContractError, csv_values, json_hash, read_json
from .state import TERMINAL
from .recovery import FIELDS as RECOVERY_FIELDS


def _request_languages(config, request):
    try:
        return set(config.select_languages(request.get('languages', 'all')))
    except (ContractError, TypeError, AttributeError):
        return set()


def _pending_covers(config, source, request, language, article):
    # Exact candidate recovery rejects any existing publication, so it cannot
    # overlap an automatic refresh. Never interpret its absent selectors as all/next.
    if RECOVERY_FIELDS.intersection(request):
        return False
    if request.get('dry_run') is True or request.get('operation') not in ('translate', 'review'):
        return False
    if language not in _request_languages(config, request):
        return False
    exact = request.get('article_ids')
    if exact is not None:
        return isinstance(exact, list) and article['id'] in exact
    selection = request.get('issues', 'next')
    if selection in ('all', 'outstanding', 'next'):
        # A pending "next" request chooses its issue at acceptance. Conservatively
        # let it run first rather than creating overlapping paid reservations.
        return True
    if not isinstance(selection, str):
        return False
    aliases = {value: issue['id'] for issue in source.get('issues', [])
               for value in (issue['id'], issue.get('slug'), issue.get('source_id')) if value}
    try:
        selected = csv_values(selection)
    except ContractError:
        return False
    return article['issue_id'] in {aliases.get(value) for value in selected}


def enqueue_source_refreshes(engine) -> list[str]:
    """Persist and checkpoint immutable requests; never prepare or submit API work.

    Only call once after the initial source discovery in a worker tick. The
    ordinary acceptance path must recheck exact article IDs, current fingerprints,
    publication/human status, and the configured spending policy before creating
    tasks. All historical attempts suppress another automatic run for that same
    article/language/fingerprint, including rollbacks and unsuccessful attempts.
    """
    config, state = engine.config, engine.state
    policy = config.runtime.get('automatic_source_refresh', {})
    if not policy.get('enabled', False):
        return []
    source = state.read('state/source.json', {})
    articles = source.get('articles', {})
    issue_ids = {issue['id'] for issue in source.get('issues', [])}
    tasks = state.tasks()
    attempted = {(task.get('language'), task.get('article_id'), task.get('translation_key')) for task in tasks}
    active = {(task.get('language'), task.get('article_id')) for task in tasks if task.get('status') not in TERMINAL}
    pending = []
    for path in sorted((config.root / 'state/queue').glob('*.json')):
        request = read_json(path)
        if not isinstance(request, dict):
            continue
        keys = request.get('source_translation_keys')
        if request.get('source_refresh') is True and isinstance(keys, dict):
            # The immutable request itself is durable deduplication, even when
            # acceptance failed or a task was never made (for example stale input).
            for language in _request_languages(config, request):
                attempted.update((language, article_id, key) for article_id, key in keys.items()
                                 if isinstance(key, str))
        if (state.read(f'state/campaigns/{path.stem}.json') is None
                and state.read(f'state/queue-errors/{path.stem}.json') is None):
            pending.append(request)

    groups = defaultdict(list)
    for record in state.records():
        language, article_id = record['language'], record['article_id']
        pub, article = record.get('published'), articles.get(article_id)
        if (language not in config.languages or not pub or pub.get('edit_issue') or pub.get('human_reviewed') is not False
                or article is None or article['issue_id'] not in issue_ids):
            continue
        key = article['translation_key']
        if (pub.get('translation_key') == key or (language, article_id) in active
                or (language, article_id, key) in attempted):
            continue
        if any(_pending_covers(config, source, request, language, article) for request in pending):
            continue
        groups[(article['issue_id'], language)].append(article_id)

    queued = []
    for (issue_id, language), identities in sorted(groups.items()):
        if len(queued) >= policy['max_campaigns_per_tick']:
            break
        # Do not split one issue/language update into multiple independently
        # funded campaigns. Oversized groups get one visible acceptance error.
        article_ids = sorted(identities)
        keys = {article_id: articles[article_id]['translation_key'] for article_id in article_ids}
        # Neither a new discovery timestamp nor a different Git revision permits
        # spending again for the same semantic source fingerprints.
        identity = 'refresh-' + json_hash({'issue_id': issue_id, 'language': language,
                                           'source_translation_keys': keys})
        request = {'id': identity, 'operation': 'translate', 'issues': issue_id, 'languages': language,
                   'article_ids': article_ids, 'source_translation_keys': keys, 'source_refresh': True,
                   'requested_by': 'automatic-source-refresh', 'model': policy['model'],
                   'review_model': policy['review_model'], 'budget_usd': policy['budget_usd'],
                   'dry_run': False, 'retry_failed': False}
        path = f'state/queue/{identity}.json'
        existing = state.read(path)
        if existing is not None:
            if existing != request:
                raise ContractError('Source refresh identity already exists with different inputs')
            continue
        state.write(path, request)
        queued.append(identity)
    if queued:
        # Publish the immutable authorization/selection before the ordinary
        # collector can reserve tasks or create a potentially billable batch.
        engine.checkpoint('runtime: queue bounded automatic source refresh requests')
    return queued
