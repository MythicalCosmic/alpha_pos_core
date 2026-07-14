from django.db.models import Q, Count
from django.core.cache import cache
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from base.models import Product


CACHE_PREFIX = 'product'
CACHE_TTL = 300


class ProductCache:
    @classmethod
    def _version(cls):
        v = cache.get(f'{CACHE_PREFIX}:v')
        if v is None:
            cache.set(f'{CACHE_PREFIX}:v', 1, None)
            return 1
        return v

    @classmethod
    def key(cls, *parts):
        v = cls._version()
        return f'{CACHE_PREFIX}:v{v}:{":".join(str(p) for p in parts)}'

    @classmethod
    def invalidate(cls):
        try:
            cache.incr(f'{CACHE_PREFIX}:v')
        except ValueError:
            cache.set(f'{CACHE_PREFIX}:v', 1, None)

    @classmethod
    def get(cls, *parts):
        return cache.get(cls.key(*parts))

    @classmethod
    def set(cls, value, *parts):
        cache.set(cls.key(*parts), value, CACHE_TTL)


class ProductRepository(BaseSyncRepository):
    model = Product

    @classmethod
    def get_by_id_cached(cls, pk):
        cached = ProductCache.get('id', pk)
        if cached is not None:
            return cached
        try:
            obj = cls.model.objects.select_related('category').get(pk=pk, is_deleted=False)
            ProductCache.set(obj, 'id', pk)
            return obj
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_id_include_deleted(cls, pk):
        try:
            return cls.model.objects.select_related('category').get(pk=pk)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_category(cls, category):
        return cls.model.objects.filter(is_deleted=False, category=category)

    @classmethod
    def get_by_category_id(cls, category_id):
        return cls.model.objects.filter(is_deleted=False, category_id=category_id)

    @classmethod
    def get_by_category_ids(cls, category_ids):
        return cls.model.objects.filter(is_deleted=False, category_id__in=category_ids)

    @classmethod
    def search(cls, queryset, search_term):
        return queryset.filter(
            Q(name__icontains=search_term) |
            Q(description__icontains=search_term)
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator

    @classmethod
    def name_exists(cls, name, category_id, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, category_id=category_id, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def get_stats(cls):
        cached = ProductCache.get('stats')
        if cached is not None:
            return cached
        total = cls.model.objects.filter(is_deleted=False).count()
        deleted = cls.model.objects.filter(is_deleted=True).count()
        by_category = list(
            cls.model.objects.filter(is_deleted=False)
            .values('category__name', 'category_id')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        stats = {
            'total_products': total,
            'deleted_products': deleted,
            'by_category': [
                {
                    'category_id': item['category_id'],
                    'category_name': item['category__name'],
                    'count': item['count'],
                }
                for item in by_category
            ],
        }
        ProductCache.set(stats, 'stats')
        return stats

    @classmethod
    def get_deleted(cls):
        return cls.model.objects.filter(is_deleted=True).select_related('category').order_by('-updated_at')

    @classmethod
    def bulk_soft_delete(cls, ids):
        return cls.sync_update_queryset(
            cls.model.objects.filter(id__in=ids, is_deleted=False),
            is_deleted=True,
        )

    @classmethod
    def invalidate_cache(cls):
        ProductCache.invalidate()
