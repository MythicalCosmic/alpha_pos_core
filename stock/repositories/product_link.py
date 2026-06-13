from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import ProductStockLink, ProductComponentStock


class ProductStockLinkRepository(BaseSyncRepository):
    model = ProductStockLink

    @classmethod
    def get_for_product(cls, product_id):
        return cls.model.objects.filter(
            product_id=product_id, is_deleted=False
        ).select_related('recipe', 'stock_item', 'unit').first()

    @classmethod
    def get_active_for_product(cls, product_id):
        return cls.model.objects.filter(
            product_id=product_id, is_active=True, is_deleted=False
        ).select_related('recipe', 'stock_item', 'unit').first()

    @classmethod
    def product_has_link(cls, product_id):
        return cls.model.objects.filter(
            product_id=product_id, is_deleted=False
        ).exists()

    @classmethod
    def get_with_components(cls, link_id):
        try:
            return cls.model.objects.select_related(
                'recipe', 'stock_item', 'unit'
            ).prefetch_related(
                'components__stock_item', 'components__unit'
            ).get(pk=link_id, is_deleted=False)
        except cls.model.DoesNotExist:
            return None


    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class ProductComponentStockRepository(BaseSyncRepository):
    model = ProductComponentStock

    @classmethod
    def get_for_link(cls, link_id):
        return cls.model.objects.filter(
            product_stock_link_id=link_id, is_deleted=False
        ).select_related('stock_item', 'unit')

    @classmethod
    def get_defaults(cls, link_id):
        return cls.model.objects.filter(
            product_stock_link_id=link_id,
            is_default=True,
            is_deleted=False,
        ).select_related('stock_item', 'unit')
