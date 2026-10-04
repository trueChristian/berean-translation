"""Audited usage settlement for new automatic authority only; no real API calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from berean_translation import autonomous
from berean_translation.common import ContractError, canonical, json_hash
from support import setup, drive


class AutomaticSettlementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        self.config.runtime['autonomous_translation'].update(enabled=True, page_size=1, max_active_tasks=1)
        self.config.runtime['automatic_new_translation'] = True
        self.config.languages = {'afr': self.config.languages['afr']}
        self.engine.discover()

    def events(self):
        return list((self.root / 'state/automatic-settlements').glob('*.json'))

    def one_task(self):
        self.upstream.articles = self.upstream.articles[:1]
        self.upstream.rebuild(); self.engine.discover()

    def test_known_usage_releases_only_unused_reservation_idempotently(self):
        self.one_task(); drive(self.engine, self.provider, ticks=4)
        funding = autonomous.ledger(self.state)
        self.assertGreater(funding['released_usd'], 0)
        self.assertGreater(funding['allocated_usd'], 0)
        self.assertEqual(len(self.events()), 1)
        event_bytes = self.events()[0].read_bytes()
        autonomous.settle(self.engine); autonomous.settle(self.engine)
        self.assertEqual(self.events()[0].read_bytes(), event_bytes)
        self.assertEqual(autonomous.ledger(self.state), funding)

    def test_budget_resume_uses_audited_settlement_within_original_cap(self):
        self.config.runtime['autonomous_translation'].update(total_budget_usd=0.1, max_envelope_usd=0.1)
        drive(self.engine, self.provider, ticks=7)
        self.assertEqual(len(self.state.projection(self.config)['articles']), 2)
        funding = autonomous.ledger(self.state)
        self.assertLessEqual(funding['allocated_usd'], 0.1)
        campaigns = sorted(self.state.campaigns(), key=lambda c: c['automatic_allocation_index'])
        self.assertTrue(campaigns[1]['automatic_settlement_credits'])
        self.assertEqual(campaigns[0]['automatic_settlement_credits'], {})
        self.assertGreater(funding['gross_allocated_usd'], funding['allocated_usd'])

    def test_unknown_or_partial_usage_retains_entire_envelope(self):
        self.one_task(); self.engine.tick(); self.provider.complete_all()
        # This is an actual provider-row simulation, not edited settled evidence.
        from berean_translation.common import loads
        for key in list(self.provider.files):
            if key.endswith('-output'):
                rows = [loads(line) for line in self.provider.files[key].splitlines()]
                for row in rows:
                    row['response']['body']['usage'] = {'prompt_tokens': 200}
                self.provider.files[key] = b'\n'.join(canonical(row) for row in rows)
        drive(self.engine, self.provider, ticks=4)
        funding = autonomous.ledger(self.state)
        self.assertEqual(funding['released_usd'], 0)
        self.assertEqual(funding['allocated_usd'], funding['gross_allocated_usd'])
        self.assertFalse(self.events())

    def test_cancelled_submitted_batch_waits_for_known_usage(self):
        self.one_task(); self.engine.tick(); self.provider.complete_all()
        campaign = self.state.campaigns()[0]
        self.engine.cancel_campaign(campaign['id'])
        self.assertFalse(self.events())
        self.engine.tick()
        self.assertEqual(len(self.events()), 1)
        funding = autonomous.ledger(self.state)
        self.assertGreater(funding['allocated_usd'], 0)
        self.assertGreater(funding['released_usd'], 0)

    def test_cancelled_submitted_without_response_usage_keeps_envelope(self):
        self.one_task(); self.engine.tick()
        campaign = self.state.campaigns()[0]
        self.engine.cancel_campaign(campaign['id']); self.engine.tick()
        self.assertFalse(self.events())
        self.assertEqual(float(autonomous.ledger(self.state)['allocated_usd']), campaign['budget_usd'])

    def test_failed_result_with_complete_usage_can_settle(self):
        self.one_task(); self.engine.tick()
        self.provider.complete_all(lambda line: {'unexpected':'invalid shape'})
        self.engine.collect()
        # First-line structural failure can use its funded correction; fail that too.
        self.engine.prepare(); self.provider.complete_all(lambda line: {'unexpected':'invalid shape'})
        self.engine.collect(); autonomous.settle(self.engine)
        self.assertEqual(len(self.events()), 1)
        self.assertGreater(autonomous.ledger(self.state)['released_usd'], 0)

    def test_uncertain_create_cannot_settle_or_create_again(self):
        self.one_task(); self.provider.raise_create = 'after'; self.engine.tick()
        campaign = self.state.campaigns()[0]
        self.assertFalse(self.events())
        self.engine.tick()
        self.assertEqual(self.provider.create_calls, 1)
        self.assertEqual(float(autonomous.ledger(self.state)['allocated_usd']), campaign['budget_usd'])

    def test_settlement_tampering_duplicate_and_evidence_change_fail_closed(self):
        self.one_task(); drive(self.engine, self.provider, ticks=4)
        path = self.events()[0]; original = path.read_bytes()
        event = self.state.read(str(path.relative_to(self.root)))
        event['released_usd'] += 0.01
        event['sha256'] = json_hash({k:v for k,v in event.items() if k != 'sha256'})
        path.write_bytes(canonical(event))
        with self.assertRaises(ContractError): autonomous.ledger(self.state)
        path.write_bytes(original)
        duplicate = path.with_name('duplicate.json'); duplicate.write_bytes(original)
        with self.assertRaises(ContractError): autonomous.ledger(self.state)
        duplicate.unlink()
        task = self.state.tasks()[0]
        evidence = self.state.read(f'state/tasks/{task["id"]}/attempts/translate.json')
        evidence['response']['usage']['completion_tokens'] = 0
        self.state.write(f'state/tasks/{task["id"]}/attempts/translate.json', evidence)
        with self.assertRaises(ContractError): autonomous.ledger(self.state)

    def test_two_admissions_share_cap_and_replays_cannot_double_allocate(self):
        self.config.runtime['autonomous_translation'].update(page_size=2, max_active_tasks=2,
                                                           total_budget_usd=0.1, max_envelope_usd=0.1)
        autonomous.enqueue(self.engine); self.engine.accept_queue(); self.engine.accept_queue()
        funding = autonomous.ledger(self.state)
        self.assertLessEqual(funding['allocated_usd'], 0.1)
        self.assertEqual(funding['count'], 1)
        self.assertEqual(len({c['automatic_allocation_index'] for c in self.state.campaigns()}), funding['count'])


if __name__ == '__main__': unittest.main()
