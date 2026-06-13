from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import StockCount, StockCountItem, VarianceReasonCode


class VarianceReasonCodeRepository(BaseSyncRepository):
    model = VarianceReasonCode

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_by_code(cls, code):
        return cls.model.objects.filter(
            code=code, is_deleted=False
        ).first()

    @classmethod
    def code_exists(cls, code, exclude_id=None):
        qs = cls.model.objects.filter(code=code, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()


class StockCountRepository(BaseSyncRepository):
    model = StockCount

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'location', 'category_filter'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        ).select_related('location').order_by('-created_at')

    @classmethod
    def filter_by_location(cls, location_id):
        return cls.model.objects.filter(
            location_id=location_id, is_deleted=False
        ).order_by('-created_at')

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class StockCountItemRepository(BaseSyncRepository):
    model = StockCountItem

    @classmethod
    def get_for_count(cls, stock_count_id):
        return cls.model.objects.filter(
            stock_count_id=stock_count_id, is_deleted=False
        ).select_related('stock_item', 'batch')

    @classmethod
    def get_uncounted(cls, stock_count_id):
        return cls.model.objects.filter(
            stock_count_id=stock_count_id,
            counted_quantity__isnull=True,
            is_deleted=False,
        ).select_related('stock_item')

    @classmethod
    def get_with_variance(cls, stock_count_id):
        return cls.model.objects.filter(
            stock_count_id=stock_count_id,
            is_deleted=False,
        ).exclude(
            variance=0
        ).exclude(
            variance__isnull=True
        ).select_related('stock_item', 'batch', 'reason_code')
