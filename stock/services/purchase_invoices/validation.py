"""Direct invoice input and locked catalog validation."""

from decimal import Decimal, ROUND_HALF_UP
import unicodedata
from zoneinfo import ZoneInfo

from django.utils import timezone

from base.money import MoneyValueError, decimal_value, whole_uzs
from base.security.permissions import user_has_permission
from base.services.branch_scope import resolve_actor_branch
from stock.models import (
    PurchaseReceiving,
    StockItem,
    StockItemUnit,
    StockLocation,
    StockUnit,
    Supplier,
    SupplierStockItem,
)
from stock.services.purchase_validation import purchase_date, unit_factor
from .idempotency import fail

UNKNOWN_PRICE_MARKER = (
    "Purchase price was not provided; 0 is a temporary "
    "unknown-price placeholder to replace on the first purchase."
)
MAX_LINE_VALUE = Decimal("99999999999")
MAX_INVOICE_VALUE = Decimal("9999999999999")
QUANTITY_MAX = Decimal("99999999999.9999")
TASHKENT = ZoneInfo("Asia/Tashkent")
HEADER_FIELDS = {
    "supplier_id",
    "location_id",
    "invoice_date",
    "supplier_invoice_number",
    "currency",
    "declared_total_uzs",
    "notes",
    "lines",
    "replaces_invoice_id",
}
LINE_FIELDS = {
    "supplier_item_id",
    "quantity",
    "unit_price_uzs",
    "is_free",
    "free_reason",
    "batch_number",
    "expiry_date",
    "notes",
    "price_change_confirmed",
    "price_change_reason",
}


def actor_scope(actor, permission, *, correction=False):
    if (
        not actor
        or actor.is_deleted
        or actor.status != "ACTIVE"
        or actor.role not in ("ADMIN", "MANAGER", "WAREHOUSE")
        or not user_has_permission(actor, permission)
        or (correction and actor.role not in ("ADMIN", "MANAGER"))
    ):
        fail("STOCK_SCOPE_FORBIDDEN", "This stock operation is not permitted.", 403)
    branch = str(resolve_actor_branch(actor) or "").strip()
    if not branch:
        fail("STOCK_SCOPE_FORBIDDEN", "An authorized branch is required.", 403)
    return branch


def text(value, field, maximum=1000, *, required=False):
    if not isinstance(value, str):
        fail("VALIDATION_ERROR", "Must be text.", field=field)
    value = value.strip()
    if len(value) > maximum or (required and not value):
        fail(
            "VALIDATION_ERROR",
            f"Must contain {1 if required else 0}–{maximum} characters.",
            field=field,
        )
    return value


def identity(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail("VALIDATION_ERROR", "Must be a positive integer ID.", field=field)
    return value


def number(
    value, field, *, whole=False, positive=False, places=4, maximum=QUANTITY_MAX
):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        fail("VALIDATION_ERROR", "Must be an unformatted JSON number.", field=field)
    if not Decimal(value).is_finite():
        fail("VALIDATION_ERROR", "Must be finite.", field=field)
    try:
        if whole:
            return whole_uzs(value, field, positive=positive, maximum=maximum)
        # Numerical granularity, independent of JSON trailing zeros.
        value = format(Decimal(value).normalize(), "f")
        return decimal_value(
            value, field, places=places, positive=positive, maximum=maximum
        )
    except MoneyValueError as exc:
        fail("VALIDATION_ERROR", str(exc), field=field)


def validate_header(payload):
    if not isinstance(payload, dict):
        fail("VALIDATION_ERROR", "An invoice object is required.")
    unknown = set(payload) - HEADER_FIELDS
    if unknown:
        fail(
            "VALIDATION_ERROR",
            "Unsupported invoice fields.",
            details={"fields": sorted(unknown)},
        )
    supplier = identity(payload.get("supplier_id"), "supplier_id")
    location = identity(payload.get("location_id"), "location_id")
    if payload.get("currency") != "UZS":
        fail("VALIDATION_ERROR", "Only UZS is supported.", field="currency")
    try:
        invoice_date = purchase_date(payload.get("invoice_date"), "invoice_date")
    except MoneyValueError as exc:
        fail("VALIDATION_ERROR", str(exc), field="invoice_date")
    if invoice_date > timezone.localdate(timezone=TASHKENT):
        fail(
            "VALIDATION_ERROR",
            "Invoice date cannot be in the future.",
            field="invoice_date",
        )
    invoice_number = text(
        payload.get("supplier_invoice_number", ""), "supplier_invoice_number", 100
    )
    normalized = unicodedata.normalize(
        "NFKC", " ".join(invoice_number.split())
    ).casefold()
    if len(normalized) > 200:
        fail(
            "VALIDATION_ERROR",
            "Normalized invoice number is too long.",
            field="supplier_invoice_number",
        )
    declared = number(
        payload.get("declared_total_uzs"),
        "declared_total_uzs",
        whole=True,
        maximum=MAX_INVOICE_VALUE,
    )
    notes = text(payload.get("notes", ""), "notes")
    lines = payload.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 500:
        fail(
            "VALIDATION_ERROR",
            "Provide between 1 and 500 invoice lines.",
            field="lines",
        )
    for index, line in enumerate(lines):
        if not isinstance(line, dict) or set(line) - LINE_FIELDS:
            fail(
                "VALIDATION_ERROR",
                "Unsupported invoice line fields.",
                field=f"lines.{index}",
            )
        identity(line.get("supplier_item_id"), f"lines.{index}.supplier_item_id")
    replaces = payload.get("replaces_invoice_id")
    if replaces is not None:
        identity(replaces, "replaces_invoice_id")
    return {
        "supplier_id": supplier,
        "location_id": location,
        "invoice_date": invoice_date,
        "supplier_invoice_number": invoice_number,
        "normalized_number": normalized,
        "declared_total": declared,
        "notes": notes,
        "lines": lines,
        "replaces_id": replaces,
    }


def lock_catalog(header, branch):
    supplier = (
        Supplier.objects.select_for_update()
        .filter(
            id=header["supplier_id"],
            branch_id=branch,
            is_active=True,
            is_deleted=False,
        )
        .first()
    )
    if not supplier:
        fail(
            "SUPPLIER_NOT_FOUND",
            "Active supplier not found in this branch.",
            404,
            field="supplier_id",
        )
    if supplier.currency != "UZS":
        fail("VALIDATION_ERROR", "Supplier currency must be UZS.", field="supplier_id")
    # Existing stock mutations lock items before locations. Preserve that order
    # across commands, with supplier/link locks preceding both for procurement.
    links = {
        row.id: row
        for row in SupplierStockItem.objects.select_for_update()
        .filter(
            id__in=sorted({line["supplier_item_id"] for line in header["lines"]}),
            branch_id=branch,
            is_deleted=False,
            is_active=True,
        )
        .order_by("id")
    }
    for index, line in enumerate(header["lines"]):
        link = links.get(line["supplier_item_id"])
        if not link:
            fail(
                "SUPPLIER_ITEM_NOT_FOUND",
                "Active supplier item not found.",
                404,
                field=f"lines.{index}.supplier_item_id",
            )
        if link.supplier_id != supplier.id:
            fail(
                "SUPPLIER_ITEM_MISMATCH",
                "Item belongs to a different supplier.",
                field=f"lines.{index}.supplier_item_id",
            )
    items = {
        row.id: row
        for row in StockItem.objects.select_for_update()
        .filter(
            id__in=sorted({link.stock_item_id for link in links.values()}),
            branch_id=branch,
            is_deleted=False,
            is_active=True,
            is_purchasable=True,
        )
        .order_by("id")
    }
    location = (
        StockLocation.objects.select_for_update()
        .filter(
            id=header["location_id"],
            branch_id=branch,
            is_active=True,
            is_deleted=False,
        )
        .first()
    )
    if not location:
        fail(
            "LOCATION_NOT_FOUND",
            "Active destination not found in this branch.",
            404,
            field="location_id",
        )
    units = {
        row.id: row
        for row in StockUnit.objects.select_for_update()
        .filter(
            id__in=sorted(
                {link.unit_id for link in links.values()}
                | {i.base_unit_id for i in items.values()}
            ),
            is_active=True,
            is_deleted=False,
        )
        .order_by("id")
    }
    overrides = {}
    for link in (
        StockItemUnit.objects.select_for_update()
        .filter(
            stock_item_id__in=sorted(items),
            branch_id=branch,
            is_deleted=False,
        )
        .order_by("stock_item_id", "unit_id")
    ):
        overrides.setdefault(link.stock_item_id, {})[link.unit_id] = (
            link.conversion_to_base
        )
    if (
        header["normalized_number"]
        and PurchaseReceiving.objects.filter(
            branch_id=branch,
            supplier=supplier,
            source_type="DIRECT_INVOICE",
            supplier_invoice_number_normalized=header["normalized_number"],
        ).exists()
    ):
        fail(
            "DUPLICATE_SUPPLIER_INVOICE",
            "This supplier invoice number is already recorded.",
            409,
            field="supplier_invoice_number",
        )
    replacement = None
    if header["replaces_id"]:
        replacement = (
            PurchaseReceiving.objects.select_for_update()
            .filter(
                pk=header["replaces_id"],
                branch_id=branch,
                supplier=supplier,
                source_type="DIRECT_INVOICE",
                is_deleted=False,
                reversed_at__isnull=False,
            )
            .first()
        )
        if replacement is None:
            fail(
                "STOCK_SCOPE_FORBIDDEN",
                "Replacement must link a reversed invoice for this supplier.",
                404,
            )
    return supplier, location, links, items, units, overrides, replacement


def known_price(link):
    return bool(
        link.price_is_known
        and link.price > 0
        and UNKNOWN_PRICE_MARKER not in link.notes
        and link.currency == "UZS"
    )


def validate_lines(header, catalog):
    supplier, location, links, items, units, overrides, replacement = catalog
    result, lots = [], set()
    for index, raw in enumerate(header["lines"]):
        prefix = f"lines.{index}"
        link = links[raw["supplier_item_id"]]
        item, unit = items.get(link.stock_item_id), units.get(link.unit_id)
        if not item or item.base_unit_id not in units or unit is None:
            fail(
                "SUPPLIER_ITEM_NOT_FOUND",
                "Item and units must be active and purchasable.",
                field=prefix + ".supplier_item_id",
            )
        item.base_unit = units[item.base_unit_id]
        factor = unit_factor(item, unit, overrides=overrides.get(item.id, {}))
        if factor is None:
            fail(
                "UNIT_CONVERSION_MISSING",
                "Configure a real conversion into the base unit.",
                field=prefix + ".supplier_item_id",
            )
        quantity = number(
            raw.get("quantity"),
            prefix + ".quantity",
            positive=True,
            places=min(4, unit.decimal_places),
        )
        base_quantity = quantity * factor
        if (
            base_quantity <= 0
            or base_quantity > QUANTITY_MAX
            or base_quantity != base_quantity.quantize(Decimal(".0001"))
        ):
            fail(
                "VALIDATION_ERROR",
                "Converted quantity exceeds storage precision or capacity.",
                field=prefix + ".quantity",
            )
        free = raw.get("is_free", False)
        confirmed = raw.get("price_change_confirmed", False)
        if not isinstance(free, bool) or not isinstance(confirmed, bool):
            fail(
                "VALIDATION_ERROR",
                "Free and confirmation flags must be boolean.",
                field=prefix,
            )
        reason = text(
            raw.get("free_reason", ""), prefix + ".free_reason", required=free
        )
        if "unit_price_uzs" not in raw or raw["unit_price_uzs"] is None:
            fail(
                "PRICE_REQUIRED",
                "Enter the invoice unit price.",
                field=prefix + ".unit_price_uzs",
            )
        price = number(
            raw["unit_price_uzs"],
            prefix + ".unit_price_uzs",
            whole=True,
            maximum=MAX_LINE_VALUE,
        )
        if (not free and price <= 0) or (free and price != 0):
            fail(
                "PRICE_REQUIRED",
                "Normal goods require a positive price; free goods require zero.",
                field=prefix + ".unit_price_uzs",
            )
        total = (quantity * price).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if total > MAX_LINE_VALUE:
            fail(
                "VALIDATION_ERROR",
                "Line inventory value exceeds capacity.",
                field=prefix + ".quantity",
            )
        cost = (total / base_quantity).quantize(
            Decimal(".0001"), rounding=ROUND_HALF_UP
        )
        if cost > QUANTITY_MAX:
            fail(
                "VALIDATION_ERROR",
                "Converted unit cost exceeds capacity.",
                field=prefix + ".unit_price_uzs",
            )
        change_reason = text(
            raw.get("price_change_reason", ""), prefix + ".price_change_reason"
        )
        if not free and known_price(link):
            change = abs(price - link.price) * 100 / link.price
            if change >= 30 and (not confirmed or not change_reason):
                fail(
                    "PRICE_CHANGE_CONFIRMATION_REQUIRED",
                    "Confirm this supplier price change with a reason.",
                    409,
                    field=prefix + ".price_change_reason",
                    details={
                        "line_index": index,
                        "old_price_uzs": link.price,
                        "new_price_uzs": price,
                        "difference_uzs": price - link.price,
                        "percentage": change.quantize(
                            Decimal(".0001"), rounding=ROUND_HALF_UP
                        ),
                    },
                )
        batch = text(raw.get("batch_number", ""), prefix + ".batch_number", 100)
        if item.track_batches and not batch:
            fail(
                "BATCH_REQUIRED",
                "Batch number is required.",
                field=prefix + ".batch_number",
            )
        try:
            expiry = purchase_date(
                raw.get("expiry_date"), prefix + ".expiry_date", optional=True
            )
        except MoneyValueError as exc:
            fail("VALIDATION_ERROR", str(exc), field=prefix + ".expiry_date")
        if item.track_expiry and expiry is None:
            fail(
                "EXPIRY_REQUIRED",
                "Expiry date is required.",
                field=prefix + ".expiry_date",
            )
        if expiry is not None and expiry <= header["invoice_date"]:
            fail(
                "VALIDATION_ERROR",
                "Expiry must be later than the invoice date.",
                field=prefix + ".expiry_date",
            )
        lot = (item.id, unit.id, batch, expiry)
        if lot in lots:
            fail(
                "DUPLICATE_INVOICE_LINE",
                "Merge duplicate item/unit/batch/expiry lines.",
                field=prefix,
            )
        if any(existing[:2] == lot[:2] for existing in lots) and not (
            item.track_batches or item.track_expiry
        ):
            fail(
                "DUPLICATE_INVOICE_LINE",
                "Separate lots require batch or expiry tracking.",
                field=prefix,
            )
        lots.add(lot)
        result.append(
            {
                "link": link,
                "item": item,
                "unit": unit,
                "quantity": quantity,
                "factor": factor,
                "base_quantity": base_quantity,
                "base_cost": cost,
                "price": price,
                "total": total,
                "is_free": free,
                "free_reason": reason,
                "price_change_confirmed": confirmed,
                "price_change_reason": change_reason,
                "batch_number": batch,
                "expiry_date": expiry,
                "notes": text(raw.get("notes", ""), prefix + ".notes"),
            }
        )
    total = sum((line["total"] for line in result), Decimal("0"))
    if total > MAX_INVOICE_VALUE:
        fail(
            "VALIDATION_ERROR",
            "Invoice total exceeds capacity.",
            field="declared_total_uzs",
        )
    if total != header["declared_total"]:
        fail(
            "INVOICE_TOTAL_MISMATCH",
            "Declared total differs from the calculated invoice total.",
            field="declared_total_uzs",
            details={"calculated_total_uzs": total},
        )
    if abs(supplier.current_balance + total) > MAX_INVOICE_VALUE:
        fail(
            "VALIDATION_ERROR",
            "Supplier balance exceeds capacity.",
            field="declared_total_uzs",
        )
    return result, total
