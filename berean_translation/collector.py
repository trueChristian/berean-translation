"""Bounded in-run pickup for already-authorized Batch work.

The runner remains the single state writer. This grants no new spending scope,
does not replace the provider's Batch completion window or promise a cron deadline.
"""
from __future__ import annotations
import time
from .common import ContractError

MAX_WAIT_SECONDS = 900
MIN_POLL_SECONDS = 30
MAX_POLL_SECONDS = 300


def collect_window(engine, *, wait_seconds=0, poll_seconds=60,
                   monotonic=time.monotonic, sleep=time.sleep):
    """Run once, then poll while submitted work remains and time permits.

    The wait budget includes the initial tick's elapsed time. A positive budget
    also stops that tick from starting another unit of work at a resumable
    boundary. An in-flight unit finishes its durable checkpoints; it is never
    interrupted here. The caller must reserve additional workflow time for
    I/O, final checkpoints and validation. Zero retains a single unrestricted tick.
    Unknown submissions and upload failures use the existing safe recovery path
    on the next collector run; they do not keep this runner alive by themselves.
    """
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
        raise ContractError(f'Collector wait must be 0..{MAX_WAIT_SECONDS} seconds')
    if type(poll_seconds) is not int or not MIN_POLL_SECONDS <= poll_seconds <= MAX_POLL_SECONDS:
        raise ContractError(f'Collector poll interval must be {MIN_POLL_SECONDS}..{MAX_POLL_SECONDS} seconds')
    deadline = monotonic() + wait_seconds
    continue_work = (lambda: monotonic() < deadline) if wait_seconds else None
    if continue_work is None:
        engine.tick()
    else:
        engine.tick(continue_work=continue_work)
    ticks = 1
    while True:
        pending = sum(batch['status'] in ('submitted', 'cancelling')
                      for batch in engine.state.batches())
        if not engine.provider:
            reason = 'provider_unavailable'
            break
        if continue_work is not None and not continue_work():
            reason = 'wait_budget_exhausted'
            break
        if not pending:
            reason = 'no_submitted_batches'
            break
        # Do not sleep past the deadline or start a tick at the deadline.
        if monotonic() + poll_seconds >= deadline:
            reason = 'wait_budget_exhausted'
            break
        sleep(poll_seconds)
        if monotonic() >= deadline:
            reason = 'wait_budget_exhausted'
            break
        # Reuse the coherent source snapshot read once at the start of this run.
        engine.tick(discover_source=False, continue_work=continue_work)
        ticks += 1
    return {'ticks': ticks, 'stop_reason': reason, 'submitted_batches': pending,
            'wait_budget_seconds': wait_seconds, 'poll_interval_seconds': poll_seconds}
