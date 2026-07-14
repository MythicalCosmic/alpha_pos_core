from django.db.models import Q, Sum, F, DecimalField
from base.repositories.base import BaseSyncRepository
from stock.models import StockLocation


class StockLocationRepository(BaseSyncRepository):
    model = StockLocation

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_default(cls):
        return cls.model.objects.filter(
            is_default=True, is_active=True, is_deleted=False
        ).first()

    @classmethod
    def get_root_locations(cls):
        return cls.model.objects.filter(
            parent_location__isnull=True, is_deleted=False
        )

    @classmethod
    def get_production_areas(cls):
        return cls.model.objects.filter(
            is_production_area=True, is_active=True, is_deleted=False
        )

    @classmethod
    def get_children(cls, location_id):
        return cls.model.objects.filter(
            parent_location_id=location_id, is_deleted=False
        )

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) | Q(type__icontains=query)
        )

    @classmethod
    def name_exists(cls, name, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def clear_default(cls, exclude_id=None):
        qs = cls.model.objects.filter(is_default=True)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return cls.sync_update_queryset(qs, is_default=False)

    @classmethod
    def deactivate_children(cls, location):
        return cls.sync_update_queryset(
            location.children.filter(is_active=True), is_active=False,
        )

    @classmethod
    def has_stock(cls, location):
        from stock.models import StockLevel
        return StockLevel.objects.filter(
            location=location, is_deleted=False
        ).exists()

    @classmethod
    def reorder(cls, ordered_ids):
        for index, loc_id in enumerate(ordered_ids):
            cls.sync_update_queryset(
                cls.model.objects.filter(id=loc_id), sort_order=index,
            )

    @classmethod
    def get_stock_stats(cls, location):
        from stock.models import StockLevel
        return StockLevel.objects.filter(
            location=location, is_deleted=False
        ).aggregate(
            total_qty=Sum('quantity'),
            # Inventory value is quantity × moving-average unit cost. The old
            # implementation returned quantity twice under two different keys,
            # making a location with 10kg @ 50,000 UZS appear worth 10 UZS.
            total_value=Sum(
                F('quantity') * F('stock_item__avg_cost_price'),
                output_field=DecimalField(max_digits=24, decimal_places=4),
            ),
        )
