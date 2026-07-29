from base.repositories.base import BaseSyncRepository
from discounts.models import DiscountType


class DiscountTypeRepository(BaseSyncRepository):
    model = DiscountType

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_by_code(cls, code):
        try:
            return cls.model.objects.get(code=code, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def code_exists(cls, code, exclude_id=None):
        qs = cls.model.objects.filter(code=code, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()
