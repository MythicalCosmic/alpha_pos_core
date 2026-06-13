from base.repositories.base import BaseSyncRepository
from base.models import CashRegister


class CashRegisterRepository(BaseSyncRepository):
    model = CashRegister

    @classmethod
    def get_current(cls):
        return cls.model.objects.filter(is_deleted=False).first()
