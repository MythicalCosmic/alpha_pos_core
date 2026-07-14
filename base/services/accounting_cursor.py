"""Serialization primitive for branch accounting event cursors.

``CashRegister`` is the one durable row owned by every operational branch.
Taking its row lock before stamping an accounting event, and taking the same
lock before choosing an Inkassa cutoff, gives those operations one total order:

* a writer that wins the lock commits an event before the cutoff; or
* Inkassa wins the lock and the writer receives a cursor at/after that cutoff.

Economic timestamps (``paid_at`` / ``refunded_at``) remain untouched. The lock
protects the database-local ``accounting_recorded_at`` cursor instead, so an
offline event that arrives late is rolled into the next Inkassa exactly once
rather than disappearing behind an already-advanced business-time boundary.
"""
from django.db import transaction

from base.repositories import CashRegisterRepository


def lock_branch_accounting(branch_id=None):
    """Return the branch register under ``SELECT ... FOR UPDATE``.

    Callers must be inside a transaction so the lock remains held through the
    event write or cutoff snapshot. Failing loudly prevents a future caller
    from accidentally reducing this invariant to a no-op lock.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('branch accounting lock requires an atomic transaction')
    return CashRegisterRepository.get_or_create_current(
        branch_id, for_update=True,
    )
