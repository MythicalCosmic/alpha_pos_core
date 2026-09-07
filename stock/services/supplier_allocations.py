"""Allocate payments to verified opening debt or posted purchase receipts."""

from datetime import datetime, time
from decimal import Decimal
import re

from base.helpers.response import ServiceResponse
from base.money import MoneyValueError, whole_uzs, uzs_int
from stock.models import PurchaseOrder, SupplierPayment, SupplierTransaction
from .supplier_opening_balance import (
    REFERENCE,
    TASHKENT,
    opening_remaining,
    verified_opening,
)


def _identity(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not re.fullmatch(r"[1-9]\d*", str(value))
    ):
        raise ValueError
    parsed = int(value)
    if parsed > 9223372036854775807:
        raise ValueError
    return parsed


def plan_allocations(supplier, principal, mode, allocations):
    from .supplier_ledger_service import SupplierPaymentService

    if mode == SupplierPayment.AllocationMode.EXPLICIT:
        if not isinstance(allocations, list) or not allocations:
            return None, ServiceResponse.validation_error(
                {"allocations": ["At least one allocation is required."]}
            )
        parsed = {}
        errors = {}
        for index, value in enumerate(allocations):
            prefix = f"allocations.{index}"
            if not isinstance(value, dict):
                errors[prefix] = ["Each allocation must be an object."]
                continue
            targets = [
                name
                for name in ("purchase_order_id", "opening_balance_id")
                if value.get(name) is not None
            ]
            if len(targets) != 1:
                errors[prefix] = [
                    "Choose exactly one purchase_order_id or opening_balance_id."
                ]
                continue
            kind = targets[0]
            try:
                target = (kind, _identity(value[kind]))
            except (ValueError, TypeError):
                errors[prefix + "." + kind] = ["Use a positive integer ID."]
                continue
            if target in parsed:
                errors[prefix + "." + kind] = ["Each debt entry may appear once."]
                continue
            try:
                amount = whole_uzs(
                    value.get("amount_uzs"),
                    prefix + ".amount_uzs",
                    positive=True,
                    maximum=Decimal("9999999999999"),
                )
            except MoneyValueError as exc:
                errors[prefix + ".amount_uzs"] = [str(exc)]
                continue
            parsed[target] = amount
        if errors:
            return None, ServiceResponse.validation_error(errors)
        if sum(parsed.values(), Decimal(0)) != principal:
            return None, ServiceResponse.validation_error(
                {"allocations": ["Allocation total must equal amount_uzs."]}
            )
        order_ids = [pk for (kind, pk) in parsed if kind == "purchase_order_id"]
        opening_ids = [pk for (kind, pk) in parsed if kind == "opening_balance_id"]
    else:
        parsed = None
        order_ids = opening_ids = None

    # Caller owns the supplier lock. Use the same target-lock order for every payment.
    orders = PurchaseOrder.objects.select_for_update().filter(
        supplier=supplier, branch_id=supplier.branch_id, is_deleted=False
    )
    openings = SupplierTransaction.objects.select_for_update().filter(
        supplier=supplier,
        branch_id=supplier.branch_id,
        is_deleted=False,
        reference_type=REFERENCE,
    )
    if parsed is not None:
        orders = orders.filter(pk__in=order_ids)
        openings = openings.filter(pk__in=opening_ids)
    else:
        orders = orders.exclude(status=PurchaseOrder.Status.CANCELED)
    orders = list(orders.order_by("pk"))
    openings = list(openings.order_by("pk"))
    if parsed is not None and len(orders) + len(openings) != len(parsed):
        return None, ServiceResponse.validation_error(
            {
                "allocations": [
                    "A debt entry is missing or belongs to another supplier or branch."
                ]
            }
        )

    targets = []
    for po in orders:
        if parsed is not None:
            amount = parsed[("purchase_order_id", po.id)]
            if po.status == PurchaseOrder.Status.CANCELED:
                return None, ServiceResponse.validation_error(
                    {
                        "allocations": [
                            "Canceled purchase orders cannot receive payments."
                        ]
                    }
                )
            error = SupplierPaymentService._allocation_error(po, amount)
            if error:
                return None, error
            targets.append((po, amount))
        else:
            paid = SupplierPaymentService._canonical_paid(po)
            if paid == po.amount_paid and po.currency == "UZS":
                available = max(
                    SupplierPaymentService._received_principal(po) - paid, Decimal(0)
                )
                if available:
                    targets.append((po, available))
    for opening in openings:
        if not verified_opening(opening):
            return None, ServiceResponse.conflict(
                "SUPPLIER_LEDGER_RECONCILIATION_REQUIRED",
                "Opening debt evidence requires reconciliation.",
            )
        available = opening_remaining(opening)
        if parsed is not None:
            amount = parsed[("opening_balance_id", opening.id)]
            if amount > available:
                return None, ServiceResponse.validation_error(
                    {
                        "allocations": [
                            f"Opening debt allocation exceeds {uzs_int(available)} UZS."
                        ]
                    }
                )
            targets.append((opening, amount))
        elif available:
            targets.append((opening, available))
    if parsed is not None:
        return targets, None

    def due(target):
        row = target[0]
        if isinstance(row, SupplierTransaction):
            return (
                False,
                datetime.combine(row.opening_balance_date, time.min, tzinfo=TASHKENT),
                0,
                row.id,
            )
        return (
            row.payment_due_date is None,
            row.payment_due_date or datetime.max.replace(tzinfo=TASHKENT),
            1,
            row.id,
        )

    targets.sort(key=due)
    remaining = principal
    output = []
    for target, available in targets:
        amount = min(remaining, available)
        output.append((target, amount))
        remaining -= amount
        if not remaining:
            return output, None
    return None, ServiceResponse.failure(
        "SUPPLIER_PAYMENT_ALLOCATION_INCOMPLETE",
        "Verified opening debts and unpaid receipts cannot fully allocate this payment.",
        422,
        errors={"allocations": ["Unallocated principal remains."]},
        details={"unallocated_uzs": uzs_int(remaining)},
    )
