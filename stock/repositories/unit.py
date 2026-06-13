from django.db.models import Q
from base.repositories.base import BaseSyncRepository
from stock.models import StockUnit, StockItemUnit


class StockUnitRepository(BaseSyncRepository):
    model = StockUnit

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_by_type(cls, unit_type):
        return cls.model.objects.filter(
            unit_type=unit_type, is_deleted=False, is_active=True
        ).order_by('name')

    @classmethod
    def get_base_units(cls, unit_type=None):
        qs = cls.model.objects.filter(
            is_base_unit=True, is_deleted=False, is_active=True
        )
        if unit_type:
            qs = qs.filter(unit_type=unit_type)
        return qs

    @classmethod
    def short_name_exists(cls, short_name, exclude_id=None):
        qs = cls.model.objects.filter(short_name__iexact=short_name, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def has_derived_units(cls, unit):
        return unit.derived_units.filter(is_deleted=False).exists()

    @classmethod
    def has_stock_items(cls, unit):
        from stock.models import StockItem
        return StockItem.objects.filter(base_unit=unit, is_deleted=False).exists()

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) | Q(short_name__icontains=query)
        )


class StockItemUnitRepository(BaseSyncRepository):
    model = StockItemUnit

    @classmethod
    def get_for_item(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).select_related('unit')

    @classmethod
    def unit_exists_for_item(cls, stock_item_id, unit_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, unit_id=unit_id, is_deleted=False
        ).exists()

    @classmethod
    def clear_default(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, is_default=True
        ).update(is_default=False)

    @classmethod
    def get_by_item_and_unit(cls, stock_item_id, unit_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, unit_id=unit_id, is_deleted=False
        ).first()
