"""Immutable posting evidence with permission-aware API projections."""

import copy
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from base.security.permissions import user_has_permission
from .posting import actor_name

NUMBER_FIELDS = {
    "purchase_quantity",
    "purchase_unit_price_uzs",
    "base_quantity",
    "base_unit_cost_uzs",
    "line_total_uzs",
    "stock_quantity_before",
    "stock_quantity_after",
    "previous_average_cost_uzs",
    "new_average_cost_uzs",
    "conversion_to_base",
}


def local_time(value):
    return (
        timezone.localtime(value, ZoneInfo("Asia/Tashkent")).isoformat()
        if value
        else None
    )


def posted_manifest(receiving, lines, actor):
    public_lines = []
    for line in lines:
        saved = line.invoice_snapshot
        public_lines.append(
            {
                "id": line.id,
                "supplier_item_id": line.supplier_stock_item_id,
                "stock_item_id": line.stock_item_id,
                **{
                    key: saved[key]
                    for key in (
                        "stock_item_name",
                        "purchase_quantity",
                        "purchase_unit",
                        "purchase_unit_price_uzs",
                        "base_quantity",
                        "base_unit",
                        "base_unit_cost_uzs",
                        "line_total_uzs",
                        "conversion_to_base",
                        "stock_quantity_before",
                        "stock_quantity_after",
                        "previous_average_cost_uzs",
                        "new_average_cost_uzs",
                    )
                },
                "stock_transaction_id": line.stock_transaction_id,
                "batch_id": line.batch_created_id,
                "batch_number": line.batch_number,
                "expiry_date": line.expiry_date.isoformat()
                if line.expiry_date
                else None,
                "is_free": line.is_free,
                "free_reason": line.free_reason,
                "notes": line.notes,
                "price_change_confirmed": saved["price_change_confirmed"],
                "price_change_reason": saved["price_change_reason"],
                "quality_status": "PASSED",
            }
        )
    return {
        "id": receiving.id,
        "receiving_number": receiving.receiving_number,
        "supplier_invoice_number": receiving.supplier_invoice_number,
        "source_type": "DIRECT_INVOICE",
        "status": "POSTED",
        "invoice_date": receiving.invoice_date.isoformat(),
        "posted_at": local_time(receiving.completed_at),
        "supplier": {"id": receiving.supplier_id, "name": receiving.supplier.name},
        "location": {"id": receiving.location_id, "name": receiving.location.name},
        "currency": "UZS",
        "line_count": len(lines),
        "subtotal_uzs": int(receiving.received_value_uzs),
        "total_uzs": int(receiving.received_value_uzs),
        "lines": public_lines,
        "supplier_transaction_id": receiving.supplier_transaction_id,
        "purchase_order_id": receiving.purchase_order_id,
        "receiving_id": receiving.id,
        "supplier_balance_before_uzs": str(receiving.supplier_balance_before),
        "supplier_balance_after_uzs": str(receiving.supplier_balance_after),
        "created_by": {
            "id": receiving.received_by_id,
            "name": actor_name(receiving.received_by),
        },
        "posted_by": {"id": actor.id, "name": actor_name(actor)},
        "notes": receiving.notes,
        "replaces_invoice_id": receiving.replaces_id,
        "action_history": [
            {
                "action": "POSTED",
                "at": local_time(receiving.completed_at),
                "actor_id": actor.id,
            }
        ],
    }


def protect_balances(data, actor):
    for field in ("supplier_balance_before_uzs", "supplier_balance_after_uzs"):
        if not user_has_permission(actor, "stock.supplier.balance.view"):
            data.pop(field, None)
        elif field in data:
            data[field] = Decimal(data[field])


def protect_replay(body, actor):
    """A cached response never extends a permission that has been revoked."""
    invoice = body.get("data", {}).get("invoice")
    if invoice:
        protect_balances(invoice, actor)
        protect_balances(invoice.get("reversal", {}), actor)
        if actor.role not in ("ADMIN", "MANAGER") or not user_has_permission(
            actor, "stock.purchase_invoice.correct"
        ):
            invoice["allowed_actions"] = []
    return body


def serialize(receiving, actor, *, detail=True):
    data = copy.deepcopy(receiving.posting_manifest.get("invoice", {}))
    if not data:
        raise ValueError("Posted invoice has no posting manifest")
    protect_balances(data, actor)
    for line in data["lines"]:
        for key in NUMBER_FIELDS:
            line[key] = Decimal(line[key])
    can_correct = actor.role in ("ADMIN", "MANAGER") and user_has_permission(
        actor, "stock.purchase_invoice.correct"
    )
    data["allowed_actions"] = (
        ["reverse"] if can_correct and receiving.reversed_at is None else []
    )
    if receiving.reversed_at:
        data["status"] = "REVERSED"
        reversal = copy.deepcopy(receiving.reversal_manifest)
        protect_balances(reversal, actor)
        data["reversal"] = reversal
        data["action_history"].append(
            {
                "action": "REVERSED",
                "at": local_time(receiving.reversed_at),
                "actor_id": receiving.reversed_by_id,
                "reason": receiving.reversal_reason,
            }
        )
    data["replacement_invoice_ids"] = [
        row.id for row in getattr(receiving, "_invoice_replacements", [])
    ]
    if not detail:
        for key in ("lines", "action_history", "reversal", "notes"):
            data.pop(key, None)
    return data
