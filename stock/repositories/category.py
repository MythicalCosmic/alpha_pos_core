from django.db.models import Q
from base.repositories.base import BaseSyncRepository
from stock.models import StockCategory


class StockCategoryRepository(BaseSyncRepository):
    model = StockCategory

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_root_categories(cls):
        return cls.model.objects.filter(
            parent__isnull=True, is_deleted=False
        ).order_by('sort_order', 'name')

    @classmethod
    def get_by_type(cls, category_type):
        return cls.model.objects.filter(
            type=category_type, is_deleted=False, is_active=True
        ).order_by('sort_order', 'name')

    @classmethod
    def get_children(cls, category_id):
        return cls.model.objects.filter(
            parent_id=category_id, is_deleted=False
        ).order_by('sort_order', 'name')

    @classmethod
    def name_exists(cls, name, parent_id=None, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, is_deleted=False)
        if parent_id:
            qs = qs.filter(parent_id=parent_id)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def has_active_children(cls, category):
        return category.children.filter(is_active=True, is_deleted=False).exists()

    @classmethod
    def has_active_items(cls, category):
        return category.items.filter(is_active=True, is_deleted=False).exists()

    @classmethod
    def clear_items_category(cls, category):
        from stock.models import StockItem
        return StockItem.objects.filter(category=category).update(category=None)

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(Q(name__icontains=query))

    @classmethod
    def reorder(cls, ordered_ids):
        for index, cat_id in enumerate(ordered_ids):
            cls.model.objects.filter(id=cat_id).update(sort_order=index)