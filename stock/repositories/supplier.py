from django.db.models import Q, Count
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import Supplier, SupplierStockItem


class SupplierRepository(BaseSyncRepository):
    model = Supplier

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

    @classmethod
    def get_next_code_seq(cls, prefix):
        last = cls.model.objects.filter(
            code__startswith=prefix, is_deleted=False
        ).order_by('-code').first()
        if last and last.code:
            try:
                return int(last.code.replace(prefix, '')) + 1
            except (ValueError, IndexError):
                pass
        return 1

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) |
            Q(code__icontains=query) |
            Q(contact_person__icontains=query) |
            Q(email__icontains=query)
        )

    @classmethod
    def with_item_count(cls, queryset):
        return queryset.annotate(item_count=Count('stock_items'))

    @classmethod
    def has_pending_orders(cls, supplier):
        return supplier.purchase_orders.filter(
            status__in=['DRAFT', 'SENT', 'CONFIRMED', 'PARTIAL'],
            is_deleted=False,
        ).exists()

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class SupplierStockItemRepository(BaseSyncRepository):
    model = SupplierStockItem

    @classmethod
    def get_for_supplier(cls, supplier_id):
        return cls.model.objects.filter(
            supplier_id=supplier_id, is_deleted=False
        ).select_related('stock_item', 'unit')

    @classmethod
    def get_for_item(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).select_related('supplier', 'unit')

    @classmethod
    def get_preferred(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id,
            is_preferred=True,
            is_deleted=False,
        ).select_related('supplier', 'unit').first()

    @classmethod
    def get_cheapest(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).select_related('supplier', 'unit').order_by('price').first()

    @classmethod
    def link_exists(cls, supplier_id, stock_item_id):
        return cls.model.objects.filter(
            supplier_id=supplier_id,
            stock_item_id=stock_item_id,
            is_deleted=False,
        ).exists()

    @classmethod
    def clear_preferred(cls, stock_item_id, exclude_id=None):
        qs = cls.model.objects.filter(
            stock_item_id=stock_item_id, is_preferred=True
        )
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return cls.sync_update_queryset(qs, is_preferred=False)
