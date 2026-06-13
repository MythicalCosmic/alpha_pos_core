from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from base.models import Table


class TableRepository(BaseSyncRepository):
    model = Table

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True).order_by('place', 'sort_order', 'number')

    @classmethod
    def get_for_place(cls, place_id):
        return cls.model.objects.filter(
            place_id=place_id, is_deleted=False
        ).order_by('sort_order', 'number')

    @classmethod
    def get_available(cls, place_id):
        return cls.model.objects.filter(
            place_id=place_id, is_deleted=False, is_active=True, status='AVAILABLE'
        ).order_by('sort_order', 'number')

    @classmethod
    def number_exists(cls, place_id, number, exclude_id=None):
        qs = cls.model.objects.filter(
            place_id=place_id, number=number, is_deleted=False
        )
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def update_status(cls, table_id, status):
        table = cls.get_by_id(table_id)
        if not table:
            return None
        table.status = status
        table.save(update_fields=['status'])
        return table

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
