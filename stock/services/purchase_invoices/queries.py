"""Branch-scoped invoice and supplier catalog reads with bounded queries."""

from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import (
    Case,
    DecimalField,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from base.helpers.response import ServiceResponse
from base.money import MoneyValueError
from base.security.permissions import user_has_permission
from stock.models import (
    PurchaseReceiving,
    StockItemUnit,
    StockLevel,
    Supplier,
    SupplierStockItem,
)
from stock.services.purchase_validation import purchase_date
from .idempotency import InvoiceError, fail
from .serialization import local_time, serialize
from .validation import actor_scope, known_price


def pagination(queryset, page=1, per_page=100):
    try:
        page, per_page = int(page), int(per_page)
        if page < 1 or not 1 <= per_page <= 500:
            raise ValueError
    except (ValueError, TypeError):
        fail("VALIDATION_ERROR", "Invalid pagination.", field="page")
    paginator = Paginator(queryset, per_page)
    rows = paginator.get_page(page)
    return rows, {
        "page": rows.number,
        "per_page": per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": rows.has_next(),
        "has_previous": rows.has_previous(),
    }


def receivable_items(*, actor, supplier_id, params):
    try:
        branch = actor_scope(actor, "stock.supplier.view")
        if not user_has_permission(actor, "stock.catalog.view"):
            fail("STOCK_SCOPE_FORBIDDEN", "Stock catalog permission is required.", 403)
        supplier = Supplier.objects.filter(
            id=supplier_id, branch_id=branch, is_active=True, is_deleted=False
        ).first()
        if supplier is None:
            fail("SUPPLIER_NOT_FOUND", "Active supplier not found in this branch.", 404)
        factor = StockItemUnit.objects.filter(
            stock_item_id=OuterRef("stock_item_id"),
            unit_id=OuterRef("unit_id"),
            branch_id=branch,
            is_deleted=False,
        ).values("conversion_to_base")[:1]
        decimal_field = DecimalField(max_digits=15, decimal_places=6)
        queryset = (
            SupplierStockItem.objects.filter(
                supplier=supplier,
                branch_id=branch,
                is_active=True,
                is_deleted=False,
                stock_item__branch_id=branch,
                stock_item__is_active=True,
                stock_item__is_deleted=False,
                stock_item__is_purchasable=True,
                unit__is_active=True,
                unit__is_deleted=False,
                stock_item__base_unit__is_active=True,
                stock_item__base_unit__is_deleted=False,
            )
            .annotate(
                receiving_factor=Case(
                    When(unit_id=F("stock_item__base_unit_id"), then=Value(Decimal(1))),
                    default=Coalesce(
                        Subquery(factor),
                        Case(
                            When(
                                unit__base_unit_id=F("stock_item__base_unit_id"),
                                then=F("unit__conversion_factor"),
                            ),
                            default=Value(None),
                            output_field=decimal_field,
                        ),
                    ),
                    output_field=decimal_field,
                )
            )
            .filter(receiving_factor__gt=0)
            .select_related("stock_item__base_unit", "unit")
        )
        search = params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(stock_item__name__icontains=search)
                | Q(stock_item__sku__icontains=search)
                | Q(supplier_sku__icontains=search)
                | Q(supplier_name__icontains=search)
            )
        page, meta = pagination(
            queryset.order_by("stock_item__name", "id"),
            params.get("page", 1),
            params.get("per_page", 100),
        )
        rows = list(page)
        totals = dict(
            StockLevel.objects.filter(
                stock_item_id__in=[row.stock_item_id for row in rows],
                branch_id=branch,
                location__branch_id=branch,
                location__is_deleted=False,
                is_deleted=False,
            )
            .values("stock_item_id")
            .annotate(total=Sum("quantity"))
            .values_list("stock_item_id", "total")
        )
        data = []
        for row in rows:
            item, unit = row.stock_item, row.unit
            known = known_price(row)
            data.append(
                {
                    "supplier_item_id": row.id,
                    "supplier_id": supplier.id,
                    "stock_item_id": item.id,
                    "stock_item_name": item.name,
                    "stock_item_sku": item.sku,
                    "item_type": item.item_type,
                    "purchase_unit": {
                        "id": unit.id,
                        "name": unit.name,
                        "short_name": unit.short_name,
                        "decimal_places": min(4, unit.decimal_places),
                        "conversion_to_base": row.receiving_factor,
                    },
                    "base_unit": {
                        "id": item.base_unit_id,
                        "name": item.base_unit.name,
                        "short_name": item.base_unit.short_name,
                    },
                    "suggested_unit_price_uzs": int(row.price)
                    if known and row.price == row.price.to_integral_value()
                    else None,
                    "price_is_known": bool(
                        known and row.price == row.price.to_integral_value()
                    ),
                    "last_price_update": local_time(row.last_price_update),
                    "track_batches": item.track_batches,
                    "track_expiry": item.track_expiry,
                    "default_expiry_days": item.default_expiry_days,
                    "current_stock_base_quantity": totals.get(item.id, Decimal(0)),
                }
            )
        return ServiceResponse.success({"items": data, "pagination": meta})
    except InvoiceError as exc:
        return exc.result


def invoice_queryset(branch):
    return PurchaseReceiving.objects.filter(
        branch_id=branch,
        source_type="DIRECT_INVOICE",
        is_deleted=False,
        status="COMPLETED",
    ).prefetch_related(
        Prefetch(
            "replacements",
            queryset=PurchaseReceiving.objects.filter(
                branch_id=branch,
                is_deleted=False,
            ).only("id", "replaces_id"),
            to_attr="_invoice_replacements",
        )
    )


def detail(*, actor, invoice_id):
    try:
        branch = actor_scope(actor, "stock.purchase_invoice.view")
        invoice = invoice_queryset(branch).filter(id=invoice_id).first()
        if invoice is None:
            fail("INVOICE_NOT_FOUND", "Invoice not found in this branch.", 404)
        return ServiceResponse.success({"invoice": serialize(invoice, actor)})
    except InvoiceError as exc:
        return exc.result


def list_invoices(*, actor, params):
    try:
        branch = actor_scope(actor, "stock.purchase_invoice.view")
        query = invoice_queryset(branch)
        search = params.get("search", "").strip()
        if search:
            query = query.filter(
                Q(receiving_number__icontains=search)
                | Q(supplier_invoice_number__icontains=search)
            )
        for name, field in [
            ("supplier", "supplier_id"),
            ("location", "location_id"),
            ("creator", "received_by_id"),
            ("poster", "posted_by_id"),
            ("stock_item", "items__stock_item_id"),
        ]:
            value = params.get(name, params.get(name + "_id"))
            if value not in (None, ""):
                try:
                    value = int(value)
                    if value <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    fail(
                        "VALIDATION_ERROR", "Filter must be a positive ID.", field=name
                    )
                criteria = {field: value}
                if name == "stock_item":
                    criteria.update(
                        items__is_deleted=False, items__po_item__is_deleted=False
                    )
                query = query.filter(**criteria)
        status = params.get("status")
        if status:
            if status not in ("POSTED", "REVERSED"):
                fail(
                    "VALIDATION_ERROR",
                    "Status must be POSTED or REVERSED.",
                    field="status",
                )
            query = query.filter(reversed_at__isnull=status == "POSTED")
        for name, field in [
            ("invoice_date", "invoice_date"),
            ("posted_date", "completed_at__date"),
        ]:
            for side, lookup in [("from", "gte"), ("to", "lte")]:
                value = params.get(name + "_" + side)
                if value:
                    try:
                        parsed = purchase_date(value, name + "_" + side)
                    except MoneyValueError as exc:
                        fail("VALIDATION_ERROR", str(exc), field=name + "_" + side)
                    query = query.filter(**{field + "__" + lookup: parsed})
        query = query.distinct()
        total = query.aggregate(total=Sum("received_value_uzs"))["total"] or 0
        rows, meta = pagination(
            query.order_by("-invoice_date", "-completed_at", "-id"),
            params.get("page", 1),
            params.get("per_page", 20),
        )
        return ServiceResponse.success(
            {
                "invoices": [serialize(row, actor, detail=False) for row in rows],
                "pagination": meta,
                "total_uzs": int(total),
            }
        )
    except InvoiceError as exc:
        return exc.result
