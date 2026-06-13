from typing import Dict, Any, List, Tuple
from decimal import Decimal
from datetime import date
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import StockTransfer, StockTransferItem, StockSettings, StockBatch
from stock.services.base_service import to_decimal, generate_number
from stock.repositories import (
    StockTransferRepository, StockTransferItemRepository,
    StockItemRepository, StockLocationRepository,
    StockUnitRepository, StockBatchRepository, StockLevelRepository,
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


class StockTransferService:


    @classmethod
    def serialize(cls, transfer: StockTransfer, include_items: bool = False) -> Dict[str, Any]:
        data = {
            "id": transfer.id,
            "uuid": str(transfer.uuid),
            "transfer_number": transfer.transfer_number,

            "from_location_id": transfer.from_location_id,
            "from_location": {
                "id": transfer.from_location.id,
                "name": transfer.from_location.name,
                "type": transfer.from_location.type,
            },

            "to_location_id": transfer.to_location_id,
            "to_location": {
                "id": transfer.to_location.id,
                "name": transfer.to_location.name,
                "type": transfer.to_location.type,
            },

            "status": transfer.status,
            "status_display": transfer.get_status_display(),
            "transfer_type": transfer.transfer_type,
            "transfer_type_display": transfer.get_transfer_type_display(),

            "requested_by_id": transfer.requested_by_id,
            "approved_by_id": transfer.approved_by_id,
            "shipped_by_id": transfer.shipped_by_id,
            "received_by_id": transfer.received_by_id,

            "requested_at": transfer.requested_at.isoformat() if transfer.requested_at else None,
            "approved_at": transfer.approved_at.isoformat() if transfer.approved_at else None,
            "shipped_at": transfer.shipped_at.isoformat() if transfer.shipped_at else None,
            "received_at": transfer.received_at.isoformat() if transfer.received_at else None,

            "notes": transfer.notes,
            "created_at": transfer.created_at.isoformat(),
            "updated_at": transfer.updated_at.isoformat(),
        }

        if include_items:
            data["items"] = [
                StockTransferItemService.serialize(item)
                for item in transfer.items.select_related("stock_item", "unit", "batch")
            ]
            data["item_count"] = transfer.items.count()

        return data

    @classmethod
    def serialize_brief(cls, transfer: StockTransfer) -> Dict[str, Any]:
        return {
            "id": transfer.id,
            "transfer_number": transfer.transfer_number,
            "from_location": transfer.from_location.name,
            "to_location": transfer.to_location.name,
            "status": transfer.status,
            "created_at": transfer.created_at.isoformat(),
        }

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             from_location_id: int = None,
             to_location_id: int = None,
             status: str = None,
             transfer_type: str = None,
             date_from: date = None,
             date_to: date = None) -> Tuple[Dict[str, Any], int]:
        queryset = StockTransferRepository.get_all().select_related(
            "from_location", "to_location"
        )

        if from_location_id:
            queryset = queryset.filter(from_location_id=from_location_id)

        if to_location_id:
            queryset = queryset.filter(to_location_id=to_location_id)

        if status:
            queryset = queryset.filter(status=status)

        if transfer_type:
            queryset = queryset.filter(transfer_type=transfer_type)

        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        queryset = queryset.order_by("-created_at")

        page_obj, paginator = StockTransferRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "transfers": [cls.serialize_brief(t) for t in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [{"value": c[0], "label": c[1]} for c in StockTransfer.Status.choices],
            "types": [{"value": c[0], "label": c[1]} for c in StockTransfer.TransferType.choices],
        })

    @classmethod
    def get_pending(cls, location_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = StockTransferRepository.get_all().filter(
            status__in=["DRAFT", "REQUESTED", "APPROVED", "IN_TRANSIT"]
        ).select_related("from_location", "to_location")

        if location_id:
            queryset = queryset.filter(
                Q(from_location_id=location_id) | Q(to_location_id=location_id)
            )

        return ServiceResponse.success(data={
            "transfers": [cls.serialize_brief(t) for t in queryset.order_by("-created_at")],
            "count": queryset.count()
        })

    @classmethod
    def get_incoming(cls, location_id: int) -> Tuple[Dict[str, Any], int]:
        transfers = StockTransferRepository.get_all().filter(
            to_location_id=location_id,
            status__in=["APPROVED", "IN_TRANSIT"]
        ).select_related("from_location").order_by("-created_at")

        return ServiceResponse.success(data={
            "transfers": [cls.serialize_brief(t) for t in transfers],
            "count": transfers.count()
        })

    @classmethod
    def get_outgoing(cls, location_id: int) -> Tuple[Dict[str, Any], int]:
        transfers = StockTransferRepository.get_all().filter(
            from_location_id=location_id,
            status__in=["REQUESTED", "APPROVED", "IN_TRANSIT"]
        ).select_related("to_location").order_by("-created_at")

        return ServiceResponse.success(data={
            "transfers": [cls.serialize_brief(t) for t in transfers],
            "count": transfers.count()
        })


    @classmethod
    def get(cls, transfer_id: int, include_items: bool = True) -> Tuple[Dict[str, Any], int]:
        transfer = StockTransferRepository.get_with_relations(transfer_id)

        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer, include_items=include_items)
        })


    @classmethod
    @transaction.atomic
    def create(cls,
               from_location_id: int,
               to_location_id: int,
               requested_by_id: int,
               transfer_type: str = "INTERNAL",
               notes: str = "",
               items: List[Dict] = None) -> Tuple[Dict[str, Any], int]:

        from_location = StockLocationRepository.get_by_id(from_location_id)
        if not from_location or not from_location.is_active:
            return ServiceResponse.not_found(f"From location with id {from_location_id} not found")

        to_location = StockLocationRepository.get_by_id(to_location_id)
        if not to_location or not to_location.is_active:
            return ServiceResponse.not_found(f"To location with id {to_location_id} not found")

        if from_location_id == to_location_id:
            return ServiceResponse.validation_error(
                errors={"to_location_id": "Cannot transfer to same location"}
            )

        valid_types = [c[0] for c in StockTransfer.TransferType.choices]
        if transfer_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"transfer_type": f"Invalid type. Valid: {valid_types}"}
            )

        transfer_number = generate_number("TRF", StockTransfer, "transfer_number")

        transfer = StockTransferRepository.create(
            transfer_number=transfer_number,
            from_location=from_location,
            to_location=to_location,
            status=StockTransfer.Status.DRAFT,
            transfer_type=transfer_type,
            requested_by_id=requested_by_id,
            notes=notes,
        )

        if items:
            for item_data in items:
                result, status = StockTransferItemService.add_item(
                    transfer_id=transfer.id,
                    stock_item_id=item_data["stock_item_id"],
                    requested_qty=item_data["quantity"],
                    unit_id=item_data.get("unit_id"),
                    batch_id=item_data.get("batch_id"),
                )
                if status >= 400:
                    return result, status

        return ServiceResponse.success(data={
            "id": transfer.id,
            "transfer_number": transfer_number,
            "transfer": cls.serialize(transfer, include_items=True)
        }, message=f"Transfer {transfer_number} created")


    @classmethod
    @transaction.atomic
    def update(cls, transfer_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        if transfer.status not in ["DRAFT", "REQUESTED"]:
            return ServiceResponse.error(f"Cannot update {transfer.status} transfer")

        update_fields = ["updated_at"]

        if "from_location_id" in kwargs:
            location = StockLocationRepository.get_by_id(kwargs["from_location_id"])
            if not location or not location.is_active:
                return ServiceResponse.not_found(
                    f"From location with id {kwargs['from_location_id']} not found"
                )
            transfer.from_location = location
            update_fields.append("from_location")

        if "to_location_id" in kwargs:
            location = StockLocationRepository.get_by_id(kwargs["to_location_id"])
            if not location or not location.is_active:
                return ServiceResponse.not_found(
                    f"To location with id {kwargs['to_location_id']} not found"
                )
            transfer.to_location = location
            update_fields.append("to_location")

        if "notes" in kwargs:
            transfer.notes = kwargs["notes"]
            update_fields.append("notes")

        transfer.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer, include_items=True)
        }, message="Transfer updated")


    @classmethod
    @transaction.atomic
    def request(cls, transfer_id: int) -> Tuple[Dict[str, Any], int]:
        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        if transfer.status != "DRAFT":
            return ServiceResponse.error("Can only request DRAFT transfers")

        if not transfer.items.exists():
            return ServiceResponse.error("Cannot request empty transfer")

        transfer.status = StockTransfer.Status.REQUESTED
        transfer.requested_at = timezone.now()
        transfer.save(update_fields=["status", "requested_at", "updated_at"])

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer)
        }, message="Transfer requested")

    @classmethod
    @transaction.atomic
    def approve(cls, transfer_id: int, approved_by_id: int) -> Tuple[Dict[str, Any], int]:
        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        settings = StockSettings.load()

        if transfer.status not in ["DRAFT", "REQUESTED"]:
            return ServiceResponse.error(f"Cannot approve {transfer.status} transfer")

        for item in transfer.items.all():
            # Lock the source StockLevel rows under the SAME row lock that
            # ship() acquires (via StockLevelService.adjust ->
            # get_or_create_level_for_update). Without the lock the availability
            # check below is an unlocked aggregate read, so two concurrent
            # approvals can both pass against the same stock and over-commit it.
            # We materialize the locked rows and sum in Python because aggregate
            # cannot be combined with select_for_update.
            locked_levels = list(
                StockLevelRepository.filter(
                    stock_item_id=item.stock_item_id,
                    location=transfer.from_location,
                ).select_for_update()
            )
            # Net of reservations: stock already reserved for orders/production
            # isn't free to transfer, so approve against available_quantity
            # (quantity - reserved_quantity), not the raw on-hand quantity.
            available = sum(
                (lvl.available_quantity for lvl in locked_levels), Decimal("0")
            )

            if item.requested_qty > available and not settings.allow_negative_stock:
                return ServiceResponse.error(
                    f"Insufficient stock for {item.stock_item.name}: "
                    f"required {item.requested_qty}, available {available}"
                )

            item.approved_qty = item.requested_qty
            item.save(update_fields=["approved_qty"])

        transfer.status = StockTransfer.Status.APPROVED
        transfer.approved_by_id = approved_by_id
        transfer.approved_at = timezone.now()
        if not transfer.requested_at:
            transfer.requested_at = timezone.now()
        transfer.save(update_fields=["status", "approved_by", "approved_at", "requested_at", "updated_at"])

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer, include_items=True)
        }, message="Transfer approved")

    @classmethod
    @transaction.atomic
    def ship(cls, transfer_id: int, shipped_by_id: int) -> Tuple[Dict[str, Any], int]:
        """Ship transfer (deduct from source location)"""
        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        if transfer.status != "APPROVED":
            return ServiceResponse.error("Can only ship APPROVED transfers")

        from .level_service import StockLevelService

        for item in transfer.items.select_related("stock_item", "unit", "batch"):
            qty = item.approved_qty or item.requested_qty

            # Batch-tracked leg: debit the specific source batch so its
            # current_quantity stays in lockstep with the location level.
            # Without this the source batch keeps its full quantity and FIFO/
            # FEFO consumption later fabricates stock that was already shipped.
            if item.batch_id:
                batch = StockBatchRepository.get_for_update(item.batch_id)
                if not batch:
                    transaction.set_rollback(True)
                    return ServiceResponse.not_found(
                        f"Batch {item.batch_id} not found for transfer item {item.id}"
                    )
                batch_available = batch.current_quantity - batch.reserved_quantity
                if qty > batch_available:
                    transaction.set_rollback(True)
                    return ServiceResponse.error(
                        f"Insufficient quantity in batch {batch.batch_number}: "
                        f"requested {qty}, available {batch_available}"
                    )
                batch.current_quantity -= qty
                if batch.current_quantity <= 0:
                    batch.status = StockBatch.BatchStatus.CONSUMED
                batch.save(update_fields=["current_quantity", "status", "updated_at"])

            result, status = StockLevelService.adjust(
                stock_item_id=item.stock_item_id,
                location_id=transfer.from_location_id,
                quantity=-qty,
                movement_type="TRANSFER_OUT",
                user_id=shipped_by_id,
                batch_id=item.batch_id,
                transfer_id=transfer.id,
                notes=f"Transfer to {transfer.to_location.name}"
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status

            item.shipped_qty = qty
            item.save(update_fields=["shipped_qty"])

        transfer.status = StockTransfer.Status.IN_TRANSIT
        transfer.shipped_by_id = shipped_by_id
        transfer.shipped_at = timezone.now()
        transfer.save(update_fields=["status", "shipped_by", "shipped_at", "updated_at"])

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer, include_items=True)
        }, message="Transfer shipped")

    @classmethod
    @transaction.atomic
    def receive(cls, transfer_id: int, received_by_id: int,
                received_quantities: Dict[int, Decimal] = None) -> Tuple[Dict[str, Any], int]:
        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        if transfer.status != "IN_TRANSIT":
            return ServiceResponse.error("Can only receive IN_TRANSIT transfers")

        from .level_service import StockLevelService
        from .batch_service import StockBatchService

        for item in transfer.items.select_related("stock_item", "unit", "batch"):
            shipped = item.shipped_qty or item.approved_qty or item.requested_qty
            if received_quantities and item.id in received_quantities:
                qty = to_decimal(received_quantities[item.id])
            else:
                qty = shipped

            # Refuse impossible receipts. Without this guard, a caller can
            # claim to receive more than was shipped — fabricating stock at
            # the destination location while the source is correctly debited.
            if qty < 0 or qty > shipped:
                transaction.set_rollback(True)
                return ServiceResponse.validation_error(
                    errors={f"item_{item.id}": (
                        f"received_qty must be between 0 and the shipped amount ({shipped})"
                    )},
                )

            # Batch-tracked leg: materialize a destination batch for the
            # received quantity instead of stamping the source batch_id (which
            # lives at the source location). A fresh batch_number is generated
            # because (batch_number, stock_item) is unique — the same batch
            # cannot exist at two locations. Cost/expiry are copied so FEFO and
            # costing remain correct at the destination.
            dest_batch_id = None
            if item.batch_id and qty > 0:
                src = item.batch
                created, cstatus = StockBatchService.create(
                    stock_item_id=item.stock_item_id,
                    location_id=transfer.to_location_id,
                    quantity=qty,
                    unit_cost=src.unit_cost if src else None,
                    manufactured_date=src.manufactured_date if src else None,
                    expiry_date=src.expiry_date if src else None,
                    supplier_id=src.supplier_id if src else None,
                    notes=(f"Received via transfer #{transfer.id} from batch "
                           f"{src.batch_number}") if src else "",
                )
                if cstatus >= 400:
                    transaction.set_rollback(True)
                    return created, cstatus
                dest_batch_id = created["data"]["id"]

            result, status = StockLevelService.adjust(
                stock_item_id=item.stock_item_id,
                location_id=transfer.to_location_id,
                quantity=qty,
                movement_type="TRANSFER_IN",
                user_id=received_by_id,
                batch_id=dest_batch_id,
                transfer_id=transfer.id,
                notes=f"Transfer from {transfer.from_location.name}"
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status

            item.received_qty = qty

            if qty != shipped:
                item.variance_reason = f"Shipped: {shipped}, Received: {qty}"

            item.save(update_fields=["received_qty", "variance_reason"])

        transfer.status = StockTransfer.Status.RECEIVED
        transfer.received_by_id = received_by_id
        transfer.received_at = timezone.now()
        transfer.save(update_fields=["status", "received_by", "received_at", "updated_at"])

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer, include_items=True)
        }, message="Transfer received")

    @classmethod
    @transaction.atomic
    def cancel(cls, transfer_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        if transfer.status in ["RECEIVED", "CANCELED"]:
            return ServiceResponse.error(f"Cannot cancel {transfer.status} transfer")

        if transfer.status == "IN_TRANSIT":
            from .level_service import StockLevelService

            for item in transfer.items.select_related("stock_item"):
                qty = item.shipped_qty or item.approved_qty or item.requested_qty

                result, status = StockLevelService.adjust(
                    stock_item_id=item.stock_item_id,
                    location_id=transfer.from_location_id,
                    quantity=qty,
                    movement_type="TRANSFER_IN",
                    user_id=transfer.shipped_by_id or transfer.requested_by_id,
                    transfer_id=transfer.id,
                    notes=f"Transfer cancelled: {reason}"
                )
                if status >= 400:
                    transaction.set_rollback(True)
                    return result, status

        transfer.status = StockTransfer.Status.CANCELED
        if reason:
            transfer.notes = f"{transfer.notes}\nCancelled: {reason}".strip()
        transfer.save(update_fields=["status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "transfer": cls.serialize(transfer)
        }, message="Transfer cancelled")

    @classmethod
    @transaction.atomic
    def quick_transfer(cls,
                       from_location_id: int,
                       to_location_id: int,
                       stock_item_id: int,
                       quantity: Decimal,
                       user_id: int,
                       unit_id: int = None,
                       batch_id: int = None,
                       notes: str = "") -> Tuple[Dict[str, Any], int]:
        result, status = cls.create(
            from_location_id=from_location_id,
            to_location_id=to_location_id,
            requested_by_id=user_id,
            notes=notes,
            items=[{
                "stock_item_id": stock_item_id,
                "quantity": quantity,
                "unit_id": unit_id,
                "batch_id": batch_id,
            }]
        )
        if status >= 400:
            return result, status

        transfer_id = result["data"]["id"]

        result, status = cls.approve(transfer_id, user_id)
        if status >= 400:
            return result, status

        result, status = cls.ship(transfer_id, user_id)
        if status >= 400:
            return result, status

        result, status = cls.receive(transfer_id, user_id)
        if status >= 400:
            return result, status

        return cls.get(transfer_id)


class StockTransferItemService:

    @classmethod
    def serialize(cls, item: StockTransferItem) -> Dict[str, Any]:
        return {
            "id": item.id,
            "uuid": str(item.uuid),
            "stock_item_id": item.stock_item_id,
            "stock_item": {
                "id": item.stock_item.id,
                "name": item.stock_item.name,
                "sku": item.stock_item.sku,
            },
            "batch_id": item.batch_id,
            "batch_number": item.batch.batch_number if item.batch else None,
            "requested_qty": str(item.requested_qty),
            "approved_qty": str(item.approved_qty) if item.approved_qty else None,
            "shipped_qty": str(item.shipped_qty) if item.shipped_qty else None,
            "received_qty": str(item.received_qty) if item.received_qty else None,
            "unit_id": item.unit_id,
            "unit_short": item.unit.short_name,
            "variance_reason": item.variance_reason,
        }

    @classmethod
    @transaction.atomic
    def add_item(cls,
                 transfer_id: int,
                 stock_item_id: int,
                 requested_qty: Decimal,
                 unit_id: int = None,
                 batch_id: int = None) -> Tuple[Dict[str, Any], int]:

        transfer = StockTransferRepository.get_by_id(transfer_id)
        if not transfer:
            return ServiceResponse.not_found(f"Transfer with id {transfer_id} not found")

        if transfer.status not in ["DRAFT", "REQUESTED"]:
            return ServiceResponse.error("Cannot add items to approved/shipped transfer")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found(f"Stock item with id {stock_item_id} not found")

        if unit_id:
            unit = StockUnitRepository.get_by_id(unit_id)
            if not unit:
                return ServiceResponse.not_found(f"Unit with id {unit_id} not found")
        else:
            unit = stock_item.base_unit

        batch = None
        if batch_id:
            batch = StockBatchRepository.first(
                id=batch_id,
                stock_item=stock_item,
                location=transfer.from_location
            )
            if not batch:
                return ServiceResponse.not_found(f"Batch with id {batch_id} not found")

        existing = StockTransferItemRepository.first(
            transfer=transfer,
            stock_item=stock_item,
            batch=batch
        )

        if existing:
            existing.requested_qty += to_decimal(requested_qty)
            existing.save(update_fields=["requested_qty"])
            return ServiceResponse.success(data={
                "item": cls.serialize(existing)
            }, message="Item quantity updated")

        item = StockTransferItemRepository.create(
            transfer=transfer,
            stock_item=stock_item,
            batch=batch,
            requested_qty=to_decimal(requested_qty),
            unit=unit,
        )

        return ServiceResponse.success(data={
            "id": item.id,
            "item": cls.serialize(item)
        }, message="Item added to transfer")

    @classmethod
    @transaction.atomic
    def update_item(cls, item_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        item = StockTransferItemRepository.get_by_id(item_id)
        if not item:
            return ServiceResponse.not_found(f"Transfer item with id {item_id} not found")

        if item.transfer.status not in ["DRAFT", "REQUESTED"]:
            return ServiceResponse.error("Cannot update items on approved/shipped transfer")

        if "requested_qty" in kwargs:
            item.requested_qty = to_decimal(kwargs["requested_qty"])

        item.save()

        return ServiceResponse.success(data={
            "item": cls.serialize(item)
        }, message="Item updated")

    @classmethod
    @transaction.atomic
    def remove_item(cls, item_id: int) -> Tuple[Dict[str, Any], int]:
        item = StockTransferItemRepository.get_by_id(item_id)
        if not item:
            return ServiceResponse.not_found(f"Transfer item with id {item_id} not found")

        if item.transfer.status not in ["DRAFT", "REQUESTED"]:
            return ServiceResponse.error("Cannot remove items from approved/shipped transfer")

        item.delete()

        return ServiceResponse.success(message="Item removed from transfer")
