"""No monetary limit may stop new work or eligible historical backlog.

All source content and provider responses are offline fixtures. These tests prove
scheduling/publication behavior, not the linguistic quality of generated text.
"""
import tempfile
import unittest
from pathlib import Path
from berean_translation import autonomous, spending
from berean_translation.common import ContractError, canonical, json_hash, loads, money
from berean_translation.validation import validate_repository
from support import A, drive, queue, setup


class ProviderManagedSpendingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.config.languages = {'afr': self.config.languages['afr']}
        self.engine.discover()

    def automatic(self):
        self.config.runtime['autonomous_translation'].update(enabled=True, page_size=2, max_active_tasks=2)
        self.config.runtime['automatic_new_translation'] = True

    def validate(self):
        self.state.derive(self.config)
        validate_repository(self.config)

    def test_cost_ledger_has_no_dollar_ceiling(self):
        self.assertEqual(money('1000000001'), 1000000001)
        for value in ('NaN', 'Infinity', '-1'):
            with self.assertRaises(ContractError):
                money(value)

    def test_new_manual_request_needs_no_budget_and_publishes(self):
        request = queue(self.state)
        request.pop('budget_usd')
        self.state.write(f'state/queue/{request["id"]}.json', request)
        drive(self.engine, self.provider)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.assertIsNone(self.state.campaigns()[0]['budget_usd'])
        self.assertEqual(self.state.read(spending.POLICY_PATH), spending.POLICY)
        self.validate()

    def test_already_accepted_campaign_continues_past_original_cap(self):
        request = queue(self.state, budget_usd=0)
        campaign = self.engine.accept_request(request)
        # Pre-policy records had no marker. Preserve their exact request data.
        campaign.pop('spending_policy')
        self.state.save_campaign(campaign)
        request_bytes = self.state.path('state/queue/request-1.json').read_bytes()
        drive(self.engine, self.provider)
        current = self.state.read('state/campaigns/request-1.json')
        self.assertEqual(current['budget_usd'], 0)
        self.assertGreater(current['reserved_usd'], 0)
        self.assertNotIn('spending_policy', current)
        self.assertEqual(self.state.path('state/queue/request-1.json').read_bytes(), request_bytes)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.validate()

    def test_untranslated_budget_backlog_and_new_source_both_drain(self):
        request = queue(self.state, budget_usd=0)
        campaign = self.engine.accept_request(request)
        frozen = {}
        for task in self.state.tasks():
            self.engine.finish(task, 'budget_blocked',
                               'Campaign spending reservation exhausted; no API request submitted')
            frozen[task['id']] = self.state.path(f'state/tasks/{task["id"]}/task.json').read_bytes()
        self.assertFalse(self.state.projection(self.config)['articles'])
        self.automatic()
        drive(self.engine, self.provider, ticks=6)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.assertTrue(all(self.state.path(f'state/tasks/{identity}/task.json').read_bytes() == raw
                            for identity, raw in frozen.items()))
        self.assertEqual(self.state.read(f'state/campaigns/{campaign["id"]}.json')['budget_usd'], 0)
        # A later English revision is processed without another manual campaign.
        self.upstream.change_source()
        drive(self.engine, self.provider, ticks=5)
        self.assertEqual(self.state.record('afr', A)['published']['source_revision'], self.upstream.revision)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.validate()

    def test_legacy_stage_and_cycle_price_holds_resume_saved_reviews(self):
        for reason in ('Request exceeds its frozen complete-stage reservation',
                       'Request exceeds its frozen complete-cycle reservation'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as path:
                config, state, source, provider, git, engine = setup(Path(path))
                config.languages = {'afr': config.languages['afr']}
                engine.discover()
                campaign = engine.accept_request(queue(state))
                old = []
                for task in state.tasks():
                    snapshot = state.source(task)
                    state.save_candidate(task, {'html':snapshot['html'],
                        **{k:snapshot['article'].get(k) for k in ('title','subtitle','section')}})
                    task.update(stage='review2', translation_model_actual=task['model'])
                    state.save_task(task)
                    engine.finish(task, 'not_ready', reason)
                    old.append(state.read(f'state/tasks/{task["id"]}/task.json'))
                config.runtime['autonomous_translation']['enabled'] = True
                config.runtime['automatic_new_translation'] = True
                drive(engine, provider, ticks=4)
                self.assertEqual(len(state.projection(config)['articles']), 2)
                new = [t for t in state.tasks() if t['campaign'] != campaign['id']]
                self.assertTrue(all(t['translation_attempts'] == 0 for t in new))
                for previous in old:
                    self.assertEqual(state.read(f'state/tasks/{previous["id"]}/task.json'), previous)
                validate_repository(config)

    def test_old_automatic_authority_stays_immutable_as_work_exceeds_it(self):
        self.automatic()
        self.config.runtime['autonomous_translation']['page_size'] = 1
        identity = autonomous.enqueue(self.engine)[0]
        campaign = autonomous.accept(self.engine, self.state.read(f'state/queue/{identity}.json'))
        authority = self.state.read(autonomous.AUTHORITY_PATH)
        authority.update(version=1, authorization='historical-fixture',
                         approved_total_usd=campaign['automatic_allocation_usd'])
        authority['sha256'] = json_hash({k:v for k,v in authority.items() if k != 'sha256'})
        self.state.write(autonomous.AUTHORITY_PATH, authority)
        campaign.pop('spending_policy')
        campaign.update(budget_usd=campaign['automatic_allocation_usd'],
                        automatic_approved_total_usd=authority['approved_total_usd'],
                        automatic_authority_sha256=authority['sha256'])
        campaign['automatic_frozen_sha256'] = json_hash(autonomous.frozen(campaign))
        self.state.save_campaign(campaign)
        frozen = canonical(autonomous.frozen(campaign))
        authority_bytes = self.state.path(autonomous.AUTHORITY_PATH).read_bytes()
        # Retired config values are also ignored, including a zero balance.
        self.config.runtime['autonomous_translation'].update(total_budget_usd=0, max_envelope_usd=0)
        drive(self.engine, self.provider, ticks=7)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        self.assertGreater(autonomous.ledger(self.state)['gross_allocated_usd'], authority['approved_total_usd'])
        self.assertEqual(self.state.path(autonomous.AUTHORITY_PATH).read_bytes(), authority_bytes)
        self.assertEqual(canonical(autonomous.frozen(self.state.read(f'state/campaigns/{identity}.json'))), frozen)
        self.validate()

    def test_provider_usage_above_estimate_is_recorded_without_stopping_admission(self):
        self.automatic(); self.engine.tick(); self.provider.complete_all()
        for key in list(self.provider.files):
            if key.endswith('-output'):
                rows = [loads(line) for line in self.provider.files[key].splitlines()]
                for row in rows:
                    row['response']['body']['usage'] = {'prompt_tokens':10000000, 'completion_tokens':10000000}
                self.provider.files[key] = b'\n'.join(canonical(row) for row in rows)
        drive(self.engine, self.provider, ticks=4)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        events = list(autonomous.ledger(self.state)['settlements'].values())
        self.assertTrue(events)
        for event in events:
            self.assertGreater(event['provider_reported_usage_priced_usd'], event['allocation_usd'])
            self.assertEqual(event['released_usd'], 0)
        self.validate()

    def test_spending_policy_event_is_idempotent_and_tampering_is_rejected(self):
        spending.record(self.engine)
        original = self.state.path(spending.POLICY_PATH).read_bytes()
        checkpoints = len(self.git.checkpoints)
        spending.record(self.engine)
        self.assertEqual(len(self.git.checkpoints), checkpoints)
        self.assertEqual(self.state.path(spending.POLICY_PATH).read_bytes(), original)
        self.state.write(spending.POLICY_PATH, {**spending.POLICY, 'authorization':'changed'})
        with self.assertRaisesRegex(ContractError, 'Spending policy audit record changed'):
            self.engine.tick()
        with self.assertRaisesRegex(ContractError, 'Spending policy audit record changed'):
            validate_repository(self.config)
        self.assertEqual(self.provider.create_calls, 0)
