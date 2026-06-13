from django.db.models import Q
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from stock.models import (
    Recipe, RecipeIngredient, RecipeIngredientSubstitute,
    RecipeByProduct, RecipeStep,
)


class RecipeRepository(BaseSyncRepository):
    model = Recipe

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(
            is_deleted=False, is_active=True, is_active_version=True
        )

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'output_item', 'output_unit', 'production_location'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_code(cls, code):
        return cls.model.objects.filter(
            code=code, is_deleted=False
        ).first()

    @classmethod
    def code_exists(cls, code, exclude_id=None):
        qs = cls.model.objects.filter(code=code, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def get_versions(cls, recipe):
        root = recipe.parent_recipe or recipe
        return cls.model.objects.filter(
            Q(id=root.id) | Q(parent_recipe=root),
            is_deleted=False,
        ).order_by('-version')

    @classmethod
    def deactivate_other_versions(cls, recipe):
        root = recipe.parent_recipe or recipe
        return cls.model.objects.filter(
            Q(id=root.id) | Q(parent_recipe=root),
            is_deleted=False,
        ).exclude(id=recipe.id).update(is_active_version=False)

    @classmethod
    def get_next_code_seq(cls, prefix):
        last = cls.model.objects.filter(
            code__startswith=prefix, is_deleted=False
        ).order_by('-code').first()
        if last and last.code:
            try:
                return int(last.code.split('-')[-1]) + 1
            except (ValueError, IndexError):
                pass
        return 1

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) |
            Q(code__icontains=query) |
            Q(output_item__name__icontains=query)
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class RecipeIngredientRepository(BaseSyncRepository):
    model = RecipeIngredient

    @classmethod
    def get_for_recipe(cls, recipe_id):
        return cls.model.objects.filter(
            recipe_id=recipe_id, is_deleted=False
        ).select_related('stock_item', 'unit').order_by('sort_order')

    @classmethod
    def reorder(cls, recipe_id, ordered_ids):
        for idx, ing_id in enumerate(ordered_ids):
            cls.model.objects.filter(
                id=ing_id, recipe_id=recipe_id
            ).update(sort_order=idx)


class RecipeIngredientSubstituteRepository(BaseSyncRepository):
    model = RecipeIngredientSubstitute

    @classmethod
    def get_for_ingredient(cls, ingredient_id):
        return cls.model.objects.filter(
            recipe_ingredient_id=ingredient_id, is_deleted=False
        ).select_related('substitute_item', 'unit').order_by('priority')


class RecipeByProductRepository(BaseSyncRepository):
    model = RecipeByProduct

    @classmethod
    def get_for_recipe(cls, recipe_id):
        return cls.model.objects.filter(
            recipe_id=recipe_id, is_deleted=False
        ).select_related('stock_item', 'unit')


class RecipeStepRepository(BaseSyncRepository):
    model = RecipeStep

    @classmethod
    def get_for_recipe(cls, recipe_id):
        return cls.model.objects.filter(
            recipe_id=recipe_id, is_deleted=False
        ).order_by('step_number')

    @classmethod
    def get_next_step_number(cls, recipe_id):
        last = cls.model.objects.filter(
            recipe_id=recipe_id, is_deleted=False
        ).order_by('-step_number').first()
        return (last.step_number + 1) if last else 1

    @classmethod
    def renumber_after_delete(cls, recipe_id):
        steps = cls.model.objects.filter(
            recipe_id=recipe_id, is_deleted=False
        ).order_by('step_number')
        for idx, step in enumerate(steps, start=1):
            if step.step_number != idx:
                cls.model.objects.filter(id=step.id).update(step_number=idx)
