"""Fresh translation fairness and append-only migration of rejected candidates."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from berean_translation import autonomous
from berean_translation.common import canonical
from support import A, B, queue, setup


class PlainSchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.config, self.state, self.source, self.provider,
         self.git, self.engine) = setup(self.root)
        self.config.languages = {'afr': self.config.languages['afr']}
        self.config.runtime['autonomous_translation'].update(enabled=False, page_size=2,
                                                             max_active_tasks=50)
        self.config.runtime['automatic_new_translation'] = False

    def old_candidate(self, kind='quality_rejection'):
        second = copy.deepcopy(self.source.articles[1])
        self.source.articles = self.source.articles[:1]
        self.source.rebuild()
        self.engine.discover()
        self.config.runtime['prompt_version'] = '1.0.3'
        accepted = self.engine.accept_request(queue(self.state, 'old-candidate', languages='afr'))
        task = self.state.read(f'state/tasks/{accepted["tasks"][0]}/task.json')
        snapshot = self.state.source(task)
        candidate = {k: snapshot['article'].get(k) for k in ('title', 'subtitle', 'section')}
        candidate['html'] = snapshot['html']
        self.state.save_candidate(task, candidate)
        task.update(stage='review2', failure_kind=kind, translation_model_actual='gpt-4.1-mini',
                    findings=[{'severity': 'major', 'location': 'p', 'source_quote': 'Faith',
                               'translation_quote': 'Faith', 'suggested_fix': 'Legacy strict finding'}])
        self.state.save_task(task)
        self.engine.finish(task, 'not_ready', 'Rejected under the old quotation contract')
        self.source.articles.append(second)
        self.source.rebuild()
        self.engine.discover()
        self.config.runtime['prompt_version'] = '2.0.0'
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        return self.state.read(f'state/tasks/{task["id"]}/task.json')

    def test_page_contains_fresh_translation_and_saved_candidate_review(self):
        old = self.old_candidate()
        original = canonical(old)
        identities = autonomous.enqueue(self.engine)
        requests = [self.state.read(f'state/queue/{identity}.json') for identity in identities]
        self.assertEqual([(r['selection']['article_id'], r['selection']['stage']) for r in requests],
                         [(B, 'translate'), (A, 'review1')])
        for request in requests:
            autonomous.accept(self.engine, request)
        migrated = next(t for t in self.state.tasks() if t.get('automatic_previous_task') == old['id'])
        self.assertEqual(migrated['findings'], [])
        campaign = self.state.read(f'state/campaigns/{migrated["campaign"]}.json')
        self.assertEqual(campaign['prompt_version'], '2.0.0')
        self.assertEqual(set(migrated['stage_budget']['stages_usd']), {'review1', 'correct', 'review2'})
        self.assertEqual(canonical(self.state.read(f'state/tasks/{old["id"]}/task.json')), original)
        self.assertLessEqual(autonomous.ledger(self.state)['allocated_usd'], 30)

    def test_admission_alternates_lanes_instead_of_exhausting_repair_prefix(self):
        self.old_candidate()
        identities = autonomous.enqueue(self.engine)
        paths = [self.root / f'state/queue/{identity}.json' for identity in reversed(identities)]
        ordered = autonomous.fair_admission_paths(self.state, paths)
        self.assertEqual([self.state.read(f'state/queue/{p.stem}.json')['selection']['recovery']
                          for p in ordered], [False, True])

    def test_legacy_unaccepted_queue_is_retained_and_superseded_without_funding(self):
        self.old_candidate()
        self.config.runtime['prompt_version'] = '1.0.3'
        old_ids = autonomous.enqueue(self.engine)
        old_requests = {identity: canonical(self.state.read(f'state/queue/{identity}.json'))
                        for identity in old_ids}
        self.assertEqual(autonomous.ledger(self.state)['allocated_usd'], 0)
        self.config.runtime['prompt_version'] = '2.0.0'
        new_ids = autonomous.enqueue(self.engine)
        self.assertTrue(new_ids)
        self.assertFalse(set(old_ids) & set(new_ids))
        for identity, original in old_requests.items():
            self.assertEqual(canonical(self.state.read(f'state/queue/{identity}.json')), original)
            request = self.state.read(f'state/queue/{identity}.json')
            error = self.state.read(f'state/queue-errors/{identity}.json')
            if request['selection']['article_id'] == A:
                self.assertIn('superseded', error['error'])
            else:
                self.assertIsNone(error)
        self.assertEqual([self.state.read(f'state/queue/{identity}.json')['selection']['stage']
                          for identity in new_ids], ['review1'])
        self.assertEqual(autonomous.ledger(self.state)['allocated_usd'], 0)

    def test_new_contract_review_does_not_repeat_for_current_review_overflow(self):
        old = self.old_candidate('invalid_result')
        self.state.write(f'state/tasks/{old["id"]}/results/review2.json',
                         {'result': {'findings': [{} for _ in range(49)]}})
        self.assertEqual(autonomous.resume_stage(self.state, old), ('review1', None))
        current_campaign = self.state.read(f'state/campaigns/{old["campaign"]}.json')
        current_campaign.update(id='current-review', prompt_version='2.0.0')
        self.state.write('state/campaigns/current-review.json', current_campaign)
        current_review = {**old, 'campaign': 'current-review'}
        self.assertEqual(autonomous.resume_stage(self.state, current_review),
                         (None, 'preserved_review_overflow_requires_attention'))
        resumed_manual_review = {**old, 'plain_policy_resumed': True}
        self.assertEqual(autonomous.resume_stage(self.state, resumed_manual_review),
                         (None, 'preserved_review_overflow_requires_attention'))

    def test_unknown_paid_outcome_is_held_even_with_a_saved_candidate(self):
        old = self.old_candidate()
        old.pop('failure_kind')
        old['failure'] = 'Missing, ambiguous, or truncated model response'
        self.assertEqual(autonomous.resume_stage(self.state, old),
                         (None, 'legacy_response_outcome_unknown_requires_owner_attention'))

    def test_refusals_remain_held_and_proven_unsubmitted_policy_retirement_can_translate(self):
        old = self.old_candidate('provider_refusal')
        self.assertEqual(autonomous.resume_stage(self.state, old),
                         (None, 'provider_refusal_requires_owner_attention'))
        retired = {**old, 'id': 'f' * 32, 'failure_kind': 'policy_retired',
                   'failure': 'Never-submitted stage retired', 'provider_failure': {}}
        self.assertEqual(autonomous.resume_stage(self.state, retired), ('translate', None))


if __name__ == '__main__':
    unittest.main()
