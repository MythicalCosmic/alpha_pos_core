from django.db.models import Q
from base.repositories.base import BaseSyncRepository
from stock.models import StockTransfer, StockTransferItem


class StockTransferRepository(BaseSyncRepository):
    model = StockTransfer

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'from_location', 'to_location'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        ).select_related('from_location', 'to_location').order_by('-created_at')

    @classmethod
    def filter_by_location(cls, location_id):
        return cls.model.objects.filter(
            Q(from_location_id=location_id) | Q(to_location_id=location_id),
            is_deleted=False,
        ).select_related('from_location', 'to_location').order_by('-created_at')


class StockTransferItemRepository(BaseSyncRepository):
    model = StockTransferItem

    @classmethod
    def get_for_transfer(cls, transfer_id):
        return cls.model.objects.filter(
            transfer_id=transfer_id, is_deleted=False
        ).select_related('stock_item', 'unit', 'batch')
