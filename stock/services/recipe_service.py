from typing import Dict, Any, Optional, List
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import Recipe
from stock.services.base_service import to_decimal, round_decimal
from stock.repositories import (
    RecipeRepository, RecipeIngredientRepository,
    RecipeIngredientSubstituteRepository, RecipeByProductRepository,
    RecipeStepRepository, StockItemRepository, StockUnitRepository,
    StockLocationRepository,
)


class RecipeService:

    @classmethod
    def serialize(cls, recipe: Recipe,
                  include_ingredients: bool = True,
                  include_steps: bool = True,
                  include_byproducts: bool = True,
                  include_cost: bool = False) -> Dict[str, Any]:
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
            "production_location_name": recipe.production_location.name if recipe.production_location else None,

            "is_scalable": recipe.is_scalable,
            "min_batch_size": str(recipe.min_batch_size),
            "max_batch_size": str(recipe.max_batch_size) if recipe.max_batch_size else None,

            "instructions": recipe.instructions,
            "notes": recipe.notes,

            "created_by_id": recipe.created_by_id,
            "approved_by_id": recipe.approved_by_id,
            "approved_at": recipe.approved_at.isoformat() if recipe.approved_at else None,

            "is_active": recipe.is_active,
            "created_at": recipe.created_at.isoformat(),
            "updated_at": recipe.updated_at.isoformat(),
        }

        if include_ingredients:
            data["ingredients"] = [
                RecipeIngredientService.serialize(ing, include_substitutes=True)
                for ing in recipe.ingredients.select_related("stock_item", "unit").order_by("sort_order")
            ]
            data["ingredient_count"] = len(data["ingredients"])

        if include_steps:
            data["steps"] = [
                RecipeStepService.serialize(step)
                for step in recipe.steps.order_by("step_number")
            ]
            data["step_count"] = len(data["steps"])

        if include_byproducts:
            data["by_products"] = [
                RecipeByProductService.serialize(bp)
                for bp in recipe.by_products.select_related("stock_item", "unit")
            ]

        if include_cost:
            data["estimated_cost"] = str(cls.calculate_cost(recipe.id))

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
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None,
             recipe_type: str = None,
             output_item_id: int = None,
             active_only: bool = True,
             active_version_only: bool = True,
             production_location_id: int = None) -> Dict[str, Any]:
        queryset = RecipeRepository.model.objects.select_related("output_item", "output_unit")

        if active_only:
            queryset = queryset.filter(is_active=True)

        if active_version_only:
            queryset = queryset.filter(is_active_version=True)

        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(code__icontains=search) |
                Q(output_item__name__icontains=search)
            )

        if recipe_type:
            queryset = queryset.filter(recipe_type=recipe_type)

        if output_item_id:
            queryset = queryset.filter(output_item_id=output_item_id)

        if production_location_id:
            queryset = queryset.filter(production_location_id=production_location_id)

        queryset = queryset.order_by("name", "-version")

        page_obj, paginator = RecipeRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
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
                {"value": c[0], "label": c[1]}
                for c in Recipe.RecipeType.choices
            ]
        })

    @classmethod
    def search(cls, query: str, limit: int = 20) -> Dict[str, Any]:
        recipes = RecipeRepository.model.objects.filter(
            Q(name__icontains=query) | Q(code__icontains=query),
            is_active=True,
            is_active_version=True
        ).select_related("output_item", "output_unit").order_by("name")[:limit]

        return ServiceResponse.success(data={
            "recipes": [cls.serialize_brief(r) for r in recipes],
            "count": recipes.count()
        })

    @classmethod
    def get_for_item(cls, output_item_id: int) -> Dict[str, Any]:
        recipes = RecipeRepository.model.objects.filter(
            output_item_id=output_item_id,
            is_active=True
        ).select_related("output_unit").order_by("-is_active_version", "-version")

        return ServiceResponse.success(data={
            "recipes": [cls.serialize_brief(r) for r in recipes],
            "count": recipes.count()
        })

    @classmethod
    def get_versions(cls, recipe_id: int) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        root = recipe
        while root.parent_recipe:
            root = root.parent_recipe

        versions = RecipeRepository.model.objects.filter(
            Q(id=root.id) | Q(parent_recipe=root)
        ).order_by("-version")

        return ServiceResponse.success(data={
            "versions": [cls.serialize_brief(v) for v in versions],
            "current_version": recipe.version,
            "active_version": next((v.version for v in versions if v.is_active_version), None)
        })

    @classmethod
    def get(cls, recipe_id: int,
            include_cost: bool = True) -> Dict[str, Any]:
        recipe = RecipeRepository.model.objects.select_related(
            "output_item", "output_unit", "production_location"
        ).filter(id=recipe_id).first()

        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        return ServiceResponse.success(data={
            "recipe": cls.serialize(recipe, include_cost=include_cost)
        })

    @classmethod
    def get_active_for_item(cls, output_item_id: int) -> Optional[Recipe]:
        return RecipeRepository.model.objects.filter(
            output_item_id=output_item_id,
            is_active=True,
            is_active_version=True
        ).first()

    @classmethod
    @transaction.atomic
    def create(cls,
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
               by_products: List[Dict] = None) -> Dict[str, Any]:

        valid_types = [c[0] for c in Recipe.RecipeType.choices]
        if recipe_type not in valid_types:
            return ServiceResponse.validation_error(errors={"recipe_type": f"Invalid recipe type. Valid: {valid_types}"})

        output_item = StockItemRepository.get_by_id(output_item_id)
        if not output_item:
            return ServiceResponse.not_found("Output item not found")

        output_unit = StockUnitRepository.get_by_id(output_unit_id)
        if not output_unit:
            return ServiceResponse.not_found("Output unit not found")

        production_location = None
        if production_location_id:
            production_location = StockLocationRepository.get_by_id(production_location_id)
            if not production_location:
                return ServiceResponse.not_found("Production location not found")

        if not code:
            code = cls._generate_code(name)

        if RecipeRepository.code_exists(code):
            return ServiceResponse.validation_error(errors={"code": f"Recipe code '{code}' already exists"})

        recipe = RecipeRepository.create(
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
                    return result, status

        return ServiceResponse.success(data={
            "id": recipe.id,
            "uuid": str(recipe.uuid),
            "code": recipe.code,
            "recipe": cls.serialize(recipe)
        }, message=f"Recipe '{name}' created")

    @classmethod
    def _generate_code(cls, name: str) -> str:
        prefix = "RCP"
        name_part = "".join(c for c in name.upper() if c.isalnum())[:4]

        count = RecipeRepository.model.objects.filter(code__startswith=f"{prefix}-{name_part}").count()
        return f"{prefix}-{name_part}-{count + 1:03d}"

    @classmethod
    @transaction.atomic
    def update(cls, recipe_id: int, **kwargs) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        minor_fields = ["instructions", "notes", "difficulty_level", "estimated_time_minutes",
                        "production_location_id", "is_scalable", "min_batch_size", "max_batch_size"]

        updating_major = any(k not in minor_fields for k in kwargs.keys()
                            if k not in ["name", "code"])

        if updating_major and recipe.approved_at:
            return cls.create_new_version(recipe_id, **kwargs)

        update_fields = ["updated_at"]

        for field in minor_fields:
            if field in kwargs:
                if field == "production_location_id":
                    if kwargs[field]:
                        location = StockLocationRepository.get_by_id(kwargs[field])
                        if not location:
                            return ServiceResponse.not_found("Production location not found")
                        recipe.production_location = location
                    else:
                        recipe.production_location = None
                    update_fields.append("production_location")
                else:
                    value = kwargs[field]
                    if field in ["min_batch_size", "max_batch_size"]:
                        value = to_decimal(value) if value else None
                    setattr(recipe, field, value)
                    update_fields.append(field)

        if "name" in kwargs:
            recipe.name = kwargs["name"]
            update_fields.append("name")

        recipe.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "recipe": cls.serialize(recipe)
        }, message="Recipe updated")

    @classmethod
    @transaction.atomic
    def create_new_version(cls, recipe_id: int, **kwargs) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        root = recipe
        while root.parent_recipe:
            root = root.parent_recipe

        max_version = RecipeRepository.model.objects.filter(
            Q(id=root.id) | Q(parent_recipe=root)
        ).count()

        new_recipe = RecipeRepository.create(
            name=kwargs.get("name", recipe.name),
            code=recipe.code,
            output_item=recipe.output_item,
            output_quantity=to_decimal(kwargs.get("output_quantity", recipe.output_quantity)),
            output_unit=recipe.output_unit,
            recipe_type=recipe.recipe_type,
            version=max_version + 1,
            is_active_version=False,
            parent_recipe=root,
            yield_percentage=to_decimal(kwargs.get("yield_percentage", recipe.yield_percentage)),
            estimated_time_minutes=kwargs.get("estimated_time_minutes", recipe.estimated_time_minutes),
            difficulty_level=kwargs.get("difficulty_level", recipe.difficulty_level),
            production_location=recipe.production_location,
            instructions=kwargs.get("instructions", recipe.instructions),
            notes=kwargs.get("notes", recipe.notes),
            is_scalable=kwargs.get("is_scalable", recipe.is_scalable),
            min_batch_size=to_decimal(kwargs.get("min_batch_size", recipe.min_batch_size)),
            max_batch_size=to_decimal(kwargs.get("max_batch_size", recipe.max_batch_size)) if recipe.max_batch_size else None,
            created_by_id=kwargs.get("created_by_id"),
        )

        for ing in recipe.ingredients.all():
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
            for sub in ing.substitutes.all():
                RecipeIngredientSubstituteRepository.create(
                    recipe_ingredient=new_ing,
                    substitute_item=sub.substitute_item,
                    quantity=sub.quantity,
                    unit=sub.unit,
                    conversion_note=sub.conversion_note,
                    priority=sub.priority,
                )

        for step in recipe.steps.all():
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

        for bp in recipe.by_products.all():
            RecipeByProductRepository.create(
                recipe=new_recipe,
                stock_item=bp.stock_item,
                expected_quantity=bp.expected_quantity,
                unit=bp.unit,
                is_waste=bp.is_waste,
                value_percentage=bp.value_percentage,
            )

        return ServiceResponse.success(data={
            "id": new_recipe.id,
            "version": new_recipe.version,
            "recipe": cls.serialize(new_recipe)
        }, message=f"New version {new_recipe.version} created")

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
        recipe.save(update_fields=["approved_by", "approved_at", "is_active_version", "updated_at"])

        return ServiceResponse.success(data={
            "recipe": cls.serialize(recipe)
        }, message=f"Recipe v{recipe.version} approved and activated")

    @classmethod
    @transaction.atomic
    def deactivate(cls, recipe_id: int) -> Dict[str, Any]:
        """Deactivate recipe"""
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        recipe.is_active = False
        recipe.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={"id": recipe_id}, message="Recipe deactivated")

    @classmethod
    @transaction.atomic
    def activate(cls, recipe_id: int) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        recipe.is_active = True
        recipe.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={"recipe": cls.serialize(recipe)}, message="Recipe activated")

    @classmethod
    def ingredient_cost_breakdown(cls, recipe: Recipe,
                                  batch_multiplier: Decimal = Decimal("1")) -> List[Dict[str, Any]]:
        """Canonical live-ingredient requirements and costs for a recipe batch.

        Quantities are converted into each item's base unit (the unit used by
        ``StockLevel`` and ``avg_cost_price``), include configured ingredient
        waste, respect non-scalable ingredients, and exclude soft-deleted rows.
        """
        if not recipe:
            return []
        from .unit_service import StockItemUnitService

        multiplier = to_decimal(batch_multiplier, Decimal("1"))
        ingredients = getattr(recipe, '_ai_live_ingredients', None)
        if ingredients is None:
            ingredients = RecipeIngredientRepository.get_for_recipe(recipe.id)

        rows = []
        for ing in ingredients:
            if (
                getattr(ing, 'is_deleted', False)
                or ing.stock_item.is_deleted
                or not ing.stock_item.is_active
            ):
                continue
            ingredient_quantity = to_decimal(ing.quantity)
            quantity = (ingredient_quantity * multiplier
                        if ing.is_scalable else ingredient_quantity)
            quantity_with_waste = quantity
            waste_percentage = to_decimal(ing.waste_percentage)
            if waste_percentage > 0:
                quantity_with_waste *= 1 + waste_percentage / 100
            base_quantity = StockItemUnitService.convert_for_item(
                ing.stock_item_id, quantity_with_waste, ing.unit_id,
            )
            cost = base_quantity * to_decimal(ing.stock_item.avg_cost_price)
            rows.append({
                'ingredient': ing,
                'quantity': quantity,
                'quantity_with_waste': quantity_with_waste,
                'base_quantity': base_quantity,
                'cost': cost,
            })
        return rows

    @classmethod
    def _recipe_cost_total(cls, recipe: Recipe,
                           batch_multiplier: Decimal = Decimal("1")) -> Decimal:
        """Unrounded batch cost for downstream portion arithmetic."""
        return sum(
            (row['cost'] for row in cls.ingredient_cost_breakdown(
                recipe, batch_multiplier,
            )),
            Decimal('0'),
        )

    @classmethod
    def calculate_recipe_cost(cls, recipe: Recipe,
                              batch_multiplier: Decimal = Decimal("1")) -> Decimal:
        return round_decimal(cls._recipe_cost_total(recipe, batch_multiplier), 2)

    @classmethod
    def effective_output_quantity(cls, recipe: Recipe,
                                  batch_multiplier: Decimal = Decimal("1")) -> Decimal:
        """Actual output in ``recipe.output_unit`` after the yield percentage."""
        if not recipe:
            return Decimal('0')
        output = to_decimal(recipe.output_quantity) * to_decimal(
            batch_multiplier, Decimal('1'),
        )
        yield_pct = max(to_decimal(recipe.yield_percentage, Decimal('100')), Decimal('0'))
        return output * yield_pct / Decimal('100')

    @classmethod
    def calculate_portion_cost(cls, recipe: Recipe,
                               quantity: Decimal = Decimal("1"),
                               unit_id: int = None) -> Decimal:
        """Cost for ``quantity`` of recipe output, with unit/yield conversion."""
        portion_ratio = cls.output_portion_ratio(recipe, quantity, unit_id)
        if portion_ratio <= 0:
            return Decimal('0')
        return round_decimal(
            cls._recipe_cost_total(recipe) * portion_ratio, 2,
        )

    @classmethod
    def output_portion_ratio(cls, recipe: Recipe,
                             quantity: Decimal = Decimal("1"),
                             unit_id: int = None) -> Decimal:
        """Fraction of one effective recipe batch represented by an output amount."""
        if not recipe:
            return Decimal('0')
        from .unit_service import StockItemUnitService

        effective_output = cls.effective_output_quantity(recipe)
        if effective_output <= 0:
            return Decimal('0')
        output_base = StockItemUnitService.convert_for_item(
            recipe.output_item_id, effective_output, recipe.output_unit_id,
        )
        portion_base = StockItemUnitService.convert_for_item(
            recipe.output_item_id, to_decimal(quantity),
            unit_id or recipe.output_unit_id,
        )
        if output_base <= 0:
            return Decimal('0')
        return portion_base / output_base

    @classmethod
    def calculate_cost(cls, recipe_id: int, batch_multiplier: Decimal = Decimal("1")) -> Decimal:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return Decimal("0")
        return cls.calculate_recipe_cost(recipe, batch_multiplier)

    @classmethod
    def scale_recipe(cls, recipe_id: int,
                     target_quantity: Decimal = None,
                     batch_multiplier: Decimal = None) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        if not recipe.is_scalable:
            return ServiceResponse.error("This recipe cannot be scaled")

        if target_quantity:
            multiplier = to_decimal(target_quantity) / recipe.output_quantity
        elif batch_multiplier:
            multiplier = to_decimal(batch_multiplier)
        else:
            multiplier = Decimal("1")

        if recipe.min_batch_size and multiplier < recipe.min_batch_size:
            return ServiceResponse.validation_error(errors={"multiplier": f"Minimum batch multiplier is {recipe.min_batch_size}"})
        if recipe.max_batch_size and multiplier > recipe.max_batch_size:
            return ServiceResponse.validation_error(errors={"multiplier": f"Maximum batch multiplier is {recipe.max_batch_size}"})

        scaled_ingredients = []
        for ing in recipe.ingredients.select_related("stock_item", "unit").order_by("sort_order"):
            scaled_qty = ing.quantity * multiplier if ing.is_scalable else ing.quantity
            with_waste = scaled_qty * (1 + ing.waste_percentage / 100) if ing.waste_percentage else scaled_qty

            scaled_ingredients.append({
                "stock_item_id": ing.stock_item_id,
                "stock_item_name": ing.stock_item.name,
                "original_quantity": str(ing.quantity),
                "scaled_quantity": str(round_decimal(scaled_qty, 4)),
                "with_waste": str(round_decimal(with_waste, 4)),
                "unit": ing.unit.short_name,
                "is_optional": ing.is_optional,
            })

        scaled_output = recipe.output_quantity * multiplier

        scaled_byproducts = []
        for bp in recipe.by_products.select_related("stock_item", "unit"):
            scaled_byproducts.append({
                "stock_item_name": bp.stock_item.name,
                "expected_quantity": str(round_decimal(bp.expected_quantity * multiplier, 4)),
                "unit": bp.unit.short_name,
                "is_waste": bp.is_waste,
            })

        return ServiceResponse.success(data={
            "recipe_id": recipe.id,
            "recipe_name": recipe.name,
            "multiplier": str(multiplier),
            "original_output": str(recipe.output_quantity),
            "scaled_output": str(round_decimal(scaled_output, 4)),
            "output_unit": recipe.output_unit.short_name,
            "ingredients": scaled_ingredients,
            "by_products": scaled_byproducts,
            "estimated_cost": str(cls.calculate_cost(recipe.id, multiplier)),
        })

    @classmethod
    @transaction.atomic
    def check_availability(cls, recipe_id: int, quantity: Decimal = Decimal("1"), location_id: int = None, batch_multiplier: Decimal = Decimal("1")) -> Dict[str, Any]:
        from .level_service import StockLevelService

        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        availability = []
        for row in cls.ingredient_cost_breakdown(recipe, batch_multiplier):
            ing = row['ingredient']
            available_stock = StockLevelService.get_available(
                ing.stock_item_id, location_id,
            )
            is_available = (
                ing.is_optional or available_stock >= row['base_quantity']
            )

            availability.append({
                "stock_item_id": ing.stock_item_id,
                "stock_item_name": ing.stock_item.name,
                "required_quantity": str(round_decimal(
                    row['quantity_with_waste'], 4,
                )),
                "required_base_quantity": str(round_decimal(
                    row['base_quantity'], 4,
                )),
                "available_stock": str(round_decimal(available_stock, 4)),
                "unit": ing.unit.short_name,
                "base_unit": ing.stock_item.base_unit.short_name,
                "is_optional": ing.is_optional,
                "is_available": is_available,
            })

        return ServiceResponse.success(data={
            "recipe_id": recipe.id,
            "recipe_name": recipe.name,
            "batch_multiplier": str(batch_multiplier),
            "ingredients": availability,
        })


class RecipeIngredientService:

    @classmethod
    def serialize(cls, ing, include_substitutes: bool = True) -> Dict[str, Any]:
        data = {
            "id": ing.id,
            "uuid": str(ing.uuid),
            "recipe_id": ing.recipe_id,
            "stock_item_id": ing.stock_item_id,
            "stock_item": {
                "id": ing.stock_item.id,
                "name": ing.stock_item.name,
                "sku": ing.stock_item.sku,
            },
            "quantity": str(ing.quantity),
            "unit_id": ing.unit_id,
            "unit": ing.unit.short_name,
            "is_optional": ing.is_optional,
            "is_scalable": ing.is_scalable,
            "waste_percentage": str(ing.waste_percentage),
            "prep_instructions": ing.prep_instructions,
            "sort_order": ing.sort_order,
            "substitute_group": ing.substitute_group,
        }

        if include_substitutes:
            data["substitutes"] = [
                RecipeIngredientSubstituteService.serialize(sub)
                for sub in ing.substitutes.select_related("substitute_item", "unit").order_by("priority")
            ]

        return data

    @classmethod
    @transaction.atomic
    def add(cls,
            recipe_id: int,
            stock_item_id: int,
            quantity: Decimal,
            unit_id: int,
            is_optional: bool = False,
            is_scalable: bool = True,
            waste_percentage: Decimal = Decimal("0"),
            prep_instructions: str = "",
            sort_order: int = 0,
            substitute_group: str = "") -> Dict[str, Any]:

        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found("Stock item not found")

        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found("Unit not found")

        if sort_order == 0:
            last = RecipeIngredientRepository.model.objects.filter(recipe=recipe).order_by("-sort_order").first()
            sort_order = (last.sort_order + 1) if last else 1

        ing = RecipeIngredientRepository.create(
            recipe=recipe,
            stock_item=stock_item,
            quantity=to_decimal(quantity),
            unit=unit,
            is_optional=is_optional,
            is_scalable=is_scalable,
            waste_percentage=to_decimal(waste_percentage),
            prep_instructions=prep_instructions,
            sort_order=sort_order,
            substitute_group=substitute_group,
        )

        return ServiceResponse.success(data={
            "id": ing.id,
            "ingredient": cls.serialize(ing)
        }, message="Ingredient added")

    @classmethod
    @transaction.atomic
    def update(cls, ingredient_id: int, **kwargs) -> Dict[str, Any]:
        ing = RecipeIngredientRepository.model.objects.select_related("stock_item", "unit").filter(id=ingredient_id).first()
        if not ing:
            return ServiceResponse.not_found("Ingredient not found")

        update_fields = ["updated_at"] if hasattr(ing, "updated_at") else []

        if "stock_item_id" in kwargs:
            stock_item = StockItemRepository.get_by_id(kwargs["stock_item_id"])
            if not stock_item:
                return ServiceResponse.not_found("Stock item not found")
            ing.stock_item = stock_item
            update_fields.append("stock_item")

        if "unit_id" in kwargs:
            unit = StockUnitRepository.get_by_id(kwargs["unit_id"])
            if not unit:
                return ServiceResponse.not_found("Unit not found")
            ing.unit = unit
            update_fields.append("unit")

        for field in ["quantity", "is_optional", "is_scalable", "waste_percentage",
                      "prep_instructions", "sort_order", "substitute_group"]:
            if field in kwargs:
                value = kwargs[field]
                if field in ["quantity", "waste_percentage"]:
                    value = to_decimal(value)
                setattr(ing, field, value)
                update_fields.append(field)

        ing.save()

        return ServiceResponse.success(data={
            "ingredient": cls.serialize(ing)
        }, message="Ingredient updated")

    @classmethod
    @transaction.atomic
    def remove(cls, ingredient_id: int) -> Dict[str, Any]:
        ing = RecipeIngredientRepository.get_by_id(ingredient_id)
        if not ing:
            return ServiceResponse.not_found("Ingredient not found")

        ing.delete()
        return ServiceResponse.success(message="Ingredient removed")

    @classmethod
    @transaction.atomic
    def reorder(cls, recipe_id: int, ingredient_ids: List[int]) -> Dict[str, Any]:
        RecipeIngredientRepository.reorder(recipe_id, ingredient_ids)

        return ServiceResponse.success(data={"reordered": len(ingredient_ids)}, message="Ingredients reordered")


class RecipeIngredientSubstituteService:

    @classmethod
    def serialize(cls, sub) -> Dict[str, Any]:
        return {
            "id": sub.id,
            "uuid": str(sub.uuid),
            "recipe_ingredient_id": sub.recipe_ingredient_id,
            "substitute_item_id": sub.substitute_item_id,
            "substitute_item_name": sub.substitute_item.name,
            "quantity": str(sub.quantity),
            "unit": sub.unit.short_name,
            "conversion_note": sub.conversion_note,
            "priority": sub.priority,
        }

    @classmethod
    @transaction.atomic
    def add(cls,
            recipe_ingredient_id: int,
            substitute_item_id: int,
            quantity: Decimal,
            unit_id: int,
            conversion_note: str = "",
            priority: int = 1) -> Dict[str, Any]:

        recipe_ing = RecipeIngredientRepository.get_by_id(recipe_ingredient_id)
        if not recipe_ing:
            return ServiceResponse.not_found("Recipe ingredient not found")

        substitute_item = StockItemRepository.get_by_id(substitute_item_id)
        if not substitute_item:
            return ServiceResponse.not_found("Substitute item not found")

        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found("Unit not found")

        sub = RecipeIngredientSubstituteRepository.create(
            recipe_ingredient=recipe_ing,
            substitute_item=substitute_item,
            quantity=to_decimal(quantity),
            unit=unit,
            conversion_note=conversion_note,
            priority=priority,
        )

        return ServiceResponse.success(data={
            "id": sub.id,
            "substitute": cls.serialize(sub)
        }, message="Substitute added")

    @classmethod
    @transaction.atomic
    def remove(cls, substitute_id: int) -> Dict[str, Any]:
        sub = RecipeIngredientSubstituteRepository.get_by_id(substitute_id)
        if not sub:
            return ServiceResponse.not_found("Substitute not found")

        sub.delete()
        return ServiceResponse.success(message="Substitute removed")


class RecipeByProductService:

    @classmethod
    def serialize(cls, bp) -> Dict[str, Any]:
        return {
            "id": bp.id,
            "uuid": str(bp.uuid),
            "recipe_id": bp.recipe_id,
            "stock_item_id": bp.stock_item_id,
            "stock_item_name": bp.stock_item.name,
            "expected_quantity": str(bp.expected_quantity),
            "unit": bp.unit.short_name,
            "is_waste": bp.is_waste,
            "value_percentage": str(bp.value_percentage),
        }

    @classmethod
    @transaction.atomic
    def add(cls,
            recipe_id: int,
            stock_item_id: int,
            expected_quantity: Decimal,
            unit_id: int,
            is_waste: bool = False,
            value_percentage: Decimal = Decimal("0")) -> Dict[str, Any]:

        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found("Stock item not found")

        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found("Unit not found")

        bp = RecipeByProductRepository.create(
            recipe=recipe,
            stock_item=stock_item,
            expected_quantity=to_decimal(expected_quantity),
            unit=unit,
            is_waste=is_waste,
            value_percentage=to_decimal(value_percentage),
        )

        return ServiceResponse.success(data={
            "id": bp.id,
            "by_product": cls.serialize(bp)
        }, message="By-product added")

    @classmethod
    @transaction.atomic
    def remove(cls, byproduct_id: int) -> Dict[str, Any]:
        bp = RecipeByProductRepository.get_by_id(byproduct_id)
        if not bp:
            return ServiceResponse.not_found("By-product not found")

        bp.delete()
        return ServiceResponse.success(message="By-product removed")


class RecipeStepService:

    @classmethod
    def serialize(cls, step) -> Dict[str, Any]:
        return {
            "id": step.id,
            "uuid": str(step.uuid),
            "recipe_id": step.recipe_id,
            "step_number": step.step_number,
            "title": step.title,
            "description": step.description,
            "duration_minutes": step.duration_minutes,
            "temperature": step.temperature,
            "equipment_needed": step.equipment_needed,
            "is_checkpoint": step.is_checkpoint,
            "photo_url": step.photo_url,
        }

    @classmethod
    @transaction.atomic
    def add(cls,
            recipe_id: int,
            step_number: int,
            title: str,
            description: str = "",
            duration_minutes: int = None,
            temperature: str = "",
            equipment_needed: str = "",
            is_checkpoint: bool = False,
            photo_url: str = "") -> Dict[str, Any]:

        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        if RecipeStepRepository.model.objects.filter(recipe=recipe, step_number=step_number).exists():
            # Descending order avoids transient duplicate positions and every
            # save publishes the shifted step to the peer.
            shifted = RecipeStepRepository.model.objects.select_for_update().filter(
                recipe=recipe,
                step_number__gte=step_number
            ).order_by('-step_number')
            for existing in shifted:
                existing.step_number += 1
                existing.save(update_fields=['step_number'])

        step = RecipeStepRepository.create(
            recipe=recipe,
            step_number=step_number,
            title=title,
            description=description,
            duration_minutes=duration_minutes,
            temperature=temperature,
            equipment_needed=equipment_needed,
            is_checkpoint=is_checkpoint,
            photo_url=photo_url,
        )

        return ServiceResponse.success(data={
            "id": step.id,
            "step": cls.serialize(step)
        }, message="Step added")

    @classmethod
    @transaction.atomic
    def update(cls, step_id: int, **kwargs) -> Dict[str, Any]:
        step = RecipeStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found("Step not found")

        for field in ["title", "description", "duration_minutes", "temperature",
                      "equipment_needed", "is_checkpoint", "photo_url"]:
            if field in kwargs:
                setattr(step, field, kwargs[field])

        step.save()

        return ServiceResponse.success(data={
            "step": cls.serialize(step)
        }, message="Step updated")

    @classmethod
    @transaction.atomic
    def remove(cls, step_id: int) -> Dict[str, Any]:
        step = RecipeStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found("Step not found")

        recipe_id = step.recipe_id
        step_number = step.step_number

        step.delete()

        shifted = RecipeStepRepository.model.objects.select_for_update().filter(
            recipe_id=recipe_id,
            step_number__gt=step_number
        ).order_by('step_number')
        for existing in shifted:
            existing.step_number -= 1
            existing.save(update_fields=['step_number'])

        return ServiceResponse.success(message="Step removed")

    @classmethod
    @transaction.atomic
    def reorder(cls, recipe_id: int, step_ids: List[int]) -> Dict[str, Any]:
        for idx, step_id in enumerate(step_ids, 1):
            step = RecipeStepRepository.model.objects.select_for_update().filter(
                id=step_id, recipe_id=recipe_id,
            ).first()
            if step and step.step_number != idx:
                step.step_number = idx
                step.save(update_fields=['step_number'])

        return ServiceResponse.success(data={"reordered": len(step_ids)}, message="Steps reordered")
