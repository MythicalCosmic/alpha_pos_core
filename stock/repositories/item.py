from django.db.models import Q, Sum, Count, F
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import StockItem


class StockItemRepository(BaseSyncRepository):
    model = StockItem

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_for_update(cls, pk):
        # Row-level lock — must be called inside a @transaction.atomic block.
        try:
            return cls.model.objects.select_for_update().get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'category', 'base_unit'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_sku(cls, sku):
        return cls.model.objects.filter(
            sku=sku, is_deleted=False
        ).first()

    @classmethod
    def get_by_barcode(cls, barcode):
        return cls.model.objects.filter(
            barcode=barcode, is_active=True, is_deleted=False
        ).first()

    @classmethod
    def sku_exists(cls, sku, exclude_id=None):
        qs = cls.model.objects.filter(sku=sku, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def barcode_exists(cls, barcode, exclude_id=None):
        qs = cls.model.objects.filter(barcode=barcode, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(barcode__icontains=query)
        )

    @classmethod
    def search_exact(cls, query):
        return cls.model.objects.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(barcode__exact=query),
            is_active=True, is_deleted=False,
        ).select_related('category', 'base_unit')

    @classmethod
    def filter_by_category(cls, category_id):
        return cls.model.objects.filter(
            category_id=category_id, is_deleted=False
        )

    @classmethod
    def get_low_stock(cls):
        return cls.model.objects.filter(
            is_active=True, is_deleted=False
        ).annotate(
            total_qty=Sum('stock_levels__quantity')
        ).filter(
            Q(total_qty__lt=F('reorder_point')) | Q(total_qty__isnull=True)
        )

    @classmethod
    def get_stats(cls):
        qs = cls.model.objects.filter(is_deleted=False)
        return {
            'total': qs.count(),
            'active': qs.filter(is_active=True).count(),
            'by_type': dict(
                qs.filter(is_active=True)
                .values_list('item_type')
                .annotate(count=Count('id'))
            ),
        }

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator

    @classmethod
    def has_transactions(cls, item):
        from stock.models import StockTransaction
        return StockTransaction.objects.filter(
            stock_item=item, is_deleted=False
        ).exists()

    @classmethod
    def has_stock_levels(cls, item):
        return item.stock_levels.filter(is_deleted=False).exists()
