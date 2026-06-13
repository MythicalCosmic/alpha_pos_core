from base.repositories.base import BaseSyncRepository
from discounts.models import DiscountUsage


class DiscountUsageRepository(BaseSyncRepository):
    model = DiscountUsage

    @classmethod
    def count_for_discount(cls, discount_id):
        return cls.model.objects.filter(
            is_deleted=False, discount_id=discount_id,
        ).count()

    @classmethod
    def count_for_user_discount(cls, user_id, discount_id):
        return cls.model.objects.filter(
            is_deleted=False, user_id=user_id, discount_id=discount_id,
        ).count()

    @classmethod
    def get_for_order(cls, order_id):
        return cls.model.objects.filter(
            is_deleted=False, order_id=order_id,
        )
