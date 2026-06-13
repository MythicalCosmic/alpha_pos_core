from django.db.models import Q, F
from django.core.paginator import Paginator
from django.utils import timezone
from base.repositories.base import BaseSyncRepository
from discounts.models import Discount


class DiscountRepository(BaseSyncRepository):
    model = Discount

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'discount_type', 'free_product', 'created_by',
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_by_code(cls, code):
        try:
            return cls.model.objects.select_related(
                'discount_type', 'free_product',
            ).get(code=code, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def code_exists(cls, code, exclude_id=None):
        qs = cls.model.objects.filter(code=code, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def filter_by_type(cls, discount_type_id):
        return cls.model.objects.filter(
            is_deleted=False, discount_type_id=discount_type_id,
        )

    @classmethod
    def filter_active_and_valid(cls):
        now = timezone.now()
        qs = cls.model.objects.filter(is_deleted=False, is_active=True)
        qs = qs.filter(Q(start_date__isnull=True) | Q(start_date__lte=now))
        qs = qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=now))
        qs = qs.filter(Q(usage_limit__isnull=True) | Q(usage_count__lt=F('usage_limit')))
        return qs

    @classmethod
    def get_for_products(cls, product_ids):
        return cls.model.objects.filter(
            is_deleted=False,
            is_active=True,
            applies_to=Discount.AppliesTo.SPECIFIC_PRODUCTS,
            target_product_ids__overlap=product_ids,
        )

    @classmethod
    def get_for_categories(cls, category_ids):
        return cls.model.objects.filter(
            is_deleted=False,
            is_active=True,
            applies_to=Discount.AppliesTo.SPECIFIC_CATEGORIES,
            target_category_ids__overlap=category_ids,
        )

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) | Q(code__icontains=query)
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
