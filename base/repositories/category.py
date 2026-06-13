from django.db.models import Q
from django.core.cache import cache
from django.core.paginator import Paginator
from django.utils.text import slugify
from base.repositories.base import BaseSyncRepository
from base.models import Category


CACHE_PREFIX = 'category'
CACHE_TTL = 300


class CategoryCache:
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


class CategoryRepository(BaseSyncRepository):
    model = Category

    @classmethod
    def get_by_slug(cls, slug):
        cached = CategoryCache.get('slug', slug)
        if cached is not None:
            return cached
        try:
            obj = cls.model.objects.get(slug=slug, is_deleted=False)
            CategoryCache.set(obj, 'slug', slug)
            return obj
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_id_cached(cls, pk):
        cached = CategoryCache.get('id', pk)
        if cached is not None:
            return cached
        try:
            obj = cls.model.objects.get(pk=pk, is_deleted=False)
            CategoryCache.set(obj, 'id', pk)
            return obj
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_id_include_deleted(cls, pk):
        try:
            return cls.model.objects.get(pk=pk)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_active(cls):
        cached = CategoryCache.get('active')
        if cached is not None:
            return cached
        qs = list(
            cls.model.objects.filter(
                is_deleted=False, status='ACTIVE'
            ).order_by('sort_order').values(
                'id', 'name', 'slug', 'description', 'colors', 'sort_order'
            )
        )
        CategoryCache.set(qs, 'active')
        return qs

    @classmethod
    def get_ordered(cls):
        return cls.model.objects.filter(is_deleted=False).order_by('sort_order')

    @classmethod
    def search(cls, queryset, search_term):
        return queryset.filter(
            Q(name__icontains=search_term) |
            Q(description__icontains=search_term) |
            Q(slug__icontains=search_term)
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator

    @classmethod
    def name_exists(cls, name, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def slug_exists(cls, slug, exclude_id=None):
        qs = cls.model.objects.filter(slug=slug, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def generate_unique_slug(cls, name, exclude_id=None):
        base_slug = slugify(name, allow_unicode=True) or 'category'
        slug = base_slug
        counter = 1
        while cls.slug_exists(slug, exclude_id):
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    @classmethod
    def get_stats(cls):
        cached = CategoryCache.get('stats')
        if cached is not None:
            return cached
        stats = {
            'total_categories': cls.model.objects.filter(is_deleted=False).count(),
            'active_categories': cls.model.objects.filter(status='ACTIVE', is_deleted=False).count(),
            'inactive_categories': cls.model.objects.filter(status='INACTIVE', is_deleted=False).count(),
            'deleted_categories': cls.model.objects.filter(is_deleted=True).count(),
        }
        CategoryCache.set(stats, 'stats')
        return stats

    @classmethod
    def get_deleted(cls):
        return cls.model.objects.filter(is_deleted=True).order_by('-updated_at')

    @classmethod
    def bulk_soft_delete(cls, ids):
        return cls.model.objects.filter(id__in=ids, is_deleted=False).update(is_deleted=True)

    @classmethod
    def get_root_categories(cls):
        return cls.model.objects.filter(
            parent__isnull=True, is_deleted=False
        ).order_by('sort_order')

    @classmethod
    def get_children(cls, parent_id):
        return cls.model.objects.filter(
            parent_id=parent_id, is_deleted=False
        ).order_by('sort_order')

    @classmethod
    def invalidate_cache(cls):
        CategoryCache.invalidate()
