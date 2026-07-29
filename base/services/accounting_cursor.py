"""Serialize branch accounting events against the Inkassa cutoff.

The shared ``CashRegister`` row lock orders each event before or after the
cutoff. ``accounting_recorded_at`` therefore assigns late offline events to the
next Inkassa without changing their economic timestamps.
"""
from django.db import transaction

from base.repositories import CashRegisterRepository


def lock_branch_accounting(branch_id=None):
    """Lock and return the branch register for the caller's transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('branch accounting lock requires an atomic transaction')
    return CashRegisterRepository.get_or_create_current(
        branch_id, for_update=True,
    )
