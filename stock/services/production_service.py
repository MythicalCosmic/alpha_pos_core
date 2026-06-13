from typing import Dict, Any, Tuple
from decimal import Decimal
from datetime import datetime, timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import (
    ProductionOrder, ProductionOrderIngredient, ProductionOrderOutput, ProductionOrderStep,
    Recipe, StockBatch, StockSettings
)
from stock.services.base_service import to_decimal, round_decimal, generate_number
from stock.repositories import (
    ProductionOrderRepository, ProductionOrderIngredientRepository,
    ProductionOrderOutputRepository, ProductionOrderStepRepository,
    RecipeRepository, StockItemRepository, StockLocationRepository,
)


class _ProductionStepError(Exception):
    """Raised by complete()'s helpers to abort the whole production-completion
    transaction with a clean (result, status) response. Replaces the old
    nested-@transaction.atomic + transaction.set_rollback(True) + return
    pattern, which only rolled back the *inner* savepoint — so a failure in
    _create_output left the ingredients consumed by _consume_ingredients
    committed while the PO stayed IN_PROGRESS (silent stock loss). Raising
    instead lets the single outer atomic roll everything back together."""

    def __init__(self, result, status):
        super().__init__(result.get('message') if isinstance(result, dict) else str(result))
        self.result = result
        self.status = status


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class ProductionOrderService:

    @classmethod
    def serialize(cls, po: ProductionOrder,
                  include_ingredients: bool = True,
                  include_outputs: bool = True,
                  include_steps: bool = True) -> Dict[str, Any]:
        data = {
            "id": po.id,
            "uuid": str(po.uuid),
            "order_number": po.order_number,

            "recipe_id": po.recipe_id,
            "recipe": {
                "id": po.recipe.id,
                "name": po.recipe.name,
                "code": po.recipe.code,
            },

            "batch_multiplier": str(po.batch_multiplier),
            "expected_output_qty": str(po.expected_output_qty),
            "actual_output_qty": str(po.actual_output_qty) if po.actual_output_qty else None,
            "output_unit": po.output_unit.short_name,

            "status": po.status,
            "status_display": po.get_status_display(),
            "priority": po.priority,
            "priority_display": po.get_priority_display(),

            "source_location_id": po.source_location_id,
            "source_location": po.source_location.name,
            "output_location_id": po.output_location_id,
            "output_location": po.output_location.name,

            "planned_start": po.planned_start.isoformat() if po.planned_start else None,
            "planned_end": po.planned_end.isoformat() if po.planned_end else None,
            "actual_start": po.actual_start.isoformat() if po.actual_start else None,
            "actual_end": po.actual_end.isoformat() if po.actual_end else None,

            "assigned_to_id": po.assigned_to_id,
            "created_by_id": po.created_by_id,

            "notes": po.notes,
            "created_at": po.created_at.isoformat(),
            "updated_at": po.updated_at.isoformat(),
        }

        if include_ingredients:
            data["ingredients"] = [
                ProductionOrderIngredientService.serialize(ing)
                for ing in po.ingredients.select_related("stock_item", "unit")
            ]

        if include_outputs:
            data["outputs"] = [
                ProductionOrderOutputService.serialize(out)
                for out in po.outputs.select_related("stock_item", "unit")
            ]

        if include_steps:
            data["steps"] = [
                ProductionOrderStepService.serialize(step)
                for step in po.steps.select_related("recipe_step").order_by("recipe_step__step_number")
            ]

        return data

    @classmethod
    def serialize_brief(cls, po: ProductionOrder) -> Dict[str, Any]:
        return {
            "id": po.id,
            "order_number": po.order_number,
            "recipe_name": po.recipe.name,
            "expected_output_qty": str(po.expected_output_qty),
            "output_unit": po.output_unit.short_name,
            "status": po.status,
            "status_display": po.get_status_display(),
            "priority": po.priority,
            "planned_start": po.planned_start.isoformat() if po.planned_start else None,
        }


    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None,
             status: str = None,
             priority: str = None,
             recipe_id: int = None,
             assigned_to_id: int = None,
             location_id: int = None,
             date_from: datetime = None,
             date_to: datetime = None) -> Tuple[Dict[str, Any], int]:
        queryset = ProductionOrderRepository.get_all().select_related(
            "recipe", "output_unit", "source_location", "output_location"
        )

        if search:
            queryset = queryset.filter(
                Q(order_number__icontains=search) |
                Q(recipe__name__icontains=search)
            )

        if status:
            queryset = queryset.filter(status=status)

        if priority:
            queryset = queryset.filter(priority=priority)

        if recipe_id:
            queryset = queryset.filter(recipe_id=recipe_id)

        if assigned_to_id:
            queryset = queryset.filter(assigned_to_id=assigned_to_id)

        if location_id:
            queryset = queryset.filter(
                Q(source_location_id=location_id) | Q(output_location_id=location_id)
            )

        if date_from:
            queryset = queryset.filter(planned_start__gte=date_from)

        if date_to:
            queryset = queryset.filter(planned_start__lte=date_to)

        queryset = queryset.order_by("-priority", "planned_start", "-created_at")

        page_obj, paginator = ProductionOrderRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "orders": [cls.serialize_brief(po) for po in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [{"value": c[0], "label": c[1]} for c in ProductionOrder.Status.choices],
            "priorities": [{"value": c[0], "label": c[1]} for c in ProductionOrder.Priority.choices],
        })

    @classmethod
    def get_active(cls, location_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = ProductionOrderRepository.get_all().filter(
            status__in=["PLANNED", "IN_PROGRESS"]
        ).select_related("recipe", "output_unit")

        if location_id:
            queryset = queryset.filter(
                Q(source_location_id=location_id) | Q(output_location_id=location_id)
            )

        orders = queryset.order_by("-priority", "planned_start")

        return ServiceResponse.success(data={
            "orders": [cls.serialize_brief(po) for po in orders],
            "count": orders.count()
        })

    @classmethod
    def get_schedule(cls,
                     date_from: datetime,
                     date_to: datetime,
                     location_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = ProductionOrderRepository.get_all().filter(
            planned_start__gte=date_from,
            planned_start__lte=date_to,
            status__in=["DRAFT", "PLANNED", "IN_PROGRESS"]
        ).select_related("recipe", "output_unit", "output_location")

        if location_id:
            queryset = queryset.filter(output_location_id=location_id)

        orders = queryset.order_by("planned_start")

        return ServiceResponse.success(data={
            "schedule": [cls.serialize_brief(po) for po in orders],
            "count": orders.count(),
            "date_range": {
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
            }
        })


    @classmethod
    def get(cls, po_id: int) -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_with_relations(po_id)

        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               recipe_id: int,
               created_by_id: int,
               batch_multiplier: Decimal = Decimal("1"),
               source_location_id: int = None,
               output_location_id: int = None,
               planned_start: datetime = None,
               planned_end: datetime = None,
               priority: str = "NORMAL",
               assigned_to_id: int = None,
               notes: str = "",
               auto_allocate: bool = False) -> Tuple[Dict[str, Any], int]:

        recipe = RecipeRepository.first(id=recipe_id, is_active=True, is_active_version=True)
        if not recipe:
            return ServiceResponse.not_found(f"Recipe with id {recipe_id} not found")

        # select_related fields needed for recipe
        recipe = Recipe.objects.select_related(
            "output_item", "output_unit", "production_location"
        ).get(id=recipe_id)

        batch_multiplier = to_decimal(batch_multiplier)

        if recipe.min_batch_size and batch_multiplier < recipe.min_batch_size:
            return ServiceResponse.validation_error(
                errors={"batch_multiplier": f"Minimum batch multiplier is {recipe.min_batch_size}"}
            )
        if recipe.max_batch_size and batch_multiplier > recipe.max_batch_size:
            return ServiceResponse.validation_error(
                errors={"batch_multiplier": f"Maximum batch multiplier is {recipe.max_batch_size}"}
            )

        settings = StockSettings.load()

        if not source_location_id:
            source_location_id = settings.default_location_id
        if not output_location_id:
            output_location_id = (
                recipe.production_location_id or
                settings.default_production_location_id or
                source_location_id
            )

        source_location = StockLocationRepository.first(id=source_location_id, is_active=True)
        if not source_location:
            return ServiceResponse.not_found(f"Source location with id {source_location_id} not found")

        output_location = StockLocationRepository.first(id=output_location_id, is_active=True)
        if not output_location:
            return ServiceResponse.not_found(f"Output location with id {output_location_id} not found")

        expected_output = recipe.output_quantity * batch_multiplier
        if recipe.yield_percentage < 100:
            expected_output = expected_output * recipe.yield_percentage / 100

        if planned_start and not planned_end and recipe.estimated_time_minutes:
            planned_end = planned_start + timedelta(minutes=recipe.estimated_time_minutes)

        order_number = generate_number("PROD", ProductionOrder, "order_number")

        po = ProductionOrderRepository.create(
            order_number=order_number,
            recipe=recipe,
            batch_multiplier=batch_multiplier,
            expected_output_qty=round_decimal(expected_output, 4),
            output_unit=recipe.output_unit,
            status=ProductionOrder.Status.DRAFT,
            priority=priority,
            source_location=source_location,
            output_location=output_location,
            planned_start=planned_start,
            planned_end=planned_end,
            assigned_to_id=assigned_to_id,
            created_by_id=created_by_id,
            notes=notes,
        )

        for recipe_ing in recipe.ingredients.select_related("stock_item", "unit"):
            if recipe_ing.is_scalable:
                planned_qty = recipe_ing.quantity * batch_multiplier
            else:
                planned_qty = recipe_ing.quantity

            if recipe_ing.waste_percentage > 0:
                planned_qty = planned_qty * (1 + recipe_ing.waste_percentage / 100)

            ProductionOrderIngredientRepository.create(
                production_order=po,
                recipe_ingredient=recipe_ing,
                stock_item=recipe_ing.stock_item,
                planned_quantity=round_decimal(planned_qty, 4),
                unit=recipe_ing.unit,
                status=ProductionOrderIngredient.IngredientStatus.PENDING,
            )

        for recipe_step in recipe.steps.all():
            ProductionOrderStepRepository.create(
                production_order=po,
                recipe_step=recipe_step,
                status=ProductionOrderStep.StepStatus.PENDING,
            )

        if auto_allocate:
            try:
                cls._allocate_ingredients(po.id)
            except _ProductionStepError as e:
                transaction.set_rollback(True)
                return e.result, e.status

        return ServiceResponse.success(data={
            "id": po.id,
            "order_number": order_number,
            "order": cls.serialize(po)
        }, message=f"Production order {order_number} created")

    @classmethod
    @transaction.atomic
    def create_from_low_stock(cls,
                              output_item_id: int,
                              created_by_id: int,
                              target_quantity: Decimal = None) -> Tuple[Dict[str, Any], int]:

        from .recipe_service import RecipeService
        recipe = RecipeService.get_active_for_item(output_item_id)

        if not recipe:
            return ServiceResponse.error("No active recipe found for this item")

        if not target_quantity:
            from .level_service import StockLevelService
            item = StockItemRepository.get_by_id(output_item_id)
            if not item:
                return ServiceResponse.not_found(f"Stock item with id {output_item_id} not found")
            current = StockLevelService.get_available(output_item_id)
            shortage = item.reorder_point - current
            target_quantity = max(shortage, recipe.output_quantity)

        batch_multiplier = to_decimal(target_quantity) / recipe.output_quantity

        if recipe.min_batch_size and batch_multiplier < recipe.min_batch_size:
            batch_multiplier = recipe.min_batch_size

        return cls.create(
            recipe_id=recipe.id,
            created_by_id=created_by_id,
            batch_multiplier=batch_multiplier,
            notes="Auto-generated from low stock"
        )

    @classmethod
    @transaction.atomic
    def plan(cls, po_id: int, planned_start: int = None) -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        if po.status != ProductionOrder.Status.DRAFT:
            return ServiceResponse.error(f"Cannot plan order in {po.status} status")

        availability_result, availability_status = cls.check_ingredient_availability(po_id)

        if availability_status >= 400:
            return availability_result, availability_status

        availability_data = availability_result.get("data", {})
        if not availability_data.get("all_available"):
            return ServiceResponse.error("Not all ingredients are available")

        po.status = ProductionOrder.Status.PLANNED
        po.save(update_fields=["status", "updated_at"])
        try:
            cls._allocate_ingredients(po_id)
        except _ProductionStepError as e:
            transaction.set_rollback(True)
            return e.result, e.status

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Production order planned")

    @classmethod
    @transaction.atomic
    def start(cls, po_id: int, user_id: int = None) -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        if po.status != ProductionOrder.Status.PLANNED:
            return ServiceResponse.error(f"Cannot start order in {po.status} status")

        po.status = ProductionOrder.Status.IN_PROGRESS
        po.actual_start = timezone.now()

        update_fields = ["status", "actual_start", "updated_at"]

        if user_id and not po.assigned_to_id:
            po.assigned_to_id = user_id
            update_fields.append("assigned_to")

        po.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Production started")

    @classmethod
    def complete(cls, po_id: int,
                 actual_output_qty: Decimal,
                 user_id: int,
                 quality_status: str = "PASSED",
                 notes: str = "") -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        if po.status != ProductionOrder.Status.IN_PROGRESS:
            return ServiceResponse.error(f"Cannot complete order in {po.status} status")

        actual_output_qty = to_decimal(actual_output_qty)
        if actual_output_qty <= 0:
            # Zero/negative output would create a zero-quantity batch (rejected
            # downstream) and a meaningless zero-cost PRODUCTION_IN. Reject up
            # front instead of failing mid-transaction.
            return ServiceResponse.error("Actual output quantity must be greater than zero")

        # Single transaction owns the whole completion: consume ingredients,
        # create output, flip status. The helpers raise _ProductionStepError on
        # a stock failure so the atomic rolls back EVERYTHING — no partial
        # commit where stock is consumed but no output is produced.
        try:
            with transaction.atomic():
                cls._consume_ingredients(po_id, user_id)
                cls._create_output(po_id, actual_output_qty, user_id, quality_status)

                po.status = ProductionOrder.Status.COMPLETED
                po.actual_output_qty = actual_output_qty
                po.actual_end = timezone.now()

                if notes:
                    po.notes = f"{po.notes}\n{notes}".strip()

                po.save(update_fields=["status", "actual_output_qty", "actual_end", "notes", "updated_at"])
        except _ProductionStepError as e:
            return e.result, e.status

        variance = actual_output_qty - po.expected_output_qty
        variance_pct = (variance / po.expected_output_qty * 100) if po.expected_output_qty else 0

        return ServiceResponse.success(data={
            "order": cls.serialize(po),
            "variance": {
                "quantity": str(variance),
                "percentage": str(round_decimal(variance_pct, 2)),
            }
        }, message="Production completed")

    @classmethod
    @transaction.atomic
    def cancel(cls, po_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        if po.status in [ProductionOrder.Status.COMPLETED, ProductionOrder.Status.CANCELED]:
            return ServiceResponse.error(f"Cannot cancel order in {po.status} status")

        if po.status in [ProductionOrder.Status.PLANNED, ProductionOrder.Status.IN_PROGRESS]:
            try:
                cls._release_ingredients(po_id)
            except _ProductionStepError as e:
                transaction.set_rollback(True)
                return e.result, e.status

        po.status = ProductionOrder.Status.CANCELED
        if reason:
            po.notes = f"{po.notes}\nCancelled: {reason}".strip()

        po.save(update_fields=["status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Production order cancelled")

    @classmethod
    @transaction.atomic
    def hold(cls, po_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        if po.status not in [ProductionOrder.Status.PLANNED, ProductionOrder.Status.IN_PROGRESS]:
            return ServiceResponse.error(f"Cannot hold order in {po.status} status")

        po.status = ProductionOrder.Status.ON_HOLD
        if reason:
            po.notes = f"{po.notes}\nOn hold: {reason}".strip()

        po.save(update_fields=["status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Production order on hold")

    @classmethod
    @transaction.atomic
    def resume(cls, po_id: int) -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        if po.status != ProductionOrder.Status.ON_HOLD:
            return ServiceResponse.error(f"Cannot resume order in {po.status} status")

        new_status = ProductionOrder.Status.IN_PROGRESS if po.actual_start else ProductionOrder.Status.PLANNED

        po.status = new_status
        po.save(update_fields=["status", "updated_at"])

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Production resumed")


    @classmethod
    def check_ingredient_availability(cls, po_id: int) -> Tuple[Dict[str, Any], int]:
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found(f"Production order with id {po_id} not found")

        from .level_service import StockLevelService

        availability = []
        all_available = True

        for ing in po.ingredients.select_related("stock_item", "unit"):
            available = StockLevelService.get_available(ing.stock_item_id, po.source_location_id)
            required = ing.planned_quantity

            is_available = available >= required
            if not is_available:
                all_available = False

            availability.append({
                "stock_item_id": ing.stock_item_id,
                "stock_item_name": ing.stock_item.name,
                "required": str(required),
                "available": str(available),
                "shortage": str(max(Decimal("0"), required - available)),
                "is_available": is_available,
            })

        return ServiceResponse.success(data={
            "all_available": all_available,
            "ingredients": availability
        })

    @classmethod
    @transaction.atomic
    def _allocate_ingredients(cls, po_id: int):
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return

        from .level_service import StockLevelService

        for ing in po.ingredients.filter(status=ProductionOrderIngredient.IngredientStatus.PENDING):
            result, status = StockLevelService.reserve(
                stock_item_id=ing.stock_item_id,
                location_id=po.source_location_id,
                quantity=ing.planned_quantity,
                user_id=po.created_by_id,
                reference_type="ProductionOrder",
                reference_id=po_id,
                notes=f"Reserved for production: {po.order_number}"
            )

            # If the reservation failed, do NOT flip the ingredient to ALLOCATED
            # (that would lie about stock being held). Raise so the caller's
            # transaction rolls back the whole allocation, consistent with the
            # _ProductionStepError pattern used by complete()'s helpers.
            if status >= 400:
                raise _ProductionStepError(result, status)

            ing.status = ProductionOrderIngredient.IngredientStatus.ALLOCATED
            ing.save(update_fields=["status"])

    @classmethod
    @transaction.atomic
    def _release_ingredients(cls, po_id: int):
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return

        from .level_service import StockLevelService

        for ing in po.ingredients.filter(status=ProductionOrderIngredient.IngredientStatus.ALLOCATED):
            result, status = StockLevelService.release_reservation(
                stock_item_id=ing.stock_item_id,
                location_id=po.source_location_id,
                quantity=ing.planned_quantity,
                user_id=po.created_by_id,
                notes=f"Released from cancelled production: {po.order_number}"
            )

            # If the release failed, do NOT flip the ingredient back to PENDING
            # (the reservation is still held). Raise so the caller's transaction
            # rolls back rather than silently leaking the reservation.
            if status >= 400:
                raise _ProductionStepError(result, status)

            ing.status = ProductionOrderIngredient.IngredientStatus.PENDING
            ing.save(update_fields=["status"])

    @classmethod
    def _consume_ingredients(cls, po_id: int, user_id: int):
        # No own @transaction.atomic — runs inside complete()'s transaction and
        # signals failure by raising _ProductionStepError so the whole thing
        # rolls back together (see complete()).
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return

        settings = StockSettings.load()

        from .level_service import StockLevelService

        for ing in po.ingredients.select_related("stock_item"):
            actual_qty = ing.actual_quantity or ing.planned_quantity

            # Release any reservation _allocate_ingredients created before
            # we deduct quantity. Without this, reserved_quantity climbs
            # by `planned_quantity` per consumed ingredient and never comes
            # back, so available = quantity - reserved eventually returns
            # zero on healthy stock and blocks future production.
            if ing.status == ProductionOrderIngredient.IngredientStatus.ALLOCATED:
                rel_result, rel_status = StockLevelService.release_reservation(
                    stock_item_id=ing.stock_item_id,
                    location_id=po.source_location_id,
                    quantity=ing.planned_quantity,
                    user_id=user_id,
                    notes=f"Released for consumption: {po.order_number}",
                )
                # Surface a failed release so complete()'s atomic rolls back
                # instead of double-counting the reservation against the deduction.
                if rel_status >= 400:
                    raise _ProductionStepError(rel_result, rel_status)

            if settings.track_batches or ing.stock_item.track_batches:
                from .batch_service import StockBatchService
                result, status = StockBatchService.auto_consume(
                    stock_item_id=ing.stock_item_id,
                    location_id=po.source_location_id,
                    quantity=actual_qty,
                    movement_type="PRODUCTION_OUT",
                    user_id=user_id,
                    reference_type="ProductionOrder",
                    reference_id=po_id,
                    notes=f"Production: {po.order_number}"
                )
                if status >= 400:
                    raise _ProductionStepError(result, status)

                if result.get("data", {}).get("batches"):
                    first_batch = result["data"]["batches"][0]
                    ing.batch_used_id = first_batch["batch_id"]
            else:
                result, status = StockLevelService.adjust(
                    stock_item_id=ing.stock_item_id,
                    location_id=po.source_location_id,
                    quantity=-actual_qty,
                    movement_type="PRODUCTION_OUT",
                    user_id=user_id,
                    production_order_id=po_id,
                    notes=f"Production: {po.order_number}"
                )
                if status >= 400:
                    raise _ProductionStepError(result, status)

            if ing.actual_quantity:
                ing.variance = ing.actual_quantity - ing.planned_quantity

            ing.status = ProductionOrderIngredient.IngredientStatus.CONSUMED
            ing.save(update_fields=["status", "batch_used", "variance"])

    @classmethod
    def _create_output(cls, po_id: int, quantity: Decimal, user_id: int, quality_status: str):
        # No own @transaction.atomic — runs inside complete()'s transaction and
        # raises _ProductionStepError on failure (see complete()).
        po = ProductionOrderRepository.get_by_id(po_id)
        if not po:
            return

        settings = StockSettings.load()

        total_cost = sum(
            (ing.actual_quantity or ing.planned_quantity) * ing.stock_item.avg_cost_price
            for ing in po.ingredients.select_related("stock_item")
        )
        unit_cost = total_cost / quantity if quantity > 0 else Decimal("0")

        batch = None
        if settings.track_batches or po.recipe.output_item.track_batches:
            from .batch_service import StockBatchService
            batch_result, batch_status = StockBatchService.create(
                stock_item_id=po.recipe.output_item_id,
                location_id=po.output_location_id,
                quantity=quantity,
                unit_cost=unit_cost,
                production_order_id=po_id,
                quality_status=quality_status,
            )
            if batch_status >= 400:
                raise _ProductionStepError(batch_result, batch_status)
            batch = StockBatch.objects.get(id=batch_result["data"]["id"])

        from .level_service import StockLevelService
        result, status = StockLevelService.adjust(
            stock_item_id=po.recipe.output_item_id,
            location_id=po.output_location_id,
            quantity=quantity,
            movement_type="PRODUCTION_IN",
            user_id=user_id,
            batch_id=batch.id if batch else None,
            production_order_id=po_id,
            unit_cost=unit_cost,
            notes=f"Production output: {po.order_number}"
        )
        if status >= 400:
            raise _ProductionStepError(result, status)

        ProductionOrderOutputRepository.create(
            production_order=po,
            stock_item=po.recipe.output_item,
            quantity=quantity,
            unit=po.output_unit,
            is_primary_output=True,
            batch_created=batch,
            quality_status=quality_status,
        )

        for bp in po.recipe.by_products.select_related("stock_item", "unit"):
            bp_qty = bp.expected_quantity * po.batch_multiplier

            ProductionOrderOutputRepository.create(
                production_order=po,
                stock_item=bp.stock_item,
                quantity=bp_qty,
                unit=bp.unit,
                is_primary_output=False,
                is_byproduct=True,
                is_waste=bp.is_waste,
            )

            if not bp.is_waste:
                bp_result, bp_status = StockLevelService.adjust(
                    stock_item_id=bp.stock_item_id,
                    location_id=po.output_location_id,
                    quantity=bp_qty,
                    movement_type="PRODUCTION_IN",
                    user_id=user_id,
                    production_order_id=po_id,
                    notes=f"By-product from: {po.order_number}"
                )
                if bp_status >= 400:
                    raise _ProductionStepError(bp_result, bp_status)


class ProductionOrderIngredientService:

    @classmethod
    def serialize(cls, ing: ProductionOrderIngredient) -> Dict[str, Any]:
        return {
            "id": ing.id,
            "uuid": str(ing.uuid),
            "production_order_id": ing.production_order_id,
            "recipe_ingredient_id": ing.recipe_ingredient_id,
            "stock_item_id": ing.stock_item_id,
            "stock_item_name": ing.stock_item.name,
            "planned_quantity": str(ing.planned_quantity),
            "actual_quantity": str(ing.actual_quantity) if ing.actual_quantity else None,
            "unit": ing.unit.short_name,
            "batch_used_id": ing.batch_used_id,
            "variance": str(ing.variance) if ing.variance else None,
            "variance_reason": ing.variance_reason,
            "status": ing.status,
            "status_display": ing.get_status_display(),
        }

    @classmethod
    @transaction.atomic
    def record_actual(cls,
                      ingredient_id: int,
                      actual_quantity: Decimal,
                      batch_id: int = None,
                      variance_reason: str = "") -> Tuple[Dict[str, Any], int]:
        ing = ProductionOrderIngredientRepository.get_by_id(ingredient_id)
        if not ing:
            return ServiceResponse.not_found(f"Ingredient with id {ingredient_id} not found")

        # Need the production_order relation
        ing = ProductionOrderIngredient.objects.select_related("production_order").get(id=ingredient_id)

        if ing.production_order.status != ProductionOrder.Status.IN_PROGRESS:
            return ServiceResponse.error("Can only record actuals for in-progress orders")

        ing.actual_quantity = to_decimal(actual_quantity)
        ing.variance = ing.actual_quantity - ing.planned_quantity
        ing.variance_reason = variance_reason

        if batch_id:
            ing.batch_used_id = batch_id

        ing.save(update_fields=["actual_quantity", "variance", "variance_reason", "batch_used"])

        return ServiceResponse.success(data={
            "ingredient": cls.serialize(ing)
        }, message="Actual quantity recorded")


class ProductionOrderOutputService:

    @classmethod
    def serialize(cls, out: ProductionOrderOutput) -> Dict[str, Any]:
        return {
            "id": out.id,
            "uuid": str(out.uuid),
            "production_order_id": out.production_order_id,
            "stock_item_id": out.stock_item_id,
            "stock_item_name": out.stock_item.name,
            "quantity": str(out.quantity),
            "unit": out.unit.short_name,
            "is_primary_output": out.is_primary_output,
            "is_byproduct": out.is_byproduct,
            "is_waste": out.is_waste,
            "batch_created_id": out.batch_created_id,
            "quality_status": out.quality_status,
            "quality_notes": out.quality_notes,
        }


class ProductionOrderStepService:

    @classmethod
    def serialize(cls, step: ProductionOrderStep) -> Dict[str, Any]:
        return {
            "id": step.id,
            "uuid": str(step.uuid),
            "production_order_id": step.production_order_id,
            "recipe_step_id": step.recipe_step_id,
            "step_number": step.recipe_step.step_number,
            "title": step.recipe_step.title,
            "description": step.recipe_step.description,
            "duration_minutes": step.recipe_step.duration_minutes,
            "temperature": step.recipe_step.temperature,
            "equipment_needed": step.recipe_step.equipment_needed,
            "is_checkpoint": step.recipe_step.is_checkpoint,
            "status": step.status,
            "status_display": step.get_status_display(),
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "completed_by_id": step.completed_by_id,
            "notes": step.notes,
            "checkpoint_passed": step.checkpoint_passed,
        }

    @classmethod
    @transaction.atomic
    def start(cls, step_id: int) -> Tuple[Dict[str, Any], int]:
        step = ProductionOrderStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found(f"Step with id {step_id} not found")

        # Need production_order relation
        step = ProductionOrderStep.objects.select_related("production_order").get(id=step_id)

        if step.production_order.status != ProductionOrder.Status.IN_PROGRESS:
            return ServiceResponse.error("Production must be in progress")

        if step.status != ProductionOrderStep.StepStatus.PENDING:
            return ServiceResponse.error(f"Step is already {step.status}")

        step.status = ProductionOrderStep.StepStatus.IN_PROGRESS
        step.started_at = timezone.now()
        step.save(update_fields=["status", "started_at"])

        return ServiceResponse.success(data={
            "step": cls.serialize(step)
        }, message="Step started")

    @classmethod
    @transaction.atomic
    def complete(cls, step_id: int,
                 completed_by_id: int,
                 checkpoint_passed: bool = None,
                 notes: str = "") -> Tuple[Dict[str, Any], int]:
        step = ProductionOrderStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found(f"Step with id {step_id} not found")

        # Need production_order and recipe_step relations
        step = ProductionOrderStep.objects.select_related("production_order", "recipe_step").get(id=step_id)

        if step.status not in [ProductionOrderStep.StepStatus.PENDING, ProductionOrderStep.StepStatus.IN_PROGRESS]:
            return ServiceResponse.error(f"Cannot complete step in {step.status} status")

        if step.recipe_step.is_checkpoint and checkpoint_passed is None:
            return ServiceResponse.validation_error(
                errors={"checkpoint_passed": "Checkpoint steps require checkpoint_passed value"}
            )

        step.status = ProductionOrderStep.StepStatus.COMPLETED
        step.completed_at = timezone.now()
        step.completed_by_id = completed_by_id
        step.checkpoint_passed = checkpoint_passed
        step.notes = notes

        if not step.started_at:
            step.started_at = step.completed_at

        step.save(update_fields=["status", "completed_at", "completed_by", "checkpoint_passed", "notes", "started_at"])

        return ServiceResponse.success(data={
            "step": cls.serialize(step)
        }, message="Step completed")

    @classmethod
    @transaction.atomic
    def skip(cls, step_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        step = ProductionOrderStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found(f"Step with id {step_id} not found")

        # Need recipe_step relation
        step = ProductionOrderStep.objects.select_related("recipe_step").get(id=step_id)

        if step.recipe_step.is_checkpoint:
            return ServiceResponse.error("Cannot skip checkpoint steps")

        step.status = ProductionOrderStep.StepStatus.SKIPPED
        step.notes = reason
        step.save(update_fields=["status", "notes"])

        return ServiceResponse.success(data={
            "step": cls.serialize(step)
        }, message="Step skipped")
