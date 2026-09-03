from typing import Dict, Any, Tuple
from decimal import Decimal
from datetime import date
from django.db import transaction
from django.db.models import Sum, F, Count
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.money import MoneyValueError, decimal_value, uzs_int
from stock.models import (
    StockBatch, StockItem, StockLevel, StockLocation, StockTransaction,
    StockSettings,
)
from stock.services.base_service import to_decimal, generate_number
from stock.repositories import (
    StockLevelRepository, StockTransactionRepository,
    StockItemRepository,
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
    def get_level_for_update(cls, stock_item_id: int, location_id: int,
                             branch_id: str = None) -> StockLevel:
        # Row-level lock — must be called inside a @transaction.atomic block.
        return StockLevelRepository.get_or_create_level_for_update(
            stock_item_id, location_id, branch_id=branch_id,
        )

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
               order_item_id: int = None,
               production_order_id: int = None,
               transfer_id: int = None,
               unit_cost: Decimal = None,
               notes: str = "",
               branch_id: str = None,
               action_id=None,
               idempotency_key: str = '',
               reversal_of_id: int = None,
               allowed_movement_types=None,
               strict: bool = False) -> Tuple[Dict[str, Any], int]:
        if action_id:
            existing = StockTransaction.objects.filter(
                command_id=action_id,
                **({'branch_id': branch_id} if branch_id else {}),
            ).first()
            if existing:
                return cls._adjustment_result(existing)
        settings = StockSettings.load()

        if not settings.stock_enabled:
            if strict:
                return ServiceResponse.conflict(
                    'STOCK_SYSTEM_DISABLED',
                    'Stock adjustment is unavailable while stock is disabled.',
                )
            return ServiceResponse.success(data={
                "skipped": True,
                "reason": "Stock system disabled"
            }, message="Stock adjustment skipped (system disabled)")

        valid_types = [c[0] for c in StockTransaction.MovementType.choices]
        if movement_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"movement_type": f"Invalid movement type. Valid: {valid_types}"}
            )
        if allowed_movement_types and movement_type not in allowed_movement_types:
            return ServiceResponse.failure(
                'STOCK_ADJUSTMENT_TYPE_FORBIDDEN',
                'This movement type is not permitted by the adjustment endpoint.',
                422,
                errors={'movement_type': ['Use an approved adjustment or waste type.']},
            )

        item_qs = StockItem.objects.select_for_update().filter(
            pk=stock_item_id, is_deleted=False, is_active=True,
        )
        if branch_id:
            item_qs = item_qs.filter(branch_id=branch_id)
        stock_item = item_qs.first()
        if not stock_item:
            if branch_id:
                return ServiceResponse.failure(
                    'STOCK_SCOPE_FORBIDDEN',
                    'Stock item is outside the authorized branch.',
                    403,
                )
            return ServiceResponse.not_found(
                f"Stock item with id {stock_item_id} not found"
            )

        location_qs = StockLocation.objects.select_for_update().filter(
            pk=location_id, is_deleted=False, is_active=True,
        )
        if branch_id:
            location_qs = location_qs.filter(branch_id=branch_id)
        location = location_qs.first()
        if not location:
            if branch_id:
                return ServiceResponse.failure(
                    'STOCK_SCOPE_FORBIDDEN',
                    'Stock location is outside the authorized branch.',
                    403,
                )
            return ServiceResponse.not_found(
                f"Location with id {location_id} not found"
            )
        if stock_item.branch_id != location.branch_id:
            return ServiceResponse.failure(
                'STOCK_SCOPE_FORBIDDEN',
                'Stock item and location must belong to the same branch.',
                403,
            )

        if unit_id:
            unit = StockUnitRepository.get_by_id(unit_id)
            if not unit:
                return ServiceResponse.not_found(f"Unit with id {unit_id} not found")
            if strict and unit_id != stock_item.base_unit_id:
                from stock.models import StockItemUnit

                if not StockItemUnit.objects.filter(
                    stock_item=stock_item,
                    unit_id=unit_id,
                    is_deleted=False,
                ).exists():
                    return ServiceResponse.validation_error({
                        'unit_id': ['Unit is not configured for this stock item.'],
                    })
        else:
            unit = stock_item.base_unit

        if strict:
            try:
                quantity = decimal_value(
                    quantity, 'quantity', places=4, positive=True,
                    maximum=Decimal('99999999999'),
                )
            except MoneyValueError as exc:
                return ServiceResponse.validation_error(
                    errors={'quantity': [str(exc)]},
                )
        else:
            quantity = to_decimal(quantity)

        if unit_id and unit_id != stock_item.base_unit_id:
            from .unit_service import StockItemUnitService
            base_quantity = StockItemUnitService.convert_for_item(
                stock_item_id, quantity, unit_id
            )
        else:
            base_quantity = quantity
        if strict and (
            not base_quantity.is_finite()
            or base_quantity <= 0
            or base_quantity > Decimal('99999999999.9999')
        ):
            return ServiceResponse.validation_error({
                'quantity': ['Converted quantity is outside the supported range.'],
            })

        batch = None
        if strict and batch_id:
            batch = StockBatch.objects.select_for_update().filter(
                pk=batch_id,
                stock_item=stock_item,
                location=location,
                branch_id=location.branch_id,
                is_deleted=False,
            ).first()
            if batch is None:
                return ServiceResponse.failure(
                    'STOCK_BATCH_SCOPE_INVALID',
                    'Batch must belong to the selected item and location.',
                    422,
                    errors={'batch_id': ['Batch does not match this stock scope.']},
                )
        elif strict and stock_item.track_batches:
            return ServiceResponse.validation_error(
                errors={'batch_id': ['A batch is required for this tracked item.']},
            )

        level = cls.get_level_for_update(
            stock_item_id, location_id, branch_id=location.branch_id,
        )
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
        if new_quantity < 0 and (strict or not settings.allow_negative_stock):
            if strict:
                return ServiceResponse.failure(
                    'INSUFFICIENT_STOCK',
                    'The selected location has insufficient stock.',
                    422,
                    details={
                        'available_quantity': str(level.quantity),
                        'required_quantity': str(abs(adjustment)),
                    },
                )
            return ServiceResponse.error(
                f"Insufficient stock for {stock_item.name}: "
                f"required {abs(adjustment)}, available {level.quantity}"
            )

        if batch is not None:
            new_batch_quantity = batch.current_quantity + adjustment
            if new_batch_quantity < 0:
                return ServiceResponse.failure(
                    'INSUFFICIENT_STOCK',
                    'The selected batch has insufficient stock.',
                    422,
                    details={
                        'available_quantity': str(batch.current_quantity),
                        'required_quantity': str(abs(adjustment)),
                    },
                )
            batch.current_quantity = new_batch_quantity
            batch.save(update_fields=['current_quantity', 'updated_at'])

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
            order_item_id=order_item_id,
            production_order_id=production_order_id,
            transfer_id=transfer_id,
            user_id=user_id,
            notes=notes,
            branch_id=location.branch_id,
            command_id=action_id,
            idempotency_key=idempotency_key or '',
            actor_display_snapshot=cls._actor_display(user_id),
            reversal_of_id=reversal_of_id,
        )

        return cls._adjustment_result(trans)

    @staticmethod
    def _actor_display(user_id):
        from base.models import User

        actor = User.objects.filter(pk=user_id).values(
            'first_name', 'last_name',
        ).first()
        if not actor:
            return ''
        return f"{actor['first_name']} {actor['last_name']}".strip()

    @classmethod
    def _adjustment_result(cls, trans):
        adjustment = trans.quantity_after - trans.quantity_before
        return ServiceResponse.success(data={
            "transaction_id": trans.id,
            "transaction_number": trans.transaction_number,
            "quantity_before": str(trans.quantity_before),
            "quantity_after": str(trans.quantity_after),
            "adjustment": str(adjustment),
            "movement_type": trans.movement_type,
            "unit_cost": str(trans.unit_cost),
            "total_cost_uzs": uzs_int(trans.total_cost),
            "reversal_of_transaction_id": trans.reversal_of_id,
        }, message=f"Stock adjusted: {adjustment:+}")

    @classmethod
    @transaction.atomic
    def reverse_adjustment(cls, transaction_id, *, actor, branch_id,
                           reason, action_id=None, idempotency_key=''):
        if not str(reason or '').strip():
            return ServiceResponse.validation_error(
                errors={'reason': ['A reversal reason is required.']},
            )
        if action_id:
            existing = StockTransaction.objects.filter(
                command_id=action_id, branch_id=branch_id,
            ).first()
            if existing:
                return cls._adjustment_result(existing)
        original = StockTransaction.objects.select_for_update().filter(
            pk=transaction_id,
            branch_id=branch_id,
            is_deleted=False,
            reversal_of__isnull=True,
            reference_type__in=['StockAdjustment', 'StockWaste'],
        ).first()
        if original is None:
            return ServiceResponse.not_found('Stock adjustment not found')
        existing_reversal = StockTransaction.objects.filter(
            reversal_of=original,
        ).first()
        if existing_reversal:
            return ServiceResponse.conflict(
                'STOCK_ADJUSTMENT_ALREADY_REVERSED',
                'This stock adjustment has already been reversed.',
                details={'reversal_transaction_id': existing_reversal.id},
            )
        outgoing = original.movement_type in {
            StockTransaction.MovementType.ADJUSTMENT_MINUS,
            StockTransaction.MovementType.WASTE,
            StockTransaction.MovementType.SPOILAGE,
        }
        reverse_type = (
            StockTransaction.MovementType.ADJUSTMENT_PLUS
            if outgoing else StockTransaction.MovementType.ADJUSTMENT_MINUS
        )
        return cls.adjust(
            stock_item_id=original.stock_item_id,
            location_id=original.location_id,
            quantity=original.quantity,
            movement_type=reverse_type,
            user_id=actor.id,
            unit_id=original.unit_id,
            batch_id=original.batch_id,
            reference_type='StockAdjustmentReversal',
            reference_id=original.id,
            unit_cost=original.unit_cost,
            notes=str(reason).strip(),
            branch_id=branch_id,
            action_id=action_id,
            idempotency_key=idempotency_key,
            reversal_of_id=original.id,
            allowed_movement_types={
                StockTransaction.MovementType.ADJUSTMENT_PLUS,
                StockTransaction.MovementType.ADJUSTMENT_MINUS,
            },
            strict=True,
        )

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
