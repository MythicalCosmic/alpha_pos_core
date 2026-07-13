from django.conf import settings
from django.db import IntegrityError, transaction

from base.repositories.base import BaseSyncRepository
from base.models import CashRegister


class CashRegisterRepository(BaseSyncRepository):
    model = CashRegister

    @classmethod
    def branch_id(cls, branch_id=None):
        value = str(branch_id or getattr(settings, 'BRANCH_ID', '') or '').strip()
        if not value:
            raise ValueError('A branch_id is required for cash-register access')
        return value

    @classmethod
    def get_current(cls, branch_id=None):
        """Return this branch's register; never fall through to another branch."""
        return cls.model.objects.filter(
            branch_id=cls.branch_id(branch_id), is_deleted=False,
        ).first()

    @classmethod
    def get_or_create_current(cls, branch_id=None, *, for_update=False):
        """Return the branch register safely when two requests create it."""
        branch = cls.branch_id(branch_id)
        qs = cls.model.objects.select_for_update() if for_update else cls.model.objects
        register = qs.filter(branch_id=branch, is_deleted=False).first()
        if register:
            return register
        try:
            with transaction.atomic():
                cls.model.objects.create(branch_id=branch, current_balance=0)
        except IntegrityError:
            # The database constraint means another transaction won the race.
            pass
        qs = cls.model.objects.select_for_update() if for_update else cls.model.objects
        return qs.get(branch_id=branch, is_deleted=False)
