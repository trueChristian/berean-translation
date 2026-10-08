"""Current processing view; historical authorizations and request bytes stay frozen."""
from __future__ import annotations

import copy


def effective_campaign(state, task, campaign, config):
    """Use current prompts and acceptance, retaining original models and funding.

    Already-paid responses are parsed against their original recorded schema;
    this view is for current processing and new request construction.
    """
    result = copy.deepcopy(campaign)
    result.update({'prompt_version': config.runtime['prompt_version'],
                   'prompts': {name: config.prompt(name) for name in ('translation', 'review', 'repair')},
                   'quality_threshold': config.runtime['quality_threshold'],
                   'upgrade_quality_threshold': config.runtime.get('upgrade_quality_threshold', 98)})
    result.pop('scripture_quotes', None)
    if 'review_contract_version' in config.runtime:
        result['review_contract_version'] = config.runtime['review_contract_version']
    else:
        result.pop('review_contract_version', None)
    return result
