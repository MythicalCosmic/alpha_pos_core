from django.db import transaction

from base.repositories import CashRegisterRepository


def lock_branch_accounting(branch_id=None):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('branch accounting lock requires an atomic transaction')
    return CashRegisterRepository.get_or_create_current(
        branch_id, for_update=True,
    )
