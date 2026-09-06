"""Recipe catalog and version lifecycle."""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from base.helpers.request import coerce_positive_id
from base.helpers.response import ServiceResponse
from stock.models import Recipe
from stock.repositories import (
    RecipeByProductRepository,
    RecipeIngredientRepository,
    RecipeIngredientSubstituteRepository,
    RecipeRepository,
    RecipeStepRepository,
    StockItemRepository,
    StockLocationRepository,
    StockUnitRepository,
)
from stock.services.base_service import to_decimal

from .byproducts import RecipeByProductService
from .costing import RecipeCostingMixin
from .ingredients import RecipeIngredientService
from .loading import load_recipe_children
from .steps import RecipeStepService


class RecipeService(RecipeCostingMixin):
    @classmethod
    def _root_recipe(cls, recipe):
        seen = set()
        while recipe.parent_recipe_id is not None:
            if recipe.pk in seen or len(seen) >= 64:
                return None, ServiceResponse.error(
                    "Recipe ancestry is cyclic or exceeds 64 levels"
                )
            seen.add(recipe.pk)
            recipe = RecipeRepository.get_by_id(recipe.parent_recipe_id)
            if recipe is None:
                return None, ServiceResponse.error(
                    "Recipe ancestor is missing or deleted"
                )
        return recipe, None

    @staticmethod
    def _validate_recipe(recipe):
        try:
            recipe.full_clean()
        except ValidationError as exc:
            return ServiceResponse.validation_error(errors=exc.message_dict)
        errors = {}
        if recipe.output_quantity <= 0:
            errors["output_quantity"] = "Must be greater than zero"
        if not 0 < recipe.yield_percentage <= 100:
            errors["yield_percentage"] = "Must be greater than zero and at most 100"
        if recipe.min_batch_size <= 0:
            errors["min_batch_size"] = "Must be greater than zero"
        if (
            recipe.max_batch_size is not None
            and recipe.max_batch_size < recipe.min_batch_size
        ):
            errors["max_batch_size"] = "Must be at least the minimum batch size"
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        return None

    @classmethod
    def _apply_updates(cls, recipe, values):
        fields = []
        for name in (
            "name",
            "code",
            "output_quantity",
            "yield_percentage",
            "recipe_type",
            "instructions",
            "notes",
            "difficulty_level",
            "estimated_time_minutes",
            "is_scalable",
            "min_batch_size",
            "max_batch_size",
        ):
            if name in values:
                setattr(recipe, name, values[name])
                fields.append(name)
        for name, repository, optional in (
            ("output_item", StockItemRepository, False),
            ("output_unit", StockUnitRepository, False),
            ("production_location", StockLocationRepository, True),
        ):
            key = name + "_id"
            if key not in values:
                continue
            value = values[key]
            if optional and (value is None or value == ""):
                related = None
            else:
                ident = coerce_positive_id(value)
                if ident is None:
                    return None, ServiceResponse.validation_error(
                        errors={key: "Must be a positive integer ID"}
                    )
                related = repository.get_by_id(ident)
                if related is None:
                    return None, ServiceResponse.not_found(
                        f'{name.replace("_", " ").capitalize()} not found'
                    )
            setattr(recipe, name, related)
            fields.append(name)
        return fields, cls._validate_recipe(recipe)

    @classmethod
    def serialize(
        cls,
        recipe: Recipe,
        include_ingredients: bool = True,
        include_steps: bool = True,
        include_byproducts: bool = True,
        include_cost: bool = False,
    ) -> Dict[str, Any]:
        load_recipe_children(
            [recipe],
            include_ingredients=include_ingredients,
            include_steps=include_steps,
            include_byproducts=include_byproducts,
            include_substitutes=include_ingredients,
            include_cost=include_cost,
        )
        data = {
            "id": recipe.id,
            "uuid": str(recipe.uuid),
            "name": recipe.name,
            "code": recipe.code,
            "output_item_id": recipe.output_item_id,
            "output_item": {
                "id": recipe.output_item.id,
                "name": recipe.output_item.name,
                "sku": recipe.output_item.sku,
            },
            "output_quantity": str(recipe.output_quantity),
            "output_unit_id": recipe.output_unit_id,
            "output_unit": recipe.output_unit.short_name,
            "recipe_type": recipe.recipe_type,
            "recipe_type_display": recipe.get_recipe_type_display(),
            "version": recipe.version,
            "is_active_version": recipe.is_active_version,
            "parent_recipe_id": recipe.parent_recipe_id,
            "yield_percentage": str(recipe.yield_percentage),
            "estimated_time_minutes": recipe.estimated_time_minutes,
            "difficulty_level": recipe.difficulty_level,
            "production_location_id": recipe.production_location_id,
            "production_location_name": recipe.production_location.name
            if recipe.production_location
            else None,
            "is_scalable": recipe.is_scalable,
            "min_batch_size": str(recipe.min_batch_size),
            "max_batch_size": str(recipe.max_batch_size)
            if recipe.max_batch_size
            else None,
            "instructions": recipe.instructions,
            "notes": recipe.notes,
            "created_by_id": recipe.created_by_id,
            "approved_by_id": recipe.approved_by_id,
            "approved_at": recipe.approved_at.isoformat()
            if recipe.approved_at
            else None,
            "is_active": recipe.is_active,
            "created_at": recipe.created_at.isoformat(),
            "updated_at": recipe.updated_at.isoformat(),
        }

        if include_ingredients:
            data["ingredients"] = [
                RecipeIngredientService.serialize(ing, include_substitutes=True)
                for ing in recipe._live_ingredients
            ]
            data["ingredient_count"] = len(data["ingredients"])

        if include_steps:
            data["steps"] = [
                RecipeStepService.serialize(step) for step in recipe._live_steps
            ]
            data["step_count"] = len(data["steps"])

        if include_byproducts:
            data["by_products"] = [
                RecipeByProductService.serialize(bp) for bp in recipe._live_byproducts
            ]

        if include_cost:
            data["estimated_cost"] = str(cls.calculate_recipe_cost(recipe))

        return data

    @classmethod
    def serialize_brief(cls, recipe: Recipe) -> Dict[str, Any]:
        return {
            "id": recipe.id,
            "uuid": str(recipe.uuid),
            "name": recipe.name,
            "code": recipe.code,
            "recipe_type": recipe.recipe_type,
            "output_item_name": recipe.output_item.name,
            "output_quantity": str(recipe.output_quantity),
            "output_unit": recipe.output_unit.short_name,
            "version": recipe.version,
            "is_active_version": recipe.is_active_version,
            "is_active": recipe.is_active,
        }

    @classmethod
    def list(
        cls,
        page: int = 1,
        per_page: int = 20,
        search: str = None,
        recipe_type: str = None,
        output_item_id: int = None,
        active_only: bool = True,
        active_version_only: bool = True,
        production_location_id: int = None,
    ) -> Dict[str, Any]:
        queryset = RecipeRepository.model.objects.filter(
            is_deleted=False
        ).select_related("output_item", "output_unit")

        if active_only:
            queryset = queryset.filter(is_active=True)

        if active_version_only:
            queryset = queryset.filter(is_active_version=True)

        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(output_item__name__icontains=search)
            )

        if recipe_type:
            queryset = queryset.filter(recipe_type=recipe_type)

        if output_item_id:
            queryset = queryset.filter(output_item_id=output_item_id)

        if production_location_id:
            queryset = queryset.filter(production_location_id=production_location_id)

        queryset = queryset.order_by("name", "-version")

        page_obj, paginator = RecipeRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(
            data={
                "recipes": [cls.serialize_brief(r) for r in page_obj.object_list],
                "pagination": {
                    "current_page": page_obj.number,
                    "total_pages": paginator.num_pages,
                    "total_items": paginator.count,
                    "per_page": per_page,
                    "has_next": page_obj.has_next(),
                    "has_previous": page_obj.has_previous(),
                },
                "recipe_types": [
                    {"value": c[0], "label": c[1]} for c in Recipe.RecipeType.choices
                ],
            }
        )

    @classmethod
    def search(cls, query: str, limit: int = 20) -> Dict[str, Any]:
        recipes = (
            RecipeRepository.model.objects.filter(
                Q(name__icontains=query) | Q(code__icontains=query),
                is_deleted=False,
                is_active=True,
                is_active_version=True,
            )
            .select_related("output_item", "output_unit")
            .order_by("name")[:limit]
        )

        return ServiceResponse.success(
            data={
                "recipes": [cls.serialize_brief(r) for r in recipes],
                "count": recipes.count(),
            }
        )

    @classmethod
    def get_for_item(cls, output_item_id: int) -> Dict[str, Any]:
        recipes = (
            RecipeRepository.model.objects.filter(
                output_item_id=output_item_id, is_deleted=False, is_active=True
            )
            .select_related("output_item", "output_unit")
            .order_by("-is_active_version", "-version")
        )

        return ServiceResponse.success(
            data={
                "recipes": [cls.serialize_brief(r) for r in recipes],
                "count": recipes.count(),
            }
        )

    @classmethod
    def get_versions(cls, recipe_id: int) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        root, error = cls._root_recipe(recipe)
        if error:
            return error

        versions = (
            RecipeRepository.model.objects.filter(
                Q(id=root.id) | Q(parent_recipe=root),
                is_deleted=False,
            )
            .select_related("output_item", "output_unit")
            .order_by("-version")
        )

        return ServiceResponse.success(
            data={
                "versions": [cls.serialize_brief(v) for v in versions],
                "current_version": recipe.version,
                "active_version": next(
                    (v.version for v in versions if v.is_active_version), None
                ),
            }
        )

    @classmethod
    def get(cls, recipe_id: int, include_cost: bool = True) -> Dict[str, Any]:
        recipe = (
            RecipeRepository.model.objects.select_related(
                "output_item__base_unit", "output_unit", "production_location"
            )
            .filter(id=recipe_id, is_deleted=False)
            .first()
        )

        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        return ServiceResponse.success(
            data={"recipe": cls.serialize(recipe, include_cost=include_cost)}
        )

    @classmethod
    def get_active_for_item(cls, output_item_id: int) -> Optional[Recipe]:
        return RecipeRepository.model.objects.filter(
            output_item_id=output_item_id,
            is_deleted=False,
            is_active=True,
            is_active_version=True,
        ).first()

    @classmethod
    @transaction.atomic
    def create(
        cls,
        name: str,
        output_item_id: int,
        output_quantity: Decimal,
        output_unit_id: int,
        recipe_type: str = "PRODUCTION",
        code: str = None,
        yield_percentage: Decimal = Decimal("100"),
        estimated_time_minutes: int = None,
        difficulty_level: int = 1,
        production_location_id: int = None,
        instructions: str = "",
        notes: str = "",
        is_scalable: bool = True,
        min_batch_size: Decimal = Decimal("1"),
        max_batch_size: Decimal = None,
        created_by_id: int = None,
        ingredients: List[Dict] = None,
        steps: List[Dict] = None,
        by_products: List[Dict] = None,
    ) -> Dict[str, Any]:
        valid_types = [c[0] for c in Recipe.RecipeType.choices]
        if recipe_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"recipe_type": f"Invalid recipe type. Valid: {valid_types}"}
            )

        output_item = StockItemRepository.get_by_id(output_item_id)
        if not output_item:
            return ServiceResponse.not_found("Output item not found")

        output_unit = StockUnitRepository.get_by_id(output_unit_id)
        if not output_unit:
            return ServiceResponse.not_found("Output unit not found")

        production_location = None
        if production_location_id:
            production_location = StockLocationRepository.get_by_id(
                production_location_id
            )
            if not production_location:
                return ServiceResponse.not_found("Production location not found")

        if not code:
            code = cls._generate_code(name)

        if RecipeRepository.code_exists(code):
            return ServiceResponse.validation_error(
                errors={"code": f"Recipe code '{code}' already exists"}
            )

        recipe = Recipe(
            name=name,
            code=code,
            output_item=output_item,
            output_quantity=to_decimal(output_quantity),
            output_unit=output_unit,
            recipe_type=recipe_type,
            version=1,
            is_active_version=True,
            yield_percentage=to_decimal(yield_percentage),
            estimated_time_minutes=estimated_time_minutes,
            difficulty_level=difficulty_level,
            production_location=production_location,
            instructions=instructions,
            notes=notes,
            is_scalable=is_scalable,
            min_batch_size=to_decimal(min_batch_size),
            max_batch_size=to_decimal(max_batch_size) if max_batch_size else None,
            created_by_id=created_by_id,
        )

        error = cls._validate_recipe(recipe)
        if error:
            return error
        recipe.save()

        if ingredients:
            for idx, ing_data in enumerate(ingredients):
                result, status = RecipeIngredientService.add(
                    recipe_id=recipe.id,
                    stock_item_id=ing_data["stock_item_id"],
                    quantity=ing_data["quantity"],
                    unit_id=ing_data["unit_id"],
                    is_optional=ing_data.get("is_optional", False),
                    waste_percentage=ing_data.get("waste_percentage", 0),
                    prep_instructions=ing_data.get("prep_instructions", ""),
                    sort_order=ing_data.get("sort_order", idx),
                )
                if status >= 400:
                    transaction.set_rollback(True)
                    return result, status

        if steps:
            for step_data in steps:
                result, status = RecipeStepService.add(
                    recipe_id=recipe.id,
                    step_number=step_data["step_number"],
                    title=step_data["title"],
                    description=step_data.get("description", ""),
                    duration_minutes=step_data.get("duration_minutes"),
                    temperature=step_data.get("temperature", ""),
                    equipment_needed=step_data.get("equipment_needed", ""),
                    is_checkpoint=step_data.get("is_checkpoint", False),
                )
                if status >= 400:
                    transaction.set_rollback(True)
                    return result, status

        if by_products:
            for bp_data in by_products:
                result, status = RecipeByProductService.add(
                    recipe_id=recipe.id,
                    stock_item_id=bp_data["stock_item_id"],
                    expected_quantity=bp_data["expected_quantity"],
                    unit_id=bp_data["unit_id"],
                    is_waste=bp_data.get("is_waste", False),
                    value_percentage=bp_data.get("value_percentage", 0),
                )
                if status >= 400:
                    transaction.set_rollback(True)
                    return result, status

        return ServiceResponse.success(
            data={
                "id": recipe.id,
                "uuid": str(recipe.uuid),
                "code": recipe.code,
                "recipe": cls.serialize(recipe),
            },
            message=f"Recipe '{name}' created",
        )

    @classmethod
    def _generate_code(cls, name: str) -> str:
        prefix = "RCP"
        name_part = "".join(c for c in name.upper() if c.isalnum())[:4]

        count = RecipeRepository.model.objects.filter(
            code__startswith=f"{prefix}-{name_part}"
        ).count()
        return f"{prefix}-{name_part}-{count + 1:03d}"

    @classmethod
    @transaction.atomic
    def update(cls, recipe_id: int, **kwargs) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        major_fields = {
            "output_quantity",
            "output_item_id",
            "output_unit_id",
            "yield_percentage",
            "recipe_type",
        }
        if major_fields.intersection(kwargs) and recipe.approved_at:
            return cls.create_new_version(recipe_id, **kwargs)

        fields, error = cls._apply_updates(recipe, kwargs)
        if error:
            return error
        if fields:
            recipe.save(update_fields=fields + ["updated_at"])

        return ServiceResponse.success(
            data={"recipe": cls.serialize(recipe)}, message="Recipe updated"
        )

    @classmethod
    @transaction.atomic
    def create_new_version(cls, recipe_id: int, **kwargs) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        root, error = cls._root_recipe(recipe)
        if error:
            return error

        # Versions of a family serialize on its root, including concurrent edits.
        root = Recipe.objects.select_for_update().get(pk=root.pk)
        max_version = (
            Recipe.objects.filter(
                Q(pk=root.pk) | Q(parent_recipe_id=root.pk),
            ).aggregate(value=Max("version"))["value"]
            or root.version
        )
        next_version = max_version + 1

        new_recipe = Recipe(
            name=kwargs.get("name", recipe.name),
            code=f"RCP-{root.uuid.hex}-V{next_version}",
            output_item=recipe.output_item,
            output_quantity=to_decimal(
                kwargs.get("output_quantity", recipe.output_quantity)
            ),
            output_unit=recipe.output_unit,
            recipe_type=recipe.recipe_type,
            version=next_version,
            is_active_version=False,
            parent_recipe=root,
            yield_percentage=to_decimal(
                kwargs.get("yield_percentage", recipe.yield_percentage)
            ),
            estimated_time_minutes=kwargs.get(
                "estimated_time_minutes", recipe.estimated_time_minutes
            ),
            difficulty_level=kwargs.get("difficulty_level", recipe.difficulty_level),
            production_location=recipe.production_location,
            instructions=kwargs.get("instructions", recipe.instructions),
            notes=kwargs.get("notes", recipe.notes),
            is_scalable=kwargs.get("is_scalable", recipe.is_scalable),
            min_batch_size=to_decimal(
                kwargs.get("min_batch_size", recipe.min_batch_size)
            ),
            max_batch_size=to_decimal(
                kwargs.get("max_batch_size", recipe.max_batch_size)
            )
            if recipe.max_batch_size
            else None,
            created_by_id=kwargs.get("created_by_id"),
        )

        # Apply the same editable field validation to the unsaved draft version.
        _, error = cls._apply_updates(
            new_recipe, {k: v for k, v in kwargs.items() if k != "code"}
        )
        if error:
            return error
        new_recipe.save()

        for ing in recipe.ingredients.filter(is_deleted=False).select_related(
            "stock_item", "unit"
        ):
            new_ing = RecipeIngredientRepository.create(
                recipe=new_recipe,
                stock_item=ing.stock_item,
                quantity=ing.quantity,
                unit=ing.unit,
                is_optional=ing.is_optional,
                is_scalable=ing.is_scalable,
                waste_percentage=ing.waste_percentage,
                prep_instructions=ing.prep_instructions,
                sort_order=ing.sort_order,
                substitute_group=ing.substitute_group,
            )
            for sub in ing.substitutes.filter(is_deleted=False).select_related(
                "substitute_item", "unit"
            ):
                RecipeIngredientSubstituteRepository.create(
                    recipe_ingredient=new_ing,
                    substitute_item=sub.substitute_item,
                    quantity=sub.quantity,
                    unit=sub.unit,
                    conversion_note=sub.conversion_note,
                    priority=sub.priority,
                )

        for step in recipe.steps.filter(is_deleted=False):
            RecipeStepRepository.create(
                recipe=new_recipe,
                step_number=step.step_number,
                title=step.title,
                description=step.description,
                duration_minutes=step.duration_minutes,
                temperature=step.temperature,
                equipment_needed=step.equipment_needed,
                is_checkpoint=step.is_checkpoint,
                photo_url=step.photo_url,
            )

        for bp in recipe.by_products.filter(is_deleted=False).select_related(
            "stock_item", "unit"
        ):
            RecipeByProductRepository.create(
                recipe=new_recipe,
                stock_item=bp.stock_item,
                expected_quantity=bp.expected_quantity,
                unit=bp.unit,
                is_waste=bp.is_waste,
                value_percentage=bp.value_percentage,
            )

        return ServiceResponse.success(
            data={
                "id": new_recipe.id,
                "version": new_recipe.version,
                "recipe": cls.serialize(new_recipe),
            },
            message=f"New version {new_recipe.version} created",
        )

    @classmethod
    @transaction.atomic
    def approve(cls, recipe_id: int, approved_by_id: int) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        if recipe.approved_at:
            return ServiceResponse.error("Recipe is already approved")

        if recipe.parent_recipe:
            RecipeRepository.deactivate_other_versions(recipe)

        recipe.approved_by_id = approved_by_id
        recipe.approved_at = timezone.now()
        recipe.is_active_version = True
        recipe.save(
            update_fields=[
                "approved_by",
                "approved_at",
                "is_active_version",
                "updated_at",
            ]
        )

        return ServiceResponse.success(
            data={"recipe": cls.serialize(recipe)},
            message=f"Recipe v{recipe.version} approved and activated",
        )

    @classmethod
    @transaction.atomic
    def deactivate(cls, recipe_id: int) -> Dict[str, Any]:
        """Deactivate recipe"""
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        recipe.is_active = False
        recipe.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(
            data={"id": recipe_id}, message="Recipe deactivated"
        )

    @classmethod
    @transaction.atomic
    def activate(cls, recipe_id: int) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        recipe.is_active = True
        recipe.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(
            data={"recipe": cls.serialize(recipe)}, message="Recipe activated"
        )
