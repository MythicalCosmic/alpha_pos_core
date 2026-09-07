"""Audited initial supplier debt, posted to the existing supplier ledger."""

from datetime import date
from decimal import Decimal
import hashlib
import json
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.models import AuditLog
from base.money import MoneyValueError, whole_uzs, uzs_int
from base.security.permissions import user_has_permission
from base.services.branch_scope import resolve_actor_branch
from stock.models import Supplier, SupplierPayment, SupplierTransaction
from stock.services.supplier_integrity import validate_supplier_ledgers

REFERENCE = "SupplierOpeningBalance"
PERMISSION = "stock.supplier.opening.manage"
TASHKENT = ZoneInfo("Asia/Tashkent")


def can_register(actor):
    return bool(
        actor
        and not actor.is_deleted
        and actor.status == "ACTIVE"
        and actor.role in ("ADMIN", "MANAGER")
        and user_has_permission(actor, PERMISSION)
    )


def verified_opening(row):
    manifest = row.opening_balance_manifest
    return bool(
        row.reference_type == REFERENCE
        and row.reference_id == row.supplier_id
        and row.type == SupplierTransaction.Type.ADJUSTMENT
        and not row.is_deleted
        and row.opening_balance_date
        and row.amount > 0
        and row.balance_before == 0
        and row.balance_after == row.amount
        and isinstance(manifest, dict)
        and manifest.get("version") == 1
        and manifest.get("confirmed") is True
        and manifest.get("amount_uzs") == uzs_int(row.amount)
        and manifest.get("source_reference")
        and manifest.get("reason")
        and manifest.get("branch_id") == row.branch_id
        and manifest.get("supplier_id") == row.supplier_id
    )


def opening_paid(row):
    return row.opening_payment_allocations.filter(
        payment__supplier_id=row.supplier_id,
        payment__branch_id=row.branch_id,
        payment__status=SupplierPayment.Status.POSTED,
    ).aggregate(total=Sum("amount_uzs"))["total"] or Decimal(0)


def opening_remaining(row):
    return (
        max(row.amount - opening_paid(row), Decimal(0))
        if verified_opening(row)
        else Decimal(0)
    )


def opening_response(row):
    manifest = row.opening_balance_manifest
    return ServiceResponse.created(
        data={
            "opening_balance_id": row.id,
            "supplier_transaction_id": row.id,
            "supplier": {"id": row.supplier_id, "name": manifest["supplier_name"]},
            "amount_uzs": manifest["amount_uzs"],
            "as_of_date": row.opening_balance_date.isoformat(),
            "mode": manifest["mode"],
            "supplier_balance_before_uzs": manifest["stored_balance_before_uzs"],
            "supplier_balance_after_uzs": manifest["amount_uzs"],
            "source_reference": manifest["source_reference"],
            "reason": manifest["reason"],
            "posted_at": manifest["posted_at"],
            "posted_by": manifest["posted_by"],
        },
        message="Verified supplier opening debt registered",
    )


class SupplierOpeningBalanceService:
    @staticmethod
    def review(supplier_id, *, actor):
        branch = resolve_actor_branch(actor)
        if not user_has_permission(actor, "stock.supplier.balance.view") or not branch:
            return ServiceResponse.failure(
                "PERMISSION_DENIED", "Opening debt review is not permitted.", 403
            )
        supplier = Supplier.objects.filter(
            pk=supplier_id, branch_id=branch, is_deleted=False
        ).first()
        if supplier is None:
            return ServiceResponse.not_found("Supplier not found")
        if (
            supplier.currency != "UZS"
            or supplier.current_balance != supplier.current_balance.to_integral_value()
        ):
            return ServiceResponse.conflict(
                "SUPPLIER_LEDGER_RECONCILIATION_REQUIRED",
                "Opening debt review requires a whole-UZS supplier balance.",
            )
        evidence = validate_supplier_ledgers([supplier])[supplier.id]
        opening = SupplierTransaction.objects.filter(
            supplier=supplier, reference_type=REFERENCE
        ).first()
        has_history = SupplierTransaction.objects.filter(supplier=supplier).exists()
        if opening:
            state = (
                "REGISTERED"
                if verified_opening(opening) and evidence.valid
                else "RECONCILIATION_REQUIRED"
            )
        elif not has_history and supplier.current_balance > 0:
            state = "REVIEW_REQUIRED"
        elif not has_history and supplier.current_balance == 0:
            state = "NOT_REGISTERED"
        else:
            state = "EXISTING_HISTORY" if evidence.valid else "RECONCILIATION_REQUIRED"
        return ServiceResponse.success(
            data={
                "supplier_id": supplier.id,
                "status": state,
                "stored_balance_uzs": uzs_int(supplier.current_balance),
                "ledger_balance_uzs": uzs_int(evidence.ledger_balance),
                "ledger_valid": evidence.valid,
                "opening_balance_id": opening.id if opening else None,
                "remaining_opening_debt_uzs": uzs_int(opening_remaining(opening))
                if opening
                else 0,
                "allowed_actions": ["REGISTER_OPENING_BALANCE"]
                if can_register(actor)
                and state in ("REVIEW_REQUIRED", "NOT_REGISTERED")
                else [],
            }
        )

    @staticmethod
    @transaction.atomic
    def register(supplier_id, *, actor, payload, action_id):
        if not can_register(actor):
            return ServiceResponse.failure(
                "PERMISSION_DENIED",
                "Opening debt registration requires an authorized Manager or Admin.",
                403,
            )
        branch = str(resolve_actor_branch(actor) or "").strip()
        if not branch:
            return ServiceResponse.failure(
                "STOCK_SCOPE_FORBIDDEN", "An authorized branch is required.", 403
            )
        if not isinstance(payload, dict):
            return ServiceResponse.validation_error(
                {"body": ["A JSON object is required."]}
            )
        fields = {
            "amount_uzs",
            "as_of_date",
            "mode",
            "reason",
            "source_reference",
            "confirmed",
        }
        if set(payload) - fields:
            return ServiceResponse.validation_error(
                {"body": ["Unsupported opening balance fields."]}
            )
        errors = {}
        try:
            amount = whole_uzs(
                payload.get("amount_uzs"),
                "amount_uzs",
                positive=True,
                maximum=Decimal("9999999999999"),
            )
        except MoneyValueError as exc:
            errors["amount_uzs"] = [str(exc)]
        value = payload.get("as_of_date")
        try:
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value
            ):
                raise ValueError
            as_of = date.fromisoformat(value)
            if as_of > timezone.localdate(timezone=TASHKENT):
                raise ValueError
        except ValueError:
            errors["as_of_date"] = [
                "Use a valid YYYY-MM-DD date that is not in the future."
            ]
        mode = payload.get("mode")
        if mode not in ("RECONCILE_EXISTING", "CREATE"):
            errors["mode"] = ["Use RECONCILE_EXISTING or CREATE."]
        for field, maximum in (("reason", 1000), ("source_reference", 255)):
            value = payload.get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
                errors[field] = [f"Provide 1–{maximum} characters."]
        if payload.get("confirmed") is not True:
            errors["confirmed"] = [
                "Confirm that the opening debt was reviewed against its source."
            ]
        try:
            action_id = UUID(str(action_id))
        except (ValueError, TypeError, AttributeError):
            errors["idempotency_key"] = ["An idempotency action is required."]
        if errors:
            return ServiceResponse.validation_error(errors)
        normalized = {
            "supplier_id": supplier_id,
            "branch_id": branch,
            "actor_id": actor.id,
            "amount_uzs": uzs_int(amount),
            "as_of_date": as_of.isoformat(),
            "mode": mode,
            "reason": payload["reason"].strip(),
            "source_reference": payload["source_reference"].strip(),
            "confirmed": True,
        }
        fingerprint = hashlib.sha256(
            json.dumps(normalized, sort_keys=True).encode()
        ).hexdigest()
        supplier = (
            Supplier.objects.select_for_update()
            .filter(pk=supplier_id, branch_id=branch, is_deleted=False)
            .first()
        )
        if supplier is None:
            return ServiceResponse.not_found("Supplier not found")
        if not supplier.is_active or supplier.currency != "UZS":
            return ServiceResponse.failure(
                "SUPPLIER_OPENING_BALANCE_UNAVAILABLE",
                "Supplier must be active and use UZS.",
                422,
            )
        existing = SupplierTransaction.objects.filter(uuid=action_id).first()
        if existing:
            if (
                existing.reference_type != REFERENCE
                or existing.opening_balance_manifest.get("request_hash") != fingerprint
            ):
                return ServiceResponse.conflict(
                    "IDEMPOTENCY_KEY_REUSED",
                    "This action already identifies different opening debt data.",
                )
            return opening_response(existing)
        if SupplierTransaction.objects.filter(
            supplier=supplier, reference_type=REFERENCE
        ).exists():
            return ServiceResponse.conflict(
                "SUPPLIER_OPENING_BALANCE_ALREADY_REGISTERED",
                "This supplier already has an opening debt entry.",
            )
        # Include deleted history: deletion never authorizes a second opening.
        if (
            SupplierTransaction.objects.filter(supplier=supplier).exists()
            or SupplierPayment.objects.filter(supplier=supplier).exists()
        ):
            return ServiceResponse.conflict(
                "SUPPLIER_LEDGER_RECONCILIATION_REQUIRED",
                "Existing supplier history must be reconciled before an opening debt can be registered.",
            )
        before = supplier.current_balance
        expected = amount if mode == "RECONCILE_EXISTING" else Decimal(0)
        if before != expected:
            return ServiceResponse.conflict(
                "SUPPLIER_BALANCE_CHANGED",
                "The current supplier balance differs from the reviewed amount.",
                details={
                    "current_balance_uzs": uzs_int(before),
                    "expected_balance_uzs": uzs_int(expected),
                },
            )
        manifest = {
            "version": 1,
            **normalized,
            "request_hash": fingerprint,
            "supplier_name": supplier.name,
            "stored_balance_before_uzs": uzs_int(before),
            "posted_at": timezone.now().astimezone(TASHKENT).isoformat(),
            "posted_by": {
                "id": actor.id,
                "name": f"{actor.first_name} {actor.last_name}".strip(),
            },
        }
        row = SupplierTransaction.objects.create(
            uuid=action_id,
            supplier=supplier,
            branch_id=branch,
            type=SupplierTransaction.Type.ADJUSTMENT,
            amount=amount,
            balance_before=0,
            balance_after=amount,
            reference_type=REFERENCE,
            reference_id=supplier.id,
            opening_balance_date=as_of,
            opening_balance_manifest=manifest,
            performed_by=actor,
            note=manifest["reason"],
        )
        if mode == "CREATE":
            supplier.current_balance = amount
            supplier.save(update_fields=["current_balance", "updated_at"])
        AuditLog.record(
            actor=actor,
            action=AuditLog.Action.FINANCIAL_REPAIR,
            target_type=REFERENCE,
            target_id=row.id,
            branch_id=branch,
            metadata=manifest,
        )
        return opening_response(row)
