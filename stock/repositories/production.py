from django.db.models import Q
from base.repositories.base import BaseSyncRepository
from stock.models import (
    ProductionOrder, ProductionOrderIngredient,
    ProductionOrderOutput, ProductionOrderStep,
)


class ProductionOrderRepository(BaseSyncRepository):
    model = ProductionOrder

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'recipe', 'output_unit', 'source_location', 'output_location'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        ).select_related('recipe', 'output_unit').order_by('-created_at')

    @classmethod
    def filter_by_location(cls, location_id):
        return cls.model.objects.filter(
            Q(source_location_id=location_id) | Q(output_location_id=location_id),
            is_deleted=False,
        ).order_by('-created_at')


class ProductionOrderIngredientRepository(BaseSyncRepository):
    model = ProductionOrderIngredient

    @classmethod
    def get_for_order(cls, production_order_id):
        return cls.model.objects.filter(
            production_order_id=production_order_id, is_deleted=False
        ).select_related('stock_item', 'unit')


class ProductionOrderOutputRepository(BaseSyncRepository):
    model = ProductionOrderOutput

    @classmethod
    def get_for_order(cls, production_order_id):
        return cls.model.objects.filter(
            production_order_id=production_order_id, is_deleted=False
        ).select_related('stock_item', 'unit')


class ProductionOrderStepRepository(BaseSyncRepository):
    model = ProductionOrderStep

    @classmethod
    def get_for_order(cls, production_order_id):
        return cls.model.objects.filter(
            production_order_id=production_order_id, is_deleted=False
        ).select_related('recipe_step').order_by('recipe_step__step_number')
