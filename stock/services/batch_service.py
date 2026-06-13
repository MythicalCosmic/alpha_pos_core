from typing import Dict, Any, Optional, List, Tuple
from decimal import Decimal
from datetime import date, timedelta
from django.db import transaction
from django.db.models import Sum, F
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import StockBatch, StockItem
from stock.services.base_service import to_decimal
from stock.repositories import (
    StockBatchRepository, StockItemRepository, StockLocationRepository,
    SupplierRepository, StockSettingsRepository,
)


class StockBatchService:

    @classmethod
    def serialize(cls, batch: StockBatch, include_transactions: bool = False) -> Dict[str, Any]:
        data = {
            "id": batch.id,
            "uuid": str(batch.uuid),
            "batch_number": batch.batch_number,

            "stock_item_id": batch.stock_item_id,
            "stock_item": {
                "id": batch.stock_item.id,
                "name": batch.stock_item.name,
                "sku": batch.stock_item.sku,
            },

            "location_id": batch.location_id,
            "location_name": batch.location.name,

            "initial_quantity": str(batch.initial_quantity),
            "current_quantity": str(batch.current_quantity),
            "reserved_quantity": str(batch.reserved_quantity),
            "available_quantity": str(batch.current_quantity - batch.reserved_quantity),

            "unit_cost": str(batch.unit_cost),
            "total_cost": str(batch.total_cost),

            "manufactured_date": batch.manufactured_date.isoformat() if batch.manufactured_date else None,
            "expiry_date": batch.expiry_date.isoformat() if batch.expiry_date else None,
            "days_until_expiry": cls._days_until_expiry(batch),
            "is_expired": cls._is_expired(batch),

            "supplier_id": batch.supplier_id,
            "supplier_name": batch.supplier.name if batch.supplier else None,
            "purchase_order_id": batch.purchase_order_id,
            "production_order_id": batch.production_order_id,

            "status": batch.status,
            "status_display": batch.get_status_display(),
            "quality_status": batch.quality_status,
            "notes": batch.notes,

            "received_at": batch.received_at.isoformat() if batch.received_at else None,
            "created_at": batch.created_at.isoformat(),
        }

        if include_transactions:
            transactions = batch.transactions.select_related("unit").order_by("-created_at")[:20]
            data["recent_transactions"] = [
                {
                    "id": t.id,
                    "movement_type": t.movement_type,
                    "quantity": str(t.quantity),
                    "created_at": t.created_at.isoformat(),
                }
                for t in transactions
            ]

        return data

    @classmethod
    def _days_until_expiry(cls, batch: StockBatch) -> Optional[int]:
        if not batch.expiry_date:
            return None
        today = timezone.now().date()
        delta = batch.expiry_date - today
        return delta.days

    @classmethod
    def _is_expired(cls, batch: StockBatch) -> bool:
        if not batch.expiry_date:
            return False
        return batch.expiry_date < timezone.now().date()

    @classmethod
    def list(cls,
             stock_item_id: int = None,
             location_id: int = None,
             status: str = None,
             expiring_within_days: int = None,
             expired_only: bool = False,
             has_stock_only: bool = True,
             page: int = 1,
             per_page: int = 50) -> Tuple[Dict[str, Any], int]:
        queryset = StockBatchRepository.get_all().select_related(
            "stock_item", "location", "supplier"
        )

        if stock_item_id:
            queryset = queryset.filter(stock_item_id=stock_item_id)

        if location_id:
            queryset = queryset.filter(location_id=location_id)

        if status:
            queryset = queryset.filter(status=status)

        if has_stock_only:
            queryset = queryset.filter(current_quantity__gt=0)

        if expired_only:
            queryset = queryset.filter(expiry_date__lt=timezone.now().date())
        elif expiring_within_days:
            expiry_threshold = timezone.now().date() + timedelta(days=expiring_within_days)
            queryset = queryset.filter(
                expiry_date__isnull=False,
                expiry_date__lte=expiry_threshold,
                expiry_date__gte=timezone.now().date()
            )

        queryset = queryset.order_by("expiry_date", "created_at")

        page_obj, paginator = StockBatchRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "batches": [cls.serialize(b) for b in page_obj.object_list],
            "pagination": {
                "current_page": page_obj.number,
                "total_pages": paginator.num_pages,
                "total_items": paginator.count,
                "per_page": per_page,
                "has_next": page_obj.has_next(),
                "has_previous": page_obj.has_previous(),
            },
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in StockBatch.BatchStatus.choices
            ]
        })

    @classmethod
    def get_available_batches(cls,
                              stock_item_id: int,
                              location_id: int,
                              costing_method: str = None) -> List[StockBatch]:
        settings = StockSettingsRepository.load()
        method = costing_method or settings.costing_method

        if method == "FIFO":
            queryset = StockBatchRepository.get_available_fifo(stock_item_id, location_id)
        elif method == "LIFO":
            queryset = StockBatchRepository.get_available_lifo(stock_item_id, location_id)
        elif method == "FEFO":
            queryset = StockBatchRepository.get_available_fefo(stock_item_id, location_id)
        else:
            queryset = StockBatchRepository.get_available_fifo(stock_item_id, location_id)

        # Exclude expired and filter for actually available quantity
        return list(
            queryset.exclude(
                expiry_date__lt=timezone.now().date()
            ).filter(
                current_quantity__gt=F("reserved_quantity")
            )
        )

    @classmethod
    def get_expiring_batches(cls, days: int = None) -> Tuple[Dict[str, Any], int]:
        settings = StockSettingsRepository.load()
        days = days or settings.expiry_alert_days

        batches = StockBatchRepository.get_expiring(days=days)

        return ServiceResponse.success(data={
            "batches": [cls.serialize(b) for b in batches],
            "count": batches.count(),
            "alert_days": days
        })

    @classmethod
    def get_expired_batches(cls) -> Tuple[Dict[str, Any], int]:
        batches = StockBatchRepository.get_expired()

        total_value = batches.aggregate(
            total=Sum(F("current_quantity") * F("unit_cost"))
        )["total"] or Decimal("0")

        return ServiceResponse.success(data={
            "batches": [cls.serialize(b) for b in batches],
            "count": batches.count(),
            "total_value": str(total_value)
        })

    @classmethod
    def get(cls, batch_id: int, include_transactions: bool = True) -> Tuple[Dict[str, Any], int]:
        batch = StockBatchRepository.get_with_relations(batch_id)

        if not batch:
            return ServiceResponse.not_found(f"Batch with id {batch_id} not found")

        return ServiceResponse.success(data={
            "batch": cls.serialize(batch, include_transactions=include_transactions)
        })

    @classmethod
    def find_by_number(cls, batch_number: str, stock_item_id: int = None) -> Tuple[Dict[str, Any], int]:
        batch = StockBatchRepository.get_by_batch_number(batch_number, stock_item_id=stock_item_id)

        if not batch:
            return ServiceResponse.not_found(f"Batch '{batch_number}' not found")

        return ServiceResponse.success(data={
            "batch": cls.serialize(batch)
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               stock_item_id: int,
               location_id: int,
               quantity: Decimal,
               unit_cost: Decimal = None,
               batch_number: str = None,
               manufactured_date: date = None,
               expiry_date: date = None,
               supplier_id: int = None,
               purchase_order_id: int = None,
               production_order_id: int = None,
               quality_status: str = "PASSED",
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found(f"Stock item with id {stock_item_id} not found")

        location = StockLocationRepository.first(id=location_id, is_active=True)
        if not location:
            return ServiceResponse.not_found(f"Location with id {location_id} not found")

        quantity = to_decimal(quantity)
        if quantity <= 0:
            return ServiceResponse.validation_error(
                errors={"quantity": "Quantity must be positive"}
            )

        if not batch_number:
            batch_number = cls._generate_batch_number(stock_item)

        if StockBatchRepository.batch_number_exists(batch_number, stock_item_id):
            return ServiceResponse.validation_error(
                errors={"batch_number": f"Batch number '{batch_number}' already exists for this item"}
            )

        if unit_cost is None:
            unit_cost = stock_item.avg_cost_price

        if not expiry_date and stock_item.track_expiry and stock_item.default_expiry_days:
            manufactured = manufactured_date or timezone.now().date()
            expiry_date = manufactured + timedelta(days=stock_item.default_expiry_days)

        supplier = None
        if supplier_id:
            supplier = SupplierRepository.get_by_id(supplier_id)
            if not supplier:
                return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        batch = StockBatchRepository.create(
            batch_number=batch_number,
            stock_item=stock_item,
            location=location,
            initial_quantity=quantity,
            current_quantity=quantity,
            unit_cost=to_decimal(unit_cost),
            total_cost=quantity * to_decimal(unit_cost),
            manufactured_date=manufactured_date,
            expiry_date=expiry_date,
            supplier=supplier,
            purchase_order_id=purchase_order_id,
            production_order_id=production_order_id,
            quality_status=quality_status,
            notes=notes,
            status=StockBatch.BatchStatus.AVAILABLE,
            received_at=timezone.now(),
        )

        return ServiceResponse.created(data={
            "id": batch.id,
            "batch_number": batch.batch_number,
            "batch": cls.serialize(batch)
        }, message=f"Batch '{batch_number}' created")

    @classmethod
    def _generate_batch_number(cls, stock_item: StockItem) -> str:
        today = timezone.now()
        prefix = f"B{today.strftime('%y%m%d')}"
        count = StockBatchRepository.count(
            stock_item=stock_item,
            batch_number__startswith=prefix
        )

        return f"{prefix}-{stock_item.sku or stock_item.id}-{count + 1:03d}"

    @classmethod
    @transaction.atomic
    def update(cls, batch_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        batch = StockBatchRepository.get_by_id(batch_id)
        if not batch:
            return ServiceResponse.not_found(f"Batch with id {batch_id} not found")

        update_fields = ["updated_at"]
        for field in ["manufactured_date", "expiry_date", "quality_status", "notes"]:
            if field in kwargs:
                setattr(batch, field, kwargs[field])
                update_fields.append(field)

        batch.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "batch": cls.serialize(batch)
        }, message="Batch updated")

    @classmethod
    @transaction.atomic
    def set_status(cls, batch_id: int, status: str, notes: str = "") -> Tuple[Dict[str, Any], int]:
        batch = StockBatchRepository.get_by_id(batch_id)
        if not batch:
            return ServiceResponse.not_found(f"Batch with id {batch_id} not found")

        valid_statuses = [c[0] for c in StockBatch.BatchStatus.choices]
        if status not in valid_statuses:
            return ServiceResponse.validation_error(
                errors={"status": f"Invalid status. Valid: {valid_statuses}"}
            )

        old_status = batch.status
        batch.status = status

        if notes:
            batch.notes = f"{batch.notes}\n{timezone.now().isoformat()}: {old_status} -> {status}: {notes}".strip()

        batch.save(update_fields=["status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "batch": cls.serialize(batch),
            "old_status": old_status,
            "new_status": status
        }, message=f"Batch status changed to {status}")

    @classmethod
    @transaction.atomic
    def quarantine(cls, batch_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        return cls.set_status(batch_id, "QUARANTINE", reason)

    @classmethod
    @transaction.atomic
    def release_from_quarantine(cls, batch_id: int) -> Tuple[Dict[str, Any], int]:
        return cls.set_status(batch_id, "AVAILABLE", "Released from quarantine")

    @classmethod
    @transaction.atomic
    def mark_expired(cls, batch_id: int) -> Tuple[Dict[str, Any], int]:
        return cls.set_status(batch_id, "EXPIRED", "Manually marked expired")

    @classmethod
    @transaction.atomic
    def consume(cls,
                batch_id: int,
                quantity: Decimal,
                movement_type: str,
                user_id: int,
                reference_type: str = None,
                reference_id: int = None,
                notes: str = "") -> Tuple[Dict[str, Any], int]:
        batch = StockBatchRepository.get_for_update(batch_id)
        if not batch:
            return ServiceResponse.not_found(f"Batch with id {batch_id} not found")

        quantity = abs(to_decimal(quantity))
        available = batch.current_quantity - batch.reserved_quantity

        if quantity > available:
            return ServiceResponse.error(
                f"Insufficient stock in batch {batch.batch_number}: "
                f"requested {quantity}, available {available}"
            )

        batch.current_quantity -= quantity

        if batch.current_quantity <= 0:
            batch.status = StockBatch.BatchStatus.CONSUMED

        batch.save(update_fields=["current_quantity", "status", "updated_at"])

        from .level_service import StockLevelService
        result, status = StockLevelService.adjust(
            stock_item_id=batch.stock_item_id,
            location_id=batch.location_id,
            quantity=-quantity,
            movement_type=movement_type,
            user_id=user_id,
            batch_id=batch.id,
            unit_cost=batch.unit_cost,
            reference_type=reference_type,
            reference_id=reference_id,
            notes=notes
        )
        if status >= 400:
            return result, status

        return ServiceResponse.success(data={
            "consumed": str(quantity),
            "remaining": str(batch.current_quantity),
            "batch_status": batch.status
        }, message=f"Consumed {quantity} from batch")

    @classmethod
    @transaction.atomic
    def auto_consume(cls,
                     stock_item_id: int,
                     location_id: int,
                     quantity: Decimal,
                     movement_type: str,
                     user_id: int,
                     reference_type: str = None,
                     reference_id: int = None,
                     notes: str = "") -> Tuple[Dict[str, Any], int]:

        settings = StockSettingsRepository.load()

        if not settings.track_batches:
            from .level_service import StockLevelService
            return StockLevelService.adjust(
                stock_item_id=stock_item_id,
                location_id=location_id,
                quantity=-quantity,
                movement_type=movement_type,
                user_id=user_id,
                reference_type=reference_type,
                reference_id=reference_id,
                notes=notes
            )

        quantity = abs(to_decimal(quantity))
        remaining = quantity
        consumed_batches = []

        batches = cls.get_available_batches(stock_item_id, location_id)

        for batch in batches:
            if remaining <= 0:
                break

            available = batch.current_quantity - batch.reserved_quantity
            consume_qty = min(remaining, available)

            if consume_qty > 0:
                result, status = cls.consume(
                    batch_id=batch.id,
                    quantity=consume_qty,
                    movement_type=movement_type,
                    user_id=user_id,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    notes=notes
                )
                if status >= 400:
                    transaction.set_rollback(True)
                    return result, status

                consumed_batches.append({
                    "batch_id": batch.id,
                    "batch_number": batch.batch_number,
                    "quantity": str(consume_qty),
                    "unit_cost": str(batch.unit_cost)
                })

                remaining -= consume_qty

        if remaining > 0:
            stock_item = StockItemRepository.get_by_id(stock_item_id)
            item_name = stock_item.name if stock_item else f"item {stock_item_id}"
            transaction.set_rollback(True)
            return ServiceResponse.error(
                f"Insufficient stock for {item_name}: "
                f"requested {quantity}, available {quantity - remaining}"
            )

        return ServiceResponse.success(data={
            "total_consumed": str(quantity),
            "batches": consumed_batches
        }, message=f"Consumed from {len(consumed_batches)} batch(es)")
