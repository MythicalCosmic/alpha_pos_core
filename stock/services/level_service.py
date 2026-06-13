from typing import Dict, Any, Tuple
from decimal import Decimal
from datetime import date
from django.db import transaction
from django.db.models import Sum, F, Count
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import (
    StockLevel, StockTransaction, StockSettings
)
from stock.services.base_service import to_decimal, generate_number
from stock.repositories import (
    StockLevelRepository, StockTransactionRepository,
    StockItemRepository, StockLocationRepository,
    StockUnitRepository,
)


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class StockLevelService:

    @classmethod
    def serialize(cls, level: StockLevel) -> Dict[str, Any]:
        return {
            "id": level.id,
            "uuid": str(level.uuid),
            "stock_item_id": level.stock_item_id,
            "stock_item": {
                "id": level.stock_item.id,
                "name": level.stock_item.name,
                "sku": level.stock_item.sku,
                "unit": level.stock_item.base_unit.short_name,
            },
            "location_id": level.location_id,
            "location": {
                "id": level.location.id,
                "name": level.location.name,
                "type": level.location.type,
            },
            "quantity": str(level.quantity),
            "reserved_quantity": str(level.reserved_quantity),
            "available_quantity": str(level.available_quantity),
            "pending_in_quantity": str(level.pending_in_quantity),
            "pending_out_quantity": str(level.pending_out_quantity),
            "last_counted_at": level.last_counted_at.isoformat() if level.last_counted_at else None,
            "last_restocked_at": level.last_restocked_at.isoformat() if level.last_restocked_at else None,
            "last_movement_at": level.last_movement_at.isoformat() if level.last_movement_at else None,
        }

    @classmethod
    def get_all(cls,
                location_id: int = None,
                category_id: int = None,
                item_type: str = None,
                low_stock_only: bool = False,
                page: int = 1,
                search: str = None,
                per_page: int = 50) -> Tuple[Dict[str, Any], int]:
        queryset = StockLevelRepository.get_all().select_related(
            "stock_item", "stock_item__base_unit", "stock_item__category", "location"
        ).filter(stock_item__is_active=True)

        if location_id:
            queryset = queryset.filter(location_id=location_id)

        if category_id:
            queryset = queryset.filter(stock_item__category_id=category_id)

        if item_type:
            queryset = queryset.filter(stock_item__item_type=item_type)

        if low_stock_only:
            queryset = queryset.filter(
                quantity__lt=F("stock_item__reorder_point")
            )

        queryset = queryset.order_by("stock_item__name", "location__name")

        page_obj, paginator = StockLevelRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "levels": [cls.serialize(lvl) for lvl in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
        })

    @classmethod
    def get_for_item(cls, stock_item_id: int) -> Tuple[Dict[str, Any], int]:
        levels = StockLevelRepository.get_for_item(stock_item_id).order_by("location__name")

        total = levels.aggregate(
            total_qty=Sum("quantity"),
            total_reserved=Sum("reserved_quantity")
        )

        return ServiceResponse.success(data={
            "levels": [cls.serialize(lvl) for lvl in levels],
            "total_quantity": str(total["total_qty"] or 0),
            "total_reserved": str(total["total_reserved"] or 0),
            "total_available": str((total["total_qty"] or 0) - (total["total_reserved"] or 0))
        })

    @classmethod
    def get_for_location(cls, location_id: int) -> Tuple[Dict[str, Any], int]:
        levels = StockLevelRepository.get_for_location(location_id).filter(
            stock_item__is_active=True
        ).select_related(
            "stock_item", "stock_item__base_unit"
        ).order_by("stock_item__name")

        return ServiceResponse.success(data={
            "levels": [cls.serialize(lvl) for lvl in levels],
            "count": levels.count()
        })

    @classmethod
    def get_level(cls, stock_item_id: int, location_id: int) -> StockLevel:
        return StockLevelRepository.get_or_create_level(stock_item_id, location_id)

    @classmethod
    def get_level_for_update(cls, stock_item_id: int, location_id: int) -> StockLevel:
        # Row-level lock — must be called inside a @transaction.atomic block.
        return StockLevelRepository.get_or_create_level_for_update(stock_item_id, location_id)

    @classmethod
    def get_available(cls, stock_item_id: int, location_id: int = None) -> Decimal:
        if location_id:
            level = StockLevelRepository.get_for_item_and_location(stock_item_id, location_id)
            if level:
                return level.quantity - level.reserved_quantity
            return Decimal("0")

        qs = StockLevelRepository.get_for_item(stock_item_id)
        result = qs.aggregate(
            total=Sum("quantity"),
            reserved=Sum("reserved_quantity")
        )

        total = result["total"] or Decimal("0")
        reserved = result["reserved"] or Decimal("0")

        return total - reserved

    @classmethod
    def get_low_stock_items(cls, location_id: int = None) -> Tuple[Dict[str, Any], int]:
        alerts = []

        if location_id:
            low_stock = StockLevelRepository.get_low_stock().filter(
                location_id=location_id
            )
            for level in low_stock:
                alerts.append({
                    "stock_item_id": level.stock_item_id,
                    "stock_item_name": level.stock_item.name,
                    "sku": level.stock_item.sku,
                    "location_id": level.location_id,
                    "location_name": level.location.name,
                    "current_quantity": str(level.quantity),
                    "reorder_point": str(level.stock_item.reorder_point),
                    "shortage": str(level.stock_item.reorder_point - level.quantity),
                })
        else:
            low_stock_items = StockItemRepository.get_low_stock()
            for item in low_stock_items:
                total_qty = item.total_qty or Decimal("0")
                alerts.append({
                    "stock_item_id": item.id,
                    "stock_item_name": item.name,
                    "sku": item.sku,
                    "current_quantity": str(total_qty),
                    "reorder_point": str(item.reorder_point),
                    "shortage": str(item.reorder_point - total_qty),
                })

        return ServiceResponse.success(data={
            "alerts": alerts,
            "count": len(alerts)
        })

    @classmethod
    @transaction.atomic
    def adjust(cls,
               stock_item_id: int,
               location_id: int,
               quantity: Decimal,
               movement_type: str,
               user_id: int,
               unit_id: int = None,
               batch_id: int = None,
               reference_type: str = None,
               reference_id: int = None,
               order_id: int = None,
               production_order_id: int = None,
               transfer_id: int = None,
               unit_cost: Decimal = None,
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        settings = StockSettings.load()

        if not settings.stock_enabled:
            return ServiceResponse.success(data={
                "skipped": True,
                "reason": "Stock system disabled"
            }, message="Stock adjustment skipped (system disabled)")

        valid_types = [c[0] for c in StockTransaction.MovementType.choices]
        if movement_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"movement_type": f"Invalid movement type. Valid: {valid_types}"}
            )

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found(f"Stock item with id {stock_item_id} not found")

        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found(f"Location with id {location_id} not found")

        if unit_id:
            unit = StockUnitRepository.get_by_id(unit_id)
            if not unit:
                return ServiceResponse.not_found(f"Unit with id {unit_id} not found")
        else:
            unit = stock_item.base_unit

        quantity = to_decimal(quantity)

        if unit_id and unit_id != stock_item.base_unit_id:
            from .unit_service import StockItemUnitService
            base_quantity = StockItemUnitService.convert_for_item(
                stock_item_id, quantity, unit_id
            )
        else:
            base_quantity = quantity

        level = cls.get_level_for_update(stock_item_id, location_id)
        quantity_before = level.quantity

        signed_movement_types = {"COUNT_ADJUSTMENT", "ADJUSTMENT"}
        is_outgoing = movement_type in [
            "SALE_OUT", "TRANSFER_OUT", "PRODUCTION_OUT",
            "ADJUSTMENT_MINUS", "WASTE", "SPOILAGE", "RETURN_TO_SUPPLIER"
        ]

        if movement_type in signed_movement_types:
            adjustment = base_quantity
        elif is_outgoing:
            adjustment = -abs(base_quantity)
        else:
            adjustment = abs(base_quantity)

        new_quantity = level.quantity + adjustment
        if new_quantity < 0 and not settings.allow_negative_stock:
            return ServiceResponse.error(
                f"Insufficient stock for {stock_item.name}: "
                f"required {abs(adjustment)}, available {level.quantity}"
            )

        level.quantity = new_quantity
        level.last_movement_at = timezone.now()

        if not is_outgoing:
            level.last_restocked_at = timezone.now()

        level.save(update_fields=["quantity", "last_movement_at", "last_restocked_at", "updated_at"])

        if unit_cost is None:
            unit_cost = stock_item.avg_cost_price

        trans_number = generate_number("TRX", StockTransaction, "transaction_number")

        trans = StockTransactionRepository.create(
            transaction_number=trans_number,
            stock_item=stock_item,
            location=location,
            batch_id=batch_id,
            movement_type=movement_type,
            quantity=abs(quantity),
            unit=unit,
            base_quantity=abs(base_quantity),
            quantity_before=quantity_before,
            quantity_after=new_quantity,
            unit_cost=to_decimal(unit_cost),
            total_cost=abs(base_quantity) * to_decimal(unit_cost),
            reference_type=reference_type or "",
            reference_id=reference_id,
            order_id=order_id,
            production_order_id=production_order_id,
            transfer_id=transfer_id,
            user_id=user_id,
            notes=notes,
        )

        return ServiceResponse.success(data={
            "transaction_id": trans.id,
            "transaction_number": trans.transaction_number,
            "quantity_before": str(quantity_before),
            "quantity_after": str(new_quantity),
            "adjustment": str(adjustment),
            "movement_type": movement_type,
        }, message=f"Stock adjusted: {adjustment:+} {stock_item.base_unit.short_name}")

    @classmethod
    @transaction.atomic
    def reserve(cls,
                stock_item_id: int,
                location_id: int,
                quantity: Decimal,
                user_id: int,
                reference_type: str = None,
                reference_id: int = None,
                notes: str = "") -> Tuple[Dict[str, Any], int]:
        settings = StockSettings.load()
        if not settings.stock_enabled:
            return ServiceResponse.success(data={"skipped": True})

        quantity = abs(to_decimal(quantity))
        level = cls.get_level_for_update(stock_item_id, location_id)

        available = level.quantity - level.reserved_quantity
        if quantity > available:
            stock_item = StockItemRepository.get_by_id(stock_item_id)
            item_name = stock_item.name if stock_item else f"item {stock_item_id}"
            return ServiceResponse.error(
                f"Insufficient stock for {item_name}: required {quantity}, available {available}"
            )

        level.reserved_quantity += quantity
        level.save(update_fields=["reserved_quantity", "updated_at"])

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        trans_number = generate_number("TRX", StockTransaction, "transaction_number")

        StockTransactionRepository.create(
            transaction_number=trans_number,
            stock_item_id=stock_item_id,
            location_id=location_id,
            movement_type="RESERVATION",
            quantity=quantity,
            unit=stock_item.base_unit,
            base_quantity=quantity,
            quantity_before=level.quantity,
            quantity_after=level.quantity,
            user_id=user_id,
            reference_type=reference_type or "",
            reference_id=reference_id,
            notes=notes,
        )

        return ServiceResponse.success(data={
            "reserved": str(quantity),
            "total_reserved": str(level.reserved_quantity),
            "available": str(level.quantity - level.reserved_quantity)
        }, message="Stock reserved")

    @classmethod
    @transaction.atomic
    def release_reservation(cls,
                           stock_item_id: int,
                           location_id: int,
                           quantity: Decimal,
                           user_id: int,
                           notes: str = "",
                           reference_type: str = "",
                           reference_id: int = None) -> Tuple[Dict[str, Any], int]:
        settings = StockSettings.load()
        if not settings.stock_enabled:
            return ServiceResponse.success(data={"skipped": True})

        quantity = abs(to_decimal(quantity))
        level = cls.get_level_for_update(stock_item_id, location_id)

        # Refuse to silently swallow a release larger than what is reserved.
        # The prior min() cap masked double-releases and order-mismatched
        # releases; now the caller gets a clear error and can investigate.
        if quantity > level.reserved_quantity:
            return ServiceResponse.error(
                f"Cannot release {quantity}: only {level.reserved_quantity} reserved at this level"
            )
        release_qty = quantity

        level.reserved_quantity -= release_qty
        level.save(update_fields=["reserved_quantity", "updated_at"])

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        trans_number = generate_number("TRX", StockTransaction, "transaction_number")

        StockTransactionRepository.create(
            transaction_number=trans_number,
            stock_item_id=stock_item_id,
            location_id=location_id,
            movement_type="RESERVATION_RELEASE",
            quantity=release_qty,
            unit=stock_item.base_unit,
            base_quantity=release_qty,
            quantity_before=level.quantity,
            quantity_after=level.quantity,
            user_id=user_id,
            reference_type=reference_type or "",
            reference_id=reference_id,
            notes=notes,
        )

        return ServiceResponse.success(data={
            "released": str(release_qty),
            "remaining_reserved": str(level.reserved_quantity)
        }, message="Reservation released")


class StockTransactionService:

    @classmethod
    def serialize(cls, trans: StockTransaction) -> Dict[str, Any]:
        return {
            "id": trans.id,
            "uuid": str(trans.uuid),
            "transaction_number": trans.transaction_number,
            "stock_item_id": trans.stock_item_id,
            "stock_item_name": trans.stock_item.name,
            "location_id": trans.location_id,
            "location_name": trans.location.name,
            "batch_id": trans.batch_id,
            "movement_type": trans.movement_type,
            "movement_type_display": trans.get_movement_type_display(),
            "quantity": str(trans.quantity),
            "unit": trans.unit.short_name,
            "base_quantity": str(trans.base_quantity),
            "quantity_before": str(trans.quantity_before),
            "quantity_after": str(trans.quantity_after),
            "unit_cost": str(trans.unit_cost),
            "total_cost": str(trans.total_cost),
            "reference_type": trans.reference_type,
            "reference_id": trans.reference_id,
            "order_id": trans.order_id,
            "production_order_id": trans.production_order_id,
            "transfer_id": trans.transfer_id,
            "user_id": trans.user_id,
            "notes": trans.notes,
            "created_at": trans.created_at.isoformat(),
        }

    @classmethod
    def list(cls,
             stock_item_id: int = None,
             location_id: int = None,
             movement_type: str = None,
             date_from: date = None,
             date_to: date = None,
             order_id: int = None,
             production_order_id: int = None,
             transfer_id: int = None,
             page: int = 1,
             per_page: int = 50) -> Tuple[Dict[str, Any], int]:
        queryset = StockTransactionRepository.get_all().select_related(
            "stock_item", "location", "unit"
        )

        if stock_item_id:
            queryset = queryset.filter(stock_item_id=stock_item_id)

        if location_id:
            queryset = queryset.filter(location_id=location_id)

        if movement_type:
            queryset = queryset.filter(movement_type=movement_type)

        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        if order_id:
            queryset = queryset.filter(order_id=order_id)

        if production_order_id:
            queryset = queryset.filter(production_order_id=production_order_id)

        if transfer_id:
            queryset = queryset.filter(transfer_id=transfer_id)

        queryset = queryset.order_by("-created_at")

        page_obj, paginator = StockTransactionRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "transactions": [cls.serialize(t) for t in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "movement_types": [
                {"value": c[0], "label": c[1]}
                for c in StockTransaction.MovementType.choices
            ]
        })

    @classmethod
    def get_by_reference(cls, reference_type: str, reference_id: int) -> Tuple[Dict[str, Any], int]:
        transactions = StockTransactionRepository.get_by_reference(
            reference_type, reference_id
        ).select_related("stock_item", "location", "unit")

        return ServiceResponse.success(data={
            "transactions": [cls.serialize(t) for t in transactions],
            "count": transactions.count()
        })

    @classmethod
    def get_item_history(cls, stock_item_id: int, days: int = 30) -> Tuple[Dict[str, Any], int]:
        transactions = StockTransactionRepository.get_for_item(
            stock_item_id, days=days
        )

        summary = transactions.values("movement_type").annotate(
            count=Count("id"),
            total_qty=Sum("base_quantity")
        )

        return ServiceResponse.success(data={
            "transactions": [cls.serialize(t) for t in transactions[:100]],
            "summary": list(summary),
            "total_transactions": transactions.count(),
            "period_days": days
        })
