from django.core.paginator import Paginator
from django.utils import timezone
from datetime import timedelta
from base.repositories.base import BaseSyncRepository
from stock.models import StockBatch


class StockBatchRepository(BaseSyncRepository):
    model = StockBatch

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'stock_item', 'location', 'supplier'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_for_update(cls, pk):
        # Row-level lock — must be called inside a @transaction.atomic block.
        try:
            return cls.model.objects.select_for_update().get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_for_item(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).select_related('stock_item', 'location').order_by('-created_at')

    @classmethod
    def get_for_location(cls, location_id):
        return cls.model.objects.filter(
            location_id=location_id, is_deleted=False
        ).select_related('stock_item', 'location').order_by('-created_at')

    @classmethod
    def get_available(cls, stock_item_id, location_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id,
            location_id=location_id,
            current_quantity__gt=0,
            status='AVAILABLE',
            is_deleted=False,
        ).select_related('stock_item', 'location')

    @classmethod
    def get_available_fifo(cls, stock_item_id, location_id):
        return cls.get_available(stock_item_id, location_id).order_by('created_at')

    @classmethod
    def get_available_lifo(cls, stock_item_id, location_id):
        return cls.get_available(stock_item_id, location_id).order_by('-created_at')

    @classmethod
    def get_available_fefo(cls, stock_item_id, location_id):
        return cls.get_available(stock_item_id, location_id).order_by('expiry_date')

    @classmethod
    def get_by_batch_number(cls, batch_number, stock_item_id=None):
        qs = cls.model.objects.filter(
            batch_number=batch_number, is_deleted=False
        )
        if stock_item_id:
            qs = qs.filter(stock_item_id=stock_item_id)
        return qs.first()

    @classmethod
    def batch_number_exists(cls, batch_number, stock_item_id):
        return cls.model.objects.filter(
            batch_number=batch_number,
            stock_item_id=stock_item_id,
            is_deleted=False,
        ).exists()

    @classmethod
    def get_expiring(cls, days=7):
        today = timezone.localdate()
        threshold = today + timedelta(days=days)
        return cls.model.objects.filter(
            expiry_date__lte=threshold,
            expiry_date__gt=today,
            current_quantity__gt=0,
            status='AVAILABLE',
            is_deleted=False,
        ).select_related('stock_item', 'location').order_by('expiry_date')

    @classmethod
    def get_expired(cls):
        return cls.model.objects.filter(
            expiry_date__lt=timezone.localdate(),
            current_quantity__gt=0,
            is_deleted=False,
        ).select_related('stock_item', 'location').order_by('expiry_date')

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
