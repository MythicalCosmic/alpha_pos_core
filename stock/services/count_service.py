from typing import Dict, Any, List, Tuple
from decimal import Decimal
from datetime import date
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import (
    StockCount, StockCountItem, VarianceReasonCode,
    StockLevel
)
from stock.services.base_service import to_decimal, round_decimal, generate_number
from stock.repositories import (
    StockCountRepository, StockCountItemRepository,
    VarianceReasonCodeRepository, StockLocationRepository, StockCategoryRepository,
    StockLevelRepository, StockBatchRepository,
    StockSettingsRepository,
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


class VarianceReasonCodeService:

    @classmethod
    def serialize(cls, code: VarianceReasonCode) -> Dict[str, Any]:
        return {
            "id": code.id,
            "uuid": str(code.uuid),
            "code": code.code,
            "name": code.name,
            "description": code.description,
            "requires_approval": code.requires_approval,
            "is_active": code.is_active,
        }

    @classmethod
    def list(cls, active_only: bool = True) -> Tuple[Dict[str, Any], int]:
        if active_only:
            queryset = VarianceReasonCodeRepository.get_active()
        else:
            queryset = VarianceReasonCodeRepository.get_all()

        queryset = queryset.order_by("code")

        return ServiceResponse.success(data={
            "codes": [cls.serialize(c) for c in queryset],
            "count": queryset.count()
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               code: str,
               name: str,
               description: str = "",
               requires_approval: bool = False) -> Tuple[Dict[str, Any], int]:

        if VarianceReasonCodeRepository.code_exists(code):
            return ServiceResponse.validation_error(
                errors={"code": f"Code '{code}' already exists"}
            )

        reason_code = VarianceReasonCodeRepository.create(
            code=code.upper(),
            name=name,
            description=description,
            requires_approval=requires_approval,
        )

        return ServiceResponse.created(data={
            "id": reason_code.id,
            "code": cls.serialize(reason_code)
        }, message=f"Variance code '{code}' created")

    @classmethod
    @transaction.atomic
    def update(cls, code_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        reason_code = VarianceReasonCodeRepository.get_by_id(code_id)
        if not reason_code:
            return ServiceResponse.not_found(f"Variance code with id {code_id} not found")

        for field in ["name", "description", "requires_approval", "is_active"]:
            if field in kwargs:
                setattr(reason_code, field, kwargs[field])

        reason_code.save()

        return ServiceResponse.success(data={
            "code": cls.serialize(reason_code)
        }, message="Variance code updated")

    @classmethod
    def get_default_codes(cls) -> List[Dict]:
        return [
            {"code": "DAMAGE", "name": "Damaged", "description": "Item damaged", "requires_approval": True},
            {"code": "THEFT", "name": "Theft", "description": "Suspected theft", "requires_approval": True},
            {"code": "EXPIRED", "name": "Expired", "description": "Item expired", "requires_approval": False},
            {"code": "COUNT_ERR", "name": "Count Error", "description": "Previous count error", "requires_approval": False},
            {"code": "UNRECORDED", "name": "Unrecorded Movement", "description": "Movement not recorded", "requires_approval": True},
            {"code": "WASTE", "name": "Waste", "description": "Normal waste", "requires_approval": False},
            {"code": "SAMPLE", "name": "Sample", "description": "Used as sample", "requires_approval": False},
            {"code": "OTHER", "name": "Other", "description": "Other reason", "requires_approval": True},
        ]

    @classmethod
    @transaction.atomic
    def seed_defaults(cls) -> Tuple[Dict[str, Any], int]:
        created = 0
        for code_data in cls.get_default_codes():
            if not VarianceReasonCodeRepository.code_exists(code_data["code"]):
                VarianceReasonCodeRepository.create(**code_data)
                created += 1

        return ServiceResponse.success(data={
            "created": created
        }, message=f"Created {created} variance code(s)")


class StockCountService:

    @classmethod
    def serialize(cls, count: StockCount, include_items: bool = False) -> Dict[str, Any]:
        data = {
            "id": count.id,
            "uuid": str(count.uuid),
            "count_number": count.count_number,

            "location_id": count.location_id,
            "location": {
                "id": count.location.id,
                "name": count.location.name,
            },

            "count_type": count.count_type,
            "count_type_display": count.get_count_type_display(),

            "category_filter_id": count.category_filter_id,
            "category_filter_name": count.category_filter.name if count.category_filter else None,

            "status": count.status,
            "status_display": count.get_status_display(),

            "started_at": count.started_at.isoformat() if count.started_at else None,
            "completed_at": count.completed_at.isoformat() if count.completed_at else None,

            "counted_by_id": count.counted_by_id,
            "approved_by_id": count.approved_by_id,
            "auto_adjust": count.auto_adjust,

            "notes": count.notes,
            "created_at": count.created_at.isoformat(),
        }

        if include_items:
            items = count.items.select_related(
                "stock_item", "batch", "reason_code"
            ).order_by("stock_item__name")

            data["items"] = [StockCountItemService.serialize(item) for item in items]
            data["item_count"] = items.count()

            # Summary statistics
            counted = items.filter(counted_quantity__isnull=False)
            data["summary"] = {
                "total_items": items.count(),
                "counted_items": counted.count(),
                "pending_items": items.filter(counted_quantity__isnull=True).count(),
                "items_with_variance": counted.exclude(variance=0).count(),
                "total_variance_cost": str(
                    counted.aggregate(total=Sum("variance_cost"))["total"] or 0
                ),
            }

        return data

    @classmethod
    def serialize_brief(cls, count: StockCount) -> Dict[str, Any]:
        """Brief serialization"""
        return {
            "id": count.id,
            "count_number": count.count_number,
            "location_name": count.location.name,
            "count_type": count.count_type,
            "status": count.status,
            "created_at": count.created_at.isoformat(),
        }

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             location_id: int = None,
             status: str = None,
             count_type: str = None,
             date_from: date = None,
             date_to: date = None) -> Tuple[Dict[str, Any], int]:
        queryset = StockCountRepository.get_all().select_related("location", "category_filter")

        if location_id:
            queryset = queryset.filter(location_id=location_id)

        if status:
            queryset = queryset.filter(status=status)

        if count_type:
            queryset = queryset.filter(count_type=count_type)

        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        queryset = queryset.order_by("-created_at")

        page_obj, paginator = StockCountRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "counts": [cls.serialize_brief(c) for c in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [{"value": c[0], "label": c[1]} for c in StockCount.Status.choices],
            "count_types": [{"value": c[0], "label": c[1]} for c in StockCount.CountType.choices],
        })

    @classmethod
    def get_active(cls, location_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = StockCountRepository.get_all().filter(
            status__in=["DRAFT", "IN_PROGRESS"]
        ).select_related("location")

        if location_id:
            queryset = queryset.filter(location_id=location_id)

        return ServiceResponse.success(data={
            "counts": [cls.serialize_brief(c) for c in queryset.order_by("-created_at")],
            "count": queryset.count()
        })

    @classmethod
    def get(cls, count_id: int, include_items: bool = True) -> Tuple[Dict[str, Any], int]:
        count = StockCountRepository.get_with_relations(count_id)
        if not count:
            return ServiceResponse.not_found(f"Stock count with id {count_id} not found")

        return ServiceResponse.success(data={
            "count": cls.serialize(count, include_items=include_items)
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               location_id: int,
               count_type: str,
               counted_by_id: int,
               category_id: int = None,
               auto_adjust: bool = False,
               notes: str = "",
               include_zero_stock: bool = True) -> Tuple[Dict[str, Any], int]:
        location = StockLocationRepository.get_by_id(location_id)
        if not location:
            return ServiceResponse.not_found(f"Location with id {location_id} not found")
        if not location.is_active:
            return ServiceResponse.error("Location is not active")

        # Validate count type
        valid_types = [c[0] for c in StockCount.CountType.choices]
        if count_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"count_type": f"Invalid type. Valid: {valid_types}"}
            )

        category = None
        if category_id:
            category = StockCategoryRepository.get_by_id(category_id)
            if not category:
                return ServiceResponse.not_found(f"Category with id {category_id} not found")
            if not category.is_active:
                return ServiceResponse.error("Category is not active")

        existing = StockCountRepository.get_all().filter(
            location=location,
            status__in=["DRAFT", "IN_PROGRESS"]
        ).first()

        if existing:
            return ServiceResponse.error(
                f"Active count already exists at this location: {existing.count_number}"
            )

        count_number = generate_number("CNT", StockCount, "count_number")

        count = StockCountRepository.create(
            count_number=count_number,
            location=location,
            count_type=count_type,
            category_filter=category,
            status=StockCount.Status.DRAFT,
            counted_by_id=counted_by_id,
            auto_adjust=auto_adjust,
            notes=notes,
        )

        items_created = cls._populate_count_items(count, include_zero_stock)

        return ServiceResponse.created(data={
            "id": count.id,
            "count_number": count_number,
            "items_created": items_created,
            "count": cls.serialize(count, include_items=True)
        }, message=f"Stock count {count_number} created with {items_created} items")

    @classmethod
    def _populate_count_items(cls, count: StockCount, include_zero_stock: bool) -> int:
        queryset = StockLevelRepository.get_all().filter(
            location=count.location,
            stock_item__is_active=True
        ).select_related("stock_item")

        if count.category_filter:
            queryset = queryset.filter(stock_item__category=count.category_filter)

        if not include_zero_stock:
            queryset = queryset.filter(quantity__gt=0)

        settings = StockSettingsRepository.load()
        items_created = 0

        for level in queryset:
            if settings.track_batches and level.stock_item.track_batches:
                batches = StockBatchRepository.get_available(
                    stock_item_id=level.stock_item_id,
                    location_id=count.location_id,
                )

                for batch in batches:
                    StockCountItemRepository.create(
                        stock_count=count,
                        stock_item=level.stock_item,
                        batch=batch,
                        system_quantity=batch.current_quantity,
                    )
                    items_created += 1
            else:
                StockCountItemRepository.create(
                    stock_count=count,
                    stock_item=level.stock_item,
                    system_quantity=level.quantity,
                )
                items_created += 1

        return items_created

    @classmethod
    @transaction.atomic
    def start(cls, count_id: int) -> Tuple[Dict[str, Any], int]:
        count = StockCountRepository.get_by_id(count_id)
        if not count:
            return ServiceResponse.not_found(f"Stock count with id {count_id} not found")

        if count.status != "DRAFT":
            return ServiceResponse.error("Can only start DRAFT counts")

        count.status = StockCount.Status.IN_PROGRESS
        count.started_at = timezone.now()
        count.save(update_fields=["status", "started_at", "updated_at"])

        return ServiceResponse.success(data={
            "count": cls.serialize(count)
        }, message="Counting started")

    @classmethod
    @transaction.atomic
    def record_count(cls,
                     count_id: int,
                     item_id: int,
                     counted_quantity: Decimal,
                     reason_code_id: int = None,
                     notes: str = "") -> Tuple[Dict[str, Any], int]:
        count = StockCountRepository.get_by_id(count_id)
        if not count:
            return ServiceResponse.not_found(f"Stock count with id {count_id} not found")

        if count.status not in ["DRAFT", "IN_PROGRESS"]:
            return ServiceResponse.error(f"Cannot record counts for {count.status} count")

        item = StockCountItemRepository.get_by_id(item_id)
        if not item or item.stock_count_id != count.id:
            return ServiceResponse.not_found(f"Count item with id {item_id} not found")

        if count.status == "DRAFT":
            count.status = StockCount.Status.IN_PROGRESS
            count.started_at = timezone.now()
            count.save(update_fields=["status", "started_at", "updated_at"])

        counted_quantity = to_decimal(counted_quantity)

        variance = counted_quantity - item.system_quantity
        variance_percentage = Decimal("0")
        if item.system_quantity != 0:
            variance_percentage = (variance / item.system_quantity) * 100

        unit_cost = item.stock_item.avg_cost_price
        variance_cost = variance * unit_cost

        reason_code = None
        if reason_code_id:
            reason_code = VarianceReasonCodeRepository.get_by_id(reason_code_id)
            if not reason_code:
                return ServiceResponse.not_found(f"Reason code with id {reason_code_id} not found")
            if not reason_code.is_active:
                return ServiceResponse.error("Reason code is not active")

        item.counted_quantity = counted_quantity
        item.variance = variance
        item.variance_percentage = round_decimal(variance_percentage, 2)
        item.variance_cost = round_decimal(variance_cost, 4)
        item.reason_code = reason_code
        item.notes = notes
        item.save()

        return ServiceResponse.success(data={
            "item": StockCountItemService.serialize(item)
        }, message="Count recorded")

    @classmethod
    @transaction.atomic
    def complete(cls, count_id: int) -> Tuple[Dict[str, Any], int]:
        count = StockCountRepository.get_by_id(count_id)
        if not count:
            return ServiceResponse.not_found(f"Stock count with id {count_id} not found")

        if count.status != "IN_PROGRESS":
            return ServiceResponse.error("Can only complete IN_PROGRESS counts")

        uncounted = count.items.filter(counted_quantity__isnull=True).count()
        if uncounted > 0:
            return ServiceResponse.error(f"{uncounted} item(s) not yet counted")

        settings = StockSettingsRepository.load()

        if settings.require_count_approval:
            count.status = StockCount.Status.PENDING_APPROVAL
        else:
            count.status = StockCount.Status.APPROVED
            count.approved_by = count.counted_by

        count.completed_at = timezone.now()
        count.save(update_fields=["status", "completed_at", "approved_by", "updated_at"])

        if count.auto_adjust and count.status == "APPROVED":
            cls._apply_adjustments(count)

        return ServiceResponse.success(data={
            "count": cls.serialize(count, include_items=True)
        }, message="Counting completed")

    @classmethod
    @transaction.atomic
    def approve(cls, count_id: int, approved_by_id: int, apply_adjustments: bool = True) -> Tuple[Dict[str, Any], int]:
        count = StockCountRepository.get_by_id(count_id)
        if not count:
            return ServiceResponse.not_found(f"Stock count with id {count_id} not found")

        if count.status != "PENDING_APPROVAL":
            return ServiceResponse.error("Can only approve PENDING_APPROVAL counts")

        count.status = StockCount.Status.APPROVED
        count.approved_by_id = approved_by_id
        count.save(update_fields=["status", "approved_by", "updated_at"])

        if apply_adjustments:
            cls._apply_adjustments(count)

        return ServiceResponse.success(data={
            "count": cls.serialize(count, include_items=True)
        }, message="Count approved and adjustments applied")

    @classmethod
    def _apply_adjustments(cls, count: StockCount):
        from .level_service import StockLevelService

        # A physical count SETS each level to what was physically counted. The
        # `variance` stored at record-time was measured against `system_quantity`,
        # which is snapshotted when the count is created (_populate_count_items).
        # By approval time that snapshot is stale: any SALE_OUT / PURCHASE_IN that
        # happened between creation and approval moved the live level. Replaying
        # the stale variance as a signed delta would double-count those interim
        # movements and corrupt inventory. So we recompute the delta against the
        # CURRENT (row-locked) live quantity here and adjust by that — landing the
        # level exactly on the counted quantity regardless of interim movements.
        #
        # Every counted item is reconciled (not only those whose snapshot variance
        # was non-zero): a snapshot variance of zero can still be wrong now if the
        # live level drifted after the snapshot was taken.
        items = count.items.filter(counted_quantity__isnull=False).select_related(
            "stock_item", "batch"
        )

        for item in items:
            counted = to_decimal(item.counted_quantity)

            if item.batch_id:
                # Batch-tracked: current truth is the batch's live quantity
                # (decremented on consumption in batch_service). adjust() only
                # touches the aggregate level, so we set the batch quantity here
                # and feed the same delta to the level for the transaction record.
                batch = StockBatchRepository.get_for_update(item.batch_id)
                if not batch:
                    raise RuntimeError(
                        f"Count adjustment failed: batch {item.batch_id} not found"
                    )
                delta = counted - batch.current_quantity
            else:
                level = StockLevelService.get_level_for_update(
                    item.stock_item_id, count.location_id
                )
                delta = counted - level.quantity

            if delta == 0:
                item.is_adjusted = True
                item.save(update_fields=["is_adjusted"])
                continue

            movement_type = "COUNT_ADJUSTMENT"

            result, status = StockLevelService.adjust(
                stock_item_id=item.stock_item_id,
                location_id=count.location_id,
                quantity=delta,
                movement_type=movement_type,
                user_id=count.approved_by_id or count.counted_by_id,
                batch_id=item.batch_id,
                reference_type="StockCount",
                reference_id=count.id,
                notes=f"Count adjustment: {count.count_number}"
            )

            if status >= 400:
                # Surface the failure to the caller's atomic so the entire
                # count approval rolls back instead of marking some items
                # adjusted while leaving levels untouched for others.
                raise RuntimeError(
                    f"Count adjustment failed for item {item.stock_item_id}: "
                    f"{result.get('message', 'unknown error')}"
                )

            if item.batch_id:
                # adjust() does not touch batch.current_quantity — set the
                # batch to the counted truth so the next count diffs correctly.
                batch.current_quantity = counted
                batch.save(update_fields=["current_quantity", "updated_at"])

            item.is_adjusted = True
            if "transaction_id" in result.get("data", {}):
                item.adjustment_transaction_id = result["data"]["transaction_id"]
            item.save(update_fields=["is_adjusted", "adjustment_transaction"])

        StockLevel.objects.filter(
            location=count.location,
            stock_item__in=count.items.values("stock_item")
        ).update(last_counted_at=timezone.now())

    @classmethod
    @transaction.atomic
    def cancel(cls, count_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        count = StockCountRepository.get_by_id(count_id)
        if not count:
            return ServiceResponse.not_found(f"Stock count with id {count_id} not found")

        if count.status == "APPROVED":
            return ServiceResponse.error("Cannot cancel approved count")

        if count.status == "CANCELED":
            return ServiceResponse.error("Count already canceled")

        count.status = StockCount.Status.CANCELED
        if reason:
            count.notes = f"{count.notes}\nCancelled: {reason}".strip()
        count.save(update_fields=["status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "count": cls.serialize(count)
        }, message="Stock count cancelled")

    @classmethod
    @transaction.atomic
    def create_blind_count(cls,
                           location_id: int,
                           counted_by_id: int,
                           count_type: str = "SPOT",
                           notes: str = "") -> Tuple[Dict[str, Any], int]:
        return cls.create(
            location_id=location_id,
            count_type=count_type,
            counted_by_id=counted_by_id,
            notes=notes,
            include_zero_stock=False,
        )


class StockCountItemService:

    @classmethod
    def serialize(cls, item: StockCountItem, hide_system_qty: bool = False) -> Dict[str, Any]:
        data = {
            "id": item.id,
            "uuid": str(item.uuid),
            "stock_item_id": item.stock_item_id,
            "stock_item": {
                "id": item.stock_item.id,
                "name": item.stock_item.name,
                "sku": item.stock_item.sku,
                "unit": item.stock_item.base_unit.short_name,
            },
            "batch_id": item.batch_id,
            "batch_number": item.batch.batch_number if item.batch else None,
            "counted_quantity": str(item.counted_quantity) if item.counted_quantity is not None else None,
            "is_counted": item.counted_quantity is not None,
            "notes": item.notes,
            "is_adjusted": item.is_adjusted,
        }

        if hide_system_qty and item.counted_quantity is None:
            data["system_quantity"] = "***"
            data["variance"] = None
            data["variance_percentage"] = None
            data["variance_cost"] = None
        else:
            data["system_quantity"] = str(item.system_quantity)
            data["variance"] = str(item.variance) if item.variance is not None else None
            data["variance_percentage"] = str(item.variance_percentage) if item.variance_percentage is not None else None
            data["variance_cost"] = str(item.variance_cost) if item.variance_cost is not None else None

        # Reason code
        if item.reason_code:
            data["reason_code"] = {
                "id": item.reason_code.id,
                "code": item.reason_code.code,
                "name": item.reason_code.name,
            }
        else:
            data["reason_code"] = None

        return data

    @classmethod
    def get_uncounted(cls, count_id: int) -> Tuple[Dict[str, Any], int]:
        items = StockCountItemRepository.get_uncounted(count_id)

        return ServiceResponse.success(data={
            "items": [cls.serialize(item) for item in items],
            "count": items.count()
        })

    @classmethod
    def get_with_variance(cls, count_id: int) -> Tuple[Dict[str, Any], int]:
        items = StockCountItemRepository.get_with_variance(count_id)

        return ServiceResponse.success(data={
            "items": [cls.serialize(item) for item in items],
            "count": items.count()
        })
