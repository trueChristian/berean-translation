"""Offline reproduction of manual prefetch expiry and immutable resumption.

Every request, source, publication, provider call and deadline is a temporary
fixture. No production state is migrated and no network or paid API is used.
"""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from berean_translation import autonomous, manual_admission
from berean_translation.collector import collect_window
from berean_translation.common import ContractError, canonical, json_hash
from berean_translation.engine import Engine
from berean_translation.scripture_provider import GetBibleMCP
from berean_translation.validation import validate_repository
from support import A, B, ISSUE2, REPO_ROOT, drive, queue, setup
from test_collector_deadline import FakeClock
from test_scripture_evidence import FakeMCP, fixture


class ManualAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.state, self.upstream, self.provider, self.git, self.engine = setup(self.root)
        shutil.copytree(REPO_ROOT/'data', self.root/'data')
        shutil.copytree(REPO_ROOT/'docs/third-party', self.root/'docs/third-party')
        self.config.runtime['scripture_quotes_enabled'] = True
        self.english = fixture('kjv')['structuredContent']['data']['verses'][1]['text']
        for identity in (A, B):
            path = f'content/articles/{identity}.html'
            self.upstream.contents[path] = self.upstream.contents[path].replace(
                'Faith and <em>grace</em>. John 3:16–18.', f'“{self.english}” (John 4:16).')
        self.upstream.rebuild()
        self.engine.discover()
        self.fake = FakeMCP()
        self.engine.scripture_provider = GetBibleMCP(self.fake)
        for name in ('socket.socket.connect', 'socket.getaddrinfo'):
            blocker = patch(name, side_effect=AssertionError('Network forbidden in offline test'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def restart(self):
        self.engine = Engine(self.config, self.upstream.client, self.provider, self.git)
        self.engine.scripture_provider = GetBibleMCP(self.fake)
        return self.engine

    def expire(self, *, at=1, identity='manual', **changes):
        request = queue(self.state, identity, languages='deu', **changes)
        clock = FakeClock()
        original = self.engine.scripture_provider.chapter
        count = 0

        def chapter(*args):
            nonlocal count
            value = original(*args)
            count += 1
            if count == at:
                clock.expire()
            return value

        with patch.object(self.engine.scripture_provider, 'chapter', side_effect=chapter):
            result = collect_window(self.engine, wait_seconds=600,
                monotonic=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(result['stop_reason'], 'wait_budget_exhausted')
        return request

    def ledger(self, identity='manual'):
        return self.state.read(manual_admission.path(identity))

    def campaign(self, identity='manual'):
        return self.state.read(f'state/campaigns/{identity}.json')

    def legacy(self, **changes):
        request = self.expire(**changes)
        campaign = self.campaign(request['id'])
        for item in campaign['selection']:
            if not any(row['article_id'] == item['article_id'] and row['language'] == item['language']
                       for row in campaign['skipped']):
                campaign['skipped'].append({**item, 'reason':'prefetch_wait_budget', 'detail':'fixture deadline'})
        campaign['status'] = 'finished'  # Actual pre-fix zero-task outcome.
        campaign.pop('admission_counts', None)
        self.state.save_campaign(campaign)
        self.state.path(manual_admission.path(request['id'])).unlink()
        self.restart()
        return request

    def test_real_deadline_restart_queue_reuses_manual_selection_and_frozen_envelope(self):
        request = self.expire(budget_usd=30)
        before = self.campaign()
        self.assertEqual(before['tasks'], [])
        self.assertEqual(before['status'], 'admission_pending')
        self.assertEqual(before['reserved_usd'], 0)
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(self.state.read('state/heartbeat.json')['unfinished_manual_admissions'], 2)
        self.assertIn('admission pending', self.state.path('STATUS.md').read_text())
        self.restart()
        with patch.object(self.engine, 'select_issues', side_effect=AssertionError('Frozen next must never rerun')):
            self.engine.accept_queue()
        after = self.campaign()
        self.assertEqual(len(after['tasks']), 2)
        self.assertEqual(after['selection'], before['selection'])
        self.assertEqual(after['skipped'], before['skipped'])
        self.assertEqual(after['request_sha256'], json_hash(request))
        for key in ('budget_usd', 'reserved_usd', 'reported_usage_usd', 'models', 'prompts', 'language_settings', 'scripture_quotes'):
            self.assertEqual(after[key], before[key])
        self.assertFalse(any(t.get('autonomous') for t in self.state.tasks()))
        self.assertEqual({t['id'] for t in self.state.tasks()},
                         {manual_admission.task_id(before, row) for row in before['selection']})
        self.assertEqual(self.provider.create_calls, 0)
        validate_repository(self.config)

    def test_second_deadline_and_duplicate_reordered_replay_are_idempotent(self):
        request = self.expire()
        self.restart()
        clock = FakeClock()
        original = self.engine.scripture_provider.chapter
        def slow(*args):
            result = original(*args)
            clock.expire()
            return result
        with patch.object(self.engine.scripture_provider, 'chapter', side_effect=slow):
            collect_window(self.engine, wait_seconds=600, monotonic=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(self.campaign()['tasks'], [])
        self.restart().accept_queue()
        first = {p: p.read_bytes() for p in self.root.glob('state/**/*.json')}
        reordered = dict(reversed(list(request.items())))
        self.engine.accept_request(reordered)
        self.engine.accept_queue()
        self.assertEqual(first, {p:p.read_bytes() for p in first})
        self.assertEqual(len(self.state.tasks()), 2)
        self.assertTrue(all(len(r['history']) == 1 for r in self.state.records()))

    def test_mixed_admissions_leave_completed_work_and_attempts_untouched(self):
        request = self.expire(at=3)
        campaign = self.campaign()
        self.assertEqual(len(campaign['tasks']), 1)
        task = self.state.tasks()[0]
        task.update(translation_attempts=2, review_attempts=2)
        self.state.save_task(task)
        self.engine.finish(task, 'complete')
        frozen_task = self.state.path(f'state/tasks/{task["id"]}/task.json').read_bytes()
        frozen_record = copy.deepcopy(self.state.record(task['language'], task['article_id']))
        self.restart().accept_request(request)
        self.assertEqual(len(self.campaign()['tasks']), 2)
        self.assertEqual(frozen_task, self.state.path(f'state/tasks/{task["id"]}/task.json').read_bytes())
        self.assertEqual(frozen_record, self.state.record(task['language'], task['article_id']))
        self.state.derive(self.config)
        validate_repository(self.config)

    def test_changed_config_uses_original_models_prices_prompts_language_and_scripture(self):
        request = self.expire()
        before = self.campaign()
        self.config.models['gpt-4.1-mini']['input_batch_usd_per_million'] = 9999
        self.config.languages['deu']['tag'] = 'af'
        self.config.runtime.update(default_model='gpt-6-luna', scripture_quotes_enabled=False)
        (self.root/'prompts/translation.txt').write_text('changed prompt')
        self.restart().accept_request(request)
        after = self.campaign()
        for key in ('models', 'prompts', 'language_settings', 'scripture_quotes', 'model', 'review_model'):
            self.assertEqual(after[key], before[key])
        for task in self.state.tasks():
            self.assertEqual(task['models'], before['models'])
            evidence = self.state.read(task['scripture_evidence_path'])
            self.assertEqual(evidence['language_tag'], 'de')
            self.assertEqual(evidence['edition']['abbreviation'], 'luther1545')

    def test_changed_source_holds_but_unrelated_revision_can_resume_original_snapshot(self):
        request = self.expire()
        original_paths = {i['provenance']['task']['source_snapshot'] for i in self.ledger()['entries'].values()}
        self.upstream.revision = 'b'*40
        self.upstream.rebuild()
        self.restart().discover()
        self.engine.accept_request(request)
        self.assertEqual({t['source_snapshot'] for t in self.state.tasks()}, original_paths)
        self.assertEqual({self.state.source(t)['revision'] for t in self.state.tasks()}, {'a'*40})

    def test_changed_content_is_attention_and_does_not_admit_changed_source(self):
        request = self.expire()
        self.upstream.revision = 'b'*40
        path = f'content/articles/{A}.html'
        self.upstream.contents[path] = self.upstream.contents[path].replace('</article>', '<p>Changed source.</p></article>')
        self.upstream.rebuild()
        self.restart().discover()
        self.engine.accept_request(request)
        entry = next(e for e in self.ledger()['entries'].values() if e['item']['article_id'] == A)
        self.assertEqual(entry['status'], 'attention')
        self.assertEqual(entry['reason'], 'source_changed_since_manual_selection')
        self.assertEqual([t['article_id'] for t in self.state.tasks()], [B])
        self.assertFalse(manual_admission.covered(self.state, 'deu', A,
                         self.state.read('state/source.json')['articles'][A]['translation_key']))

    def test_human_edit_and_changed_latest_task_block_late_admission(self):
        request = self.expire()
        content = self.root/f'content/deu/articles/{A}.html'
        content.parent.mkdir(parents=True)
        content.write_text('Human words must survive.')
        record = self.state.record('deu', B)
        record['latest_task'] = 'e'*32
        record['history'].append({'event':'requested', 'task':'e'*32, 'at':'2099-01-01T00:00:00Z'})
        self.state.save_record(record)
        self.restart().accept_request(request)
        reasons = {e['reason'] for e in self.ledger()['entries'].values()}
        self.assertIn('human_reviewed_or_edited_protected', reasons)
        self.assertIn('publication_or_latest_task_changed_since_manual_selection', reasons)
        self.assertEqual(self.state.tasks(), [])
        self.assertEqual(content.read_text(), 'Human words must survive.')

    def test_manual_overlap_and_automatic_enqueue_accept_cannot_steal_pending_pair(self):
        self.expire()
        self.restart()
        other = self.engine.accept_request(queue(self.state, 'other', languages='deu', issues='all'))
        self.assertEqual(other['tasks'], [])
        self.assertTrue(all(i['reason'] == 'manual_admission_pending' for i in other['skipped']))
        self.config.languages = {'deu':self.config.languages['deu']}
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        self.assertEqual(autonomous.enqueue(self.engine), [])
        article = self.state.read('state/source.json')['articles'][A]
        spec = {'language':'deu', 'article_id':A, 'translation_key':article['translation_key'],
                'issue_id':article['issue_id'], 'stage':'translate', 'previous_task_id':None,
                'previous_task_sha256':None, 'candidate_sha256':None, 'recovery':False}
        automatic = {'id':'auto-'+json_hash(spec), 'operation':'automatic', 'autonomous':True,
                     'requested_by':'owner-authorized-autonomous-archive', 'selection':spec}
        with self.assertRaisesRegex(ContractError, 'manual_admission_pending'):
            autonomous.accept(self.engine, automatic)
        self.assertEqual(autonomous.ledger(self.state)['allocated_usd'], 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_cancellation_remains_cancelled_across_restarts(self):
        request = self.expire()
        self.restart().cancel_campaign('manual')
        self.restart().accept_request(request)
        self.engine.tick()
        self.assertEqual(self.state.tasks(), [])
        self.assertTrue(self.campaign()['cancel_requested'])
        self.assertEqual(self.campaign()['status'], 'finished')
        self.assertEqual({e['status'] for e in self.ledger()['entries'].values()}, {'cancelled'})
        self.assertEqual(self.provider.create_calls, 0)
        validate_repository(self.config)

    def test_request_and_campaign_tampering_fail_before_paid_submission(self):
        request = self.expire(budget_usd='0.000001')
        changed = {**request, 'budget_usd':30}
        with self.assertRaisesRegex(ContractError, 'immutable inputs'):
            self.restart().accept_request(changed)
        self.engine.accept_request(request)
        campaign = self.campaign()
        campaign['budget_usd'] = 30
        self.state.save_campaign(campaign)
        with self.assertRaisesRegex(ContractError, 'frozen request or campaign changed'):
            self.engine.prepare()
        self.assertEqual(self.provider.create_calls, 0)

    def test_original_tight_budget_never_falls_back_during_admission_or_prepare(self):
        request = self.expire(budget_usd='0.000001')
        self.restart().accept_request(request)
        self.engine.prepare()
        self.assertEqual(self.campaign()['budget_usd'], 0.000001)
        self.assertEqual(self.campaign()['reserved_usd'], 0)
        self.assertEqual({t['status'] for t in self.state.tasks()}, {'budget_blocked'})
        self.assertEqual(self.provider.create_calls, 0)
        self.assertEqual(len(self.state.campaigns()), 1)
        self.assertIsNone(self.state.read('state/automatic-authority.json'))

    def test_legacy_translate_requires_original_hash_and_unique_snapshot(self):
        request = self.legacy()
        original = copy.deepcopy(self.campaign())
        self.engine.accept_request(request)
        self.assertTrue(self.ledger()['legacy'])
        self.assertEqual(len(self.campaign()['tasks']), 2)
        for key in ('selection', 'skipped', 'budget_usd', 'reserved_usd', 'reported_usage_usd'):
            self.assertEqual(self.campaign()[key], original[key])
        self.state.derive(self.config)
        validate_repository(self.config)

    def test_legacy_missing_and_ambiguous_snapshots_require_attention(self):
        request = self.legacy()
        paths = list((self.root/'state/sources').glob('*.json'))
        source_a = next(p for p in paths if self.state.read(str(p.relative_to(self.root)))['article']['id'] == A)
        source_a.unlink()
        source_b = next(self.state.read(str(p.relative_to(self.root))) for p in paths if p.exists())
        extra = {**source_b, 'fixture_extra':'ambiguous history'}
        self.state.write(f'state/sources/{json_hash(extra)}.json', extra)
        self.engine.accept_request(request)
        self.assertEqual(self.state.tasks(), [])
        self.assertEqual({e['reason'] for e in self.ledger()['entries'].values()}, {'legacy_source_snapshot_missing_or_ambiguous'})
        self.assertEqual(self.campaign()['status'], 'admission_attention')

    def test_legacy_changed_request_contract_is_not_a_new_authority(self):
        request = self.legacy(budget_usd='0.000001')
        campaign = self.campaign()
        campaign['budget_usd'] = 30
        self.state.save_campaign(campaign)
        self.engine.accept_request(request)
        self.assertEqual(self.state.tasks(), [])
        self.assertEqual({e['reason'] for e in self.ledger()['entries'].values()}, {'legacy_request_contract_unproven'})
        self.assertEqual(self.provider.create_calls, 0)

    def test_legacy_duplicate_and_reordered_skips_materialize_each_target_once(self):
        request = self.legacy()
        campaign = self.campaign()
        campaign['skipped'] = list(reversed(campaign['skipped'] * 2))
        frozen = copy.deepcopy(campaign['skipped'])
        self.state.save_campaign(campaign)
        self.engine.accept_request(request)
        self.engine.accept_request(dict(reversed(list(request.items()))))
        self.assertEqual(len(self.state.tasks()), 2)
        self.assertEqual(self.campaign()['skipped'], frozen)

    def test_permanent_evidence_hold_does_not_retry_or_block_explicit_later_request(self):
        # Provider failures are deliberately not promoted to deadline deferrals.
        self.fake.failure = RuntimeError('fixture unavailable')
        request = queue(self.state, 'hold', languages='deu', issues='all')
        self.engine.accept_request(request)
        self.assertEqual({e['status'] for e in self.ledger('hold')['entries'].values()}, {'attention'})
        calls = len(self.fake.calls)
        self.engine.accept_queue()
        self.assertEqual(len(self.fake.calls), calls)
        self.fake.failure = None
        self.config.languages = {'deu':self.config.languages['deu']}
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        self.assertEqual(autonomous.enqueue(self.engine), [])
        self.assertEqual(self.provider.create_calls, 0)
        next_request = queue(self.state, 'later', languages='deu', issues='all')
        self.engine.accept_request(next_request)
        self.assertEqual(len(self.campaign('later')['tasks']), 2)
        task = self.state.tasks()[0]
        source = self.state.source(task)
        self.state.save_candidate(task, {**{k:source['article'][k] for k in ('title','subtitle','section')}, 'html':source['html']})
        task.update(stage='review1', translation_model_actual=task['model'])
        self.state.save_task(task)
        self.engine.finish(task, 'budget_blocked')
        article = self.state.read('state/source.json')['articles'][task['article_id']]
        spec, reason = autonomous.selection(self.engine, task['language'], article, self.state.tasks()[0])
        self.assertEqual(reason, None)
        self.assertEqual(spec['stage'], 'review1')

    def test_completed_ledgers_do_not_starve_later_queue_requests(self):
        self.engine.accept_request(queue(self.state, 'a-old', languages='deu'))
        self.config.runtime['max_pending_campaigns_per_tick'] = 1
        queue(self.state, 'z-new', languages='afr')
        self.engine.accept_queue()
        self.assertIsNotNone(self.campaign('z-new'))

    def test_claim_index_reads_campaigns_once_for_a_selection_scan(self):
        self.expire()
        self.restart()
        self.config.languages = {'deu':self.config.languages['deu']}
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        original = manual_admission.claims
        with patch('berean_translation.manual_admission.claims', wraps=original) as build:
            autonomous.enqueue(self.engine)
        self.assertEqual(build.call_count, 1)

    def test_initial_campaign_write_crash_restores_original_frozen_contract(self):
        request = queue(self.state, 'init-crash', languages='deu')
        with patch.object(self.engine.state, 'save_campaign', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                self.engine.accept_request(request)
        ledger = self.ledger('init-crash')
        self.assertIsNone(self.campaign('init-crash'))
        self.config.runtime['default_model'] = 'gpt-6-luna'
        self.config.runtime['autonomous_translation']['enabled'] = True
        self.config.runtime['automatic_new_translation'] = True
        self.config.languages = {'deu':self.config.languages['deu']}
        self.restart().tick()
        self.assertEqual(manual_admission.contract(self.campaign('init-crash')),
                         manual_admission.contract(ledger['initial_campaign']))
        self.assertEqual(len(self.state.tasks()), 2)

    def test_partial_task_record_campaign_writes_resume_without_reset_or_duplicates(self):
        request = self.expire()
        self.restart()
        original = self.engine.state.save_record
        def crash_after_record(record):
            original(record)
            raise OSError('fixture crash after record')
        with patch.object(self.engine.state, 'save_record', side_effect=crash_after_record):
            with self.assertRaises(OSError):
                self.engine.accept_request(request)
        task = self.state.tasks()[0]
        self.assertFalse(manual_admission.admitted(self.state, task))
        self.engine.prepare()
        self.assertEqual(self.provider.create_calls, 0)
        self.restart().accept_request(request)
        self.assertEqual(len(self.state.tasks()), 2)
        self.assertTrue(all(len(r['history']) == 1 for r in self.state.records()))
        self.assertTrue(all(t['translation_attempts'] == t['review_attempts'] == 0 for t in self.state.tasks()))

    def test_failed_initial_checkpoint_propagates_and_restart_resumes(self):
        request = queue(self.state, languages='deu')
        with patch.object(self.git, 'checkpoint', side_effect=ContractError('fixture push rejected')):
            with self.assertRaisesRegex(ContractError, 'push rejected'):
                self.engine.accept_request(request)
        self.assertEqual(self.state.tasks(), [])
        self.restart().accept_queue()
        self.assertEqual(len(self.state.tasks()), 2)

    def test_expired_boundary_stops_before_quote_free_next_target(self):
        path = f'content/articles/{B}.html'
        self.upstream.contents[path] = self.upstream.contents[path].replace(f'“{self.english}” (John 4:16).', 'Plain prose.')
        self.upstream.rebuild()
        request = self.expire(at=2)
        self.assertEqual(len(self.campaign()['tasks']), 1)
        self.assertEqual(self.campaign()['admission_counts']['pending'], 1)
        self.restart().accept_request(request)
        self.assertEqual(len(self.campaign()['tasks']), 2)

    def test_new_review_candidate_is_frozen_before_deadline_legacy_review_is_held(self):
        self.config.runtime['scripture_quotes_enabled'] = False
        queue(self.state, 'original', languages='deu')
        drive(self.engine, self.provider)
        self.assertTrue(all(t['status'] == 'complete' for t in self.state.tasks()))
        self.config.runtime['scripture_quotes_enabled'] = True
        request = self.expire(identity='review', operation='review')
        candidate_proofs = [e['provenance'] for e in self.ledger('review')['entries'].values()]
        self.assertTrue(all(p['candidate_path'] and p['candidate_sha256'] for p in candidate_proofs))
        self.restart().accept_request(request)
        campaign = self.campaign('review')
        self.assertEqual(len(campaign['tasks']), 2)
        for identity in campaign['tasks']:
            task = self.state.read(f'state/tasks/{identity}/task.json')
            proof = self.ledger('review')['entries'][identity]['provenance']
            self.assertEqual(json_hash(self.state.candidate(task)), proof['candidate_sha256'])
        # A true legacy review lacks a frozen pre-deadline candidate binding.
        request2 = queue(self.state, 'legacy-review', languages='deu', operation='review')
        legacy = copy.deepcopy(self.campaign('review'))
        legacy.update(id='legacy-review', request_sha256=json_hash(request2), tasks=[], status='finished')
        legacy['skipped'] = [{**i, 'reason':'prefetch_wait_budget'} for i in legacy['selection']]
        self.state.save_campaign(legacy)
        self.engine.accept_request(request2)
        self.assertEqual({e['reason'] for e in self.ledger('legacy-review')['entries'].values()},
                         {'legacy_review_candidate_provenance_unproven'})

    def test_cancel_partial_materialization_keeps_campaign_and_record_history(self):
        self.expire()
        self.restart()
        original = self.engine.state.save_task
        def crash(task):
            original(task)
            raise OSError('fixture crash after task save')
        with patch.object(self.engine.state, 'save_task', side_effect=crash):
            with self.assertRaises(OSError):
                self.engine.accept_queue()
        self.restart().cancel_campaign('manual')
        self.engine.cancel_campaign('manual')
        task, = self.state.tasks()
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(self.campaign()['tasks'], [task['id']])
        events = self.state.record(task['language'], task['article_id'])['history']
        self.assertEqual([e['event'] for e in events], ['requested', 'cancelled'])
        validate_repository(self.config)

    def test_legacy_missing_or_same_second_predecessor_is_unproven(self):
        request = self.legacy()
        for article_id in (A, B):
            record = self.state.record('deu', article_id)
            identity = ('e' if article_id == A else 'f')*32
            record.update(latest_task=identity, history=[{'event':'requested', 'task':identity,
                          'at':self.campaign()['created_at']}])
            self.state.save_record(record)
            if article_id == B:
                snapshot = next(self.state.read(str(p.relative_to(self.root))) for p in
                                (self.root/'state/sources').glob('*.json')
                                if self.state.read(str(p.relative_to(self.root)))['article']['id'] == B)
                task = manual_admission._template(self.campaign(), {'language':'deu', 'article_id':B},
                    f'state/sources/{json_hash(snapshot)}.json', snapshot, record, None)
                task.update(id=identity, status='not_ready', finished_at=self.campaign()['created_at'])
                task.pop('manual_admission_version')
                self.state.save_task(task)
        self.engine.accept_request(request)
        self.assertEqual({e['reason'] for e in self.ledger()['entries'].values()},
                         {'legacy_latest_task_missing', 'legacy_record_provenance_unproven'})
        self.assertEqual(self.campaign()['tasks'], [])

    def test_prepared_batch_cannot_use_tampered_campaign_or_task_prices(self):
        self.engine.accept_request(queue(self.state, 'manual', languages='deu'))
        with patch.object(self.engine, 'submit'):
            self.engine.prepare()
        batch, = self.state.batches()
        campaign = self.campaign()
        original = copy.deepcopy(campaign)
        campaign['budget_usd'] += 10
        self.state.save_campaign(campaign)
        with self.assertRaisesRegex(ContractError, 'frozen request or campaign changed'):
            self.engine.submit(batch)
        self.state.save_campaign(original)
        task = self.state.tasks()[0]
        task['model'] = 'gpt-6-luna'
        self.state.save_task(task)
        with self.assertRaisesRegex(ContractError, 'frozen provenance'):
            self.engine.submit(batch)
        self.assertEqual(self.provider.upload_calls, 0)
        self.assertEqual(self.provider.create_calls, 0)

    def test_malformed_or_expanded_ledger_fails_closed_without_billing(self):
        request = self.expire()
        original = self.ledger()
        identity = next(iter(original['entries']))
        variants = []
        value = copy.deepcopy(original)
        value['entries'][identity]['events'] = [None]
        variants.append(value)
        value = copy.deepcopy(original)
        value['entries'][identity]['events'] *= 10
        variants.append(value)
        value = copy.deepcopy(original)
        value['entries']['f'*32] = copy.deepcopy(value['entries'][identity])
        variants.append(value)
        value = copy.deepcopy(original)
        value['entries'][identity]['provenance']['task']['source_snapshot'] = None
        value['entries'][identity]['provenance_sha256'] = json_hash(value['entries'][identity]['provenance'])
        variants.append(value)
        for ledger in variants:
            self.state.write(manual_admission.path('manual'), ledger)
            with self.subTest(ledger=variants.index(ledger)), self.assertRaises(ContractError):
                self.restart().accept_request(request)
            with self.assertRaises(ContractError):
                self.engine.prepare()
        self.assertEqual(self.provider.create_calls, 0)
        self.state.write(manual_admission.path('manual'), original)

    def test_cancel_crash_after_terminal_task_write_reconciles_audit_once(self):
        request = self.expire()
        self.restart()
        original = self.engine.state.save_task
        def crash(task):
            original(task)
            raise OSError('fixture crash after task write')
        with patch.object(self.engine.state, 'save_task', side_effect=crash):
            with self.assertRaises(OSError):
                self.engine.accept_request(request)
        self.restart()
        original = self.engine.state.save_task
        with patch.object(self.engine.state, 'save_task', side_effect=crash):
            with self.assertRaises(OSError):
                self.engine.cancel_campaign('manual')
        task, = self.state.tasks()
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual([e['event'] for e in self.state.record(task['language'], task['article_id'])['history']], ['requested'])
        self.restart().cancel_campaign('manual')
        record = self.state.record(task['language'], task['article_id'])
        self.assertEqual([e['event'] for e in record['history']], ['requested', 'cancelled'])
        self.assertEqual(record['history'][-1]['at'], task['finished_at'])
        decision = self.state.read(f'state/tasks/{task["id"]}/decisions/terminal.json')
        self.assertEqual(decision['outcome'], 'cancelled')
        self.engine.cancel_campaign('manual')
        self.assertEqual(self.state.record(task['language'], task['article_id']), record)
        validate_repository(self.config)


if __name__ == '__main__':
    unittest.main()
