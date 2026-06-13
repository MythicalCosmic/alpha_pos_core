from django.db.models import Sum, F
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import StockLevel, StockTransaction


class StockLevelRepository(BaseSyncRepository):
    model = StockLevel

    @classmethod
    def get_for_item(cls, stock_item_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).select_related('stock_item', 'location')

    @classmethod
    def get_for_location(cls, location_id):
        return cls.model.objects.filter(
            location_id=location_id, is_deleted=False
        ).select_related('stock_item', 'location')

    @classmethod
    def get_for_item_and_location(cls, stock_item_id, location_id):
        return cls.model.objects.filter(
            stock_item_id=stock_item_id,
            location_id=location_id,
            is_deleted=False,
        ).first()

    @classmethod
    def get_or_create_level(cls, stock_item_id, location_id):
        obj, created = cls.model.objects.get_or_create(
            stock_item_id=stock_item_id,
            location_id=location_id,
            defaults={'quantity': 0, 'reserved_quantity': 0},
        )
        return obj

    @classmethod
    def get_or_create_level_for_update(cls, stock_item_id, location_id):
        # Acquire a row-level lock for the duration of the surrounding
        # transaction so concurrent mutations of the same level serialize.
        # Must be called inside a @transaction.atomic block.
        try:
            return cls.model.objects.select_for_update().get(
                stock_item_id=stock_item_id,
                location_id=location_id,
            )
        except cls.model.DoesNotExist:
            cls.model.objects.get_or_create(
                stock_item_id=stock_item_id,
                location_id=location_id,
                defaults={'quantity': 0, 'reserved_quantity': 0},
            )
            return cls.model.objects.select_for_update().get(
                stock_item_id=stock_item_id,
                location_id=location_id,
            )

    @classmethod
    def get_total_quantity(cls, stock_item_id):
        result = cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).aggregate(total=Sum('quantity'))
        return result['total'] or 0

    @classmethod
    def get_low_stock(cls):
        return cls.model.objects.filter(
            is_deleted=False,
            stock_item__is_active=True,
        ).select_related(
            'stock_item', 'location'
        ).filter(
            quantity__lt=F('stock_item__reorder_point')
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class StockTransactionRepository(BaseSyncRepository):
    model = StockTransaction

    @classmethod
    def get_for_item(cls, stock_item_id, days=None):
        qs = cls.model.objects.filter(
            stock_item_id=stock_item_id, is_deleted=False
        ).select_related('stock_item', 'location', 'unit')
        if days:
            from django.utils import timezone
            from datetime import timedelta
            cutoff = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=cutoff)
        return qs.order_by('-created_at')

    @classmethod
    def get_for_location(cls, location_id):
        return cls.model.objects.filter(
            location_id=location_id, is_deleted=False
        ).select_related('stock_item', 'location', 'unit').order_by('-created_at')

    @classmethod
    def filter_by_type(cls, movement_type):
        return cls.model.objects.filter(
            movement_type=movement_type, is_deleted=False
        ).order_by('-created_at')

    @classmethod
    def get_by_reference(cls, reference_type, reference_id):
        return cls.model.objects.filter(
            reference_type=reference_type,
            reference_id=reference_id,
            is_deleted=False,
        ).order_by('-created_at')

    @classmethod
    def exists_for_order(cls, order_id, movement_type='SALE_OUT'):
        return cls.model.objects.filter(
            order_id=order_id,
            movement_type=movement_type,
            is_deleted=False,
        ).exists()

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
