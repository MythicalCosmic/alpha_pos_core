from django.db.models import Q
from base.repositories.base import BaseSyncRepository
from base.models import Place


class PlaceRepository(BaseSyncRepository):
    model = Place

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True).order_by('sort_order', 'name')

    @classmethod
    def name_exists(cls, name, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query)
        )
