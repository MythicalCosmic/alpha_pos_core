from django.db import transaction
from django.utils import timezone

from base.models import CashRegister
from base.repositories import CashRegisterRepository


class InkassaService:
    @staticmethod
    @transaction.atomic
    def add_to_register(amount):
        register = CashRegisterRepository.get_current()
        if not register:
            # No active register yet — create one so the cash is never silently
            # dropped (the old `return` here lost the entry entirely). Mirrors
            # the canonical get_or_create in admins.services.inkassa_service.
            register, _ = CashRegister.objects.get_or_create(
                is_deleted=False, defaults={'current_balance': 0}
            )
        # Row-lock the register and increment under the lock to avoid lost
        # updates under concurrent payments. We go through save() (rather than
        # a bare .update(F(...))) so SyncMixin resets synced_at and enqueues
        # the new balance — a .update() bypasses save() entirely, so the
        # running balance would never propagate to the cloud / other branches.
        locked = CashRegister.objects.select_for_update().get(pk=register.pk)
        locked.current_balance = (locked.current_balance or 0) + amount
        locked.last_updated = timezone.now()
        locked.save(update_fields=['current_balance', 'last_updated',
                                   'synced_at', 'sync_version'])

