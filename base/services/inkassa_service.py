from django.db import transaction
from django.utils import timezone

from base.repositories import CashRegisterRepository


class InkassaService:
    @staticmethod
    @transaction.atomic
    def add_to_register(amount, branch_id=None):
        register = CashRegisterRepository.get_or_create_current(
            branch_id, for_update=True,
        )
        register.current_balance = (register.current_balance or 0) + amount
        register.last_updated = timezone.now()
        register.save(update_fields=['current_balance', 'last_updated',
                                     'synced_at', 'sync_version'])

