from base.repositories.base import BaseSyncRepository
from discounts.models import OrderDiscount


class OrderDiscountRepository(BaseSyncRepository):
    model = OrderDiscount

    @classmethod
    def get_for_order(cls, order_id):
        return cls.model.objects.filter(
            is_deleted=False, order_id=order_id,
        ).select_related('discount')

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'discount', 'discount__discount_type', 'order', 'applied_by',
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None
