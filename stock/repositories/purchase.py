from django.db.models import Sum
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceiving, PurchaseReceivingItem


class PurchaseOrderRepository(BaseSyncRepository):
    model = PurchaseOrder

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'supplier', 'delivery_location'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_supplier(cls, supplier_id):
        return cls.model.objects.filter(
            supplier_id=supplier_id, is_deleted=False
        ).order_by('-order_date')

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        ).order_by('-order_date')

    @classmethod
    def has_received_items(cls, po):
        return po.receivings.filter(
            status='COMPLETED', is_deleted=False
        ).exists()

    @classmethod
    def get_stats(cls, status=None, date_range=None):
        qs = cls.model.objects.filter(is_deleted=False)
        if status:
            qs = qs.filter(status=status)
        if date_range:
            start, end = date_range
            qs = qs.filter(order_date__range=(start, end))
        return qs.aggregate(
            total_value=Sum('total'),
            count=Sum('id'),
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class PurchaseOrderItemRepository(BaseSyncRepository):
    model = PurchaseOrderItem

    @classmethod
    def get_for_order(cls, po_id):
        return cls.model.objects.filter(
            purchase_order_id=po_id, is_deleted=False
        ).select_related('stock_item', 'unit')

    @classmethod
    def get_pending_quantity(cls, po_item):
        return po_item.quantity_ordered - po_item.quantity_received


class PurchaseReceivingRepository(BaseSyncRepository):
    model = PurchaseReceiving

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'purchase_order', 'location'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_for_update(cls, pk):
        # Row-level lock — must be called inside a @transaction.atomic block.
        try:
            return cls.model.objects.select_for_update().select_related(
                'purchase_order', 'location'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_for_order(cls, po_id):
        return cls.model.objects.filter(
            purchase_order_id=po_id, is_deleted=False
        ).order_by('-created_at')


class PurchaseReceivingItemRepository(BaseSyncRepository):
    model = PurchaseReceivingItem

    @classmethod
    def get_for_receiving(cls, receiving_id):
        return cls.model.objects.filter(
            receiving_id=receiving_id, is_deleted=False
        ).select_related('stock_item', 'unit', 'po_item')
