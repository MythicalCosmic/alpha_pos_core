from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction
from django.utils import timezone

from base.models import AuditLog, TreasuryTransaction
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from hr.models import Expense
from stock.models import (
    PurchaseOrder,
    PurchaseReceiving,
    PurchaseReceivingCorrection,
    StockLevel,
    StockTransaction,
    Supplier,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierTransaction,
)
from stock.services.supplier_integrity import validate_supplier_ledgers
from stock.services.supplier_ledger_service import (
    SupplierLedgerService,
    SupplierPaymentService,
)
from stock.services.supplier_opening_balance import (
    REFERENCE,
    SupplierOpeningBalanceService,
    opening_remaining,
)
from stock.tests.test_money_control_contract import _catalog, _purchase_order

pytestmark = pytest.mark.django_db


def register(data, payload=None, action=None):
    return SupplierOpeningBalanceService.register(
        data.supplier.id,
        actor=data.actor,
        payload=payload or data.payload,
        action_id=action or uuid4(),
    )


def state(data):
    data.supplier.refresh_from_db()
    data.bank.refresh_from_db()
    return (
        data.supplier.current_balance,
        data.bank.balance,
        SupplierTransaction.objects.count(),
        SupplierPayment.objects.count(),
        SupplierPaymentAllocation.objects.count(),
        TreasuryTransaction.objects.count(),
        StockLevel.objects.count(),
        StockTransaction.objects.count(),
        Expense.objects.count(),
        AuditLog.objects.count(),
    )


def pay(data, amount=3000000, **kwargs):
    return SupplierPaymentService.pay(
        data.supplier.id,
        amount,
        "BANK",
        actor=data.actor,
        allocation_mode=kwargs.pop("allocation_mode", "AUTO_OLDEST_DUE"),
        **kwargs,
    )


def receipt(data, amount=1000000):
    _base, unit, location, item = _catalog()
    po, _line = _purchase_order(
        data.actor, data.supplier, location, item, unit, total=str(amount)
    )
    receiving = PurchaseReceiving.objects.create(
        receiving_number="RCV-" + uuid4().hex[:12],
        purchase_order=po,
        location=location,
        received_by=data.actor,
        received_date=timezone.localdate(),
        status="COMPLETED",
        completed_at=timezone.now(),
        received_value_uzs=amount,
        branch_id="branch1",
    )
    SupplierLedgerService.record_purchase(
        data.supplier.id,
        Decimal(amount),
        reference_type="PurchaseReceiving",
        reference_id=receiving.id,
        performed_by=data.actor,
    )
    return po, receiving


@pytest.mark.parametrize("mode", ["RECONCILE_EXISTING", "CREATE"])
def test_opening_posts_one_audited_ledger_entry_without_inventory_or_money_movement(
    opening_data, mode
):
    data = opening_data
    payload = {**data.payload, "mode": mode}
    if mode == "CREATE":
        data.supplier.current_balance = 0
        data.supplier.save()
    before = state(data)
    body, status = register(data, payload)
    assert status == 201, body
    after = state(data)
    assert after[0] == 5000000 and after[1] == before[1]
    assert after[2] == before[2] + 1 and after[3:9] == before[3:9]
    assert after[9] == before[9] + 1
    row = SupplierTransaction.objects.get(pk=body["data"]["opening_balance_id"])
    assert (
        row.type == "ADJUSTMENT"
        and row.balance_before == 0
        and row.balance_after == 5000000
    )
    assert row.opening_balance_manifest["confirmed"] is True
    assert (
        row.opening_balance_manifest["source_reference"]
        == data.payload["source_reference"]
    )
    assert validate_supplier_ledgers([data.supplier])[data.supplier.id].valid
    assert PurchaseOrder.objects.count() == PurchaseReceiving.objects.count() == 0


def test_unreviewed_balance_is_blocked_with_clear_error_and_no_mutation(opening_data):
    data = opening_data
    before = state(data)
    body, status = pay(data)
    assert status == 409 and body["code"] == "SUPPLIER_OPENING_BALANCE_REVIEW_REQUIRED"
    assert state(data) == before
    response = data.client.get(
        f"/api/admins/stock/suppliers/{data.supplier.id}/opening-balance/"
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_uzs", True),
        ("amount_uzs", -1),
        ("amount_uzs", 0),
        ("amount_uzs", "NaN"),
        ("amount_uzs", "Infinity"),
        ("amount_uzs", "3e6"),
        ("amount_uzs", "1.5"),
        ("amount_uzs", "10000000000000"),
        ("confirmed", False),
        ("confirmed", "true"),
        ("reason", ""),
        ("reason", "x" * 1001),
        ("source_reference", "  "),
        ("as_of_date", "2999-01-01"),
        ("as_of_date", "20260230"),
        ("as_of_date", "2026-02-30"),
        ("mode", "AUTO"),
    ],
)
def test_invalid_opening_does_not_change_any_financial_state(
    opening_data, field, value
):
    data = opening_data
    before = state(data)
    body, status = register(data, {**data.payload, field: value})
    assert status == 422 and field in body["errors"], body
    assert state(data) == before


def test_existing_amount_must_match_review_and_create_requires_zero(opening_data):
    data = opening_data
    for payload in (
        {**data.payload, "amount_uzs": 4000000},
        {**data.payload, "mode": "CREATE"},
    ):
        before = state(data)
        body, status = register(data, payload)
        assert status == 409 and body["code"] == "SUPPLIER_BALANCE_CHANGED"
        assert state(data) == before


def test_existing_or_deleted_history_cannot_be_replaced_with_an_opening(opening_data):
    data = opening_data
    row = SupplierTransaction.objects.create(
        supplier=data.supplier,
        branch_id="branch1",
        type="ADJUSTMENT",
        amount=5000000,
        balance_before=0,
        balance_after=5000000,
    )
    for deleted in (False, True):
        SupplierTransaction.objects.filter(pk=row.pk).update(is_deleted=deleted)
        before = state(data)
        body, status = register(data)
        assert (
            status == 409 and body["code"] == "SUPPLIER_LEDGER_RECONCILIATION_REQUIRED"
        )
        assert state(data) == before


def test_opening_idempotency_survives_payment_and_rejects_changed_payload(opening_data):
    data = opening_data
    action = uuid4()
    first = register(data, action=action)
    assert pay(data)[1] == 201
    assert register(data, action=action) == first
    body, status = register(
        data, {**data.payload, "reason": "Changed reason"}, action=action
    )
    assert status == 409 and body["code"] == "IDEMPOTENCY_KEY_REUSED"
    body, status = register(data)
    assert (
        status == 409 and body["code"] == "SUPPLIER_OPENING_BALANCE_ALREADY_REGISTERED"
    )
    assert SupplierTransaction.objects.filter(reference_type=REFERENCE).count() == 1


def test_api_replay_and_changed_payload_conflict(opening_data):
    data = opening_data
    path = f"/api/admins/stock/suppliers/{data.supplier.id}/opening-balance/"
    kwargs = {
        "content_type": "application/json",
        "HTTP_IDEMPOTENCY_KEY": "opening-api-1",
    }
    first = data.client.post(path, data.payload, **kwargs)
    second = data.client.post(path, data.payload, **kwargs)
    assert first.status_code == second.status_code == 201, first.content
    assert first.content == second.content
    changed = data.client.post(path, {**data.payload, "reason": "changed"}, **kwargs)
    assert changed.status_code == 409
    assert (
        data.client.post(
            path, data.payload, content_type="application/json"
        ).status_code
        == 422
    )


@pytest.mark.parametrize("role", ["WAREHOUSE", "CASHIER"])
def test_opening_registration_is_not_a_warehouse_or_cashier_action(opening_data, role):
    data = opening_data
    data.actor.role = role
    data.actor.permissions = ["*"]
    data.actor.save()
    before = state(data)
    body, status = register(data)
    assert status == 403
    assert state(data) == before


def test_manager_permission_and_cross_branch_ids_are_checked(opening_data):
    data = opening_data
    data.actor.permissions = []
    data.actor.save()
    assert register(data)[1] == 403
    data.actor.permissions = DEFAULT_ROLE_PERMISSIONS["MANAGER"]
    data.actor.branch_id = "branch2"
    data.actor.save()
    assert register(data)[1] == 404
    data.actor.branch_id = "branch1"
    data.actor.save()
    data.supplier.is_active = False
    data.supplier.save()
    assert register(data)[1] == 422


def test_opening_bank_payment_and_reversal_restore_debt_and_source_once(opening_data):
    data = opening_data
    registered, _ = register(data)
    action = uuid4()
    body, status = pay(data, action_id=action, fee_uzs=15000)
    assert status == 201, body
    assert body["data"]["supplier_balance_after_uzs"] == 2000000
    assert body["data"]["source_balance_after_uzs"] == 6985000
    allocation = body["data"]["allocations"][0]
    assert allocation["opening_balance_id"] == registered["data"]["opening_balance_id"]
    assert (
        allocation["purchase_order_id"] is None
        and allocation["remaining_uzs"] == 2000000
    )
    assert pay(data, action_id=action, fee_uzs=15000) == (body, status)
    assert (
        PurchaseOrder.objects.count()
        == PurchaseReceiving.objects.count()
        == Expense.objects.count()
        == 0
    )
    reverse_action = uuid4()
    reverse = SupplierPaymentService.reverse(
        body["data"]["payment_id"],
        actor=data.actor,
        reason="Bank transfer recalled",
        action_id=reverse_action,
    )
    assert reverse[1] == 200, reverse
    assert (
        SupplierPaymentService.reverse(
            body["data"]["payment_id"],
            actor=data.actor,
            reason="Bank transfer recalled",
            action_id=reverse_action,
        )
        == reverse
    )
    data.supplier.refresh_from_db()
    data.bank.refresh_from_db()
    assert data.supplier.current_balance == 5000000 and data.bank.balance == 10000000
    row = SupplierTransaction.objects.get(pk=allocation["opening_balance_id"])
    assert opening_remaining(row) == 5000000
    assert validate_supplier_ledgers([data.supplier])[data.supplier.id].valid


@pytest.mark.parametrize("explicit", [False, True])
def test_payment_can_cover_opening_debt_and_received_invoice(opening_data, explicit):
    data = opening_data
    registered, _ = register(data)
    po, _rcv = receipt(data)
    kwargs = {}
    if explicit:
        kwargs = {
            "allocation_mode": "EXPLICIT",
            "allocations": [
                {
                    "opening_balance_id": registered["data"]["opening_balance_id"],
                    "amount_uzs": 5000000,
                },
                {"purchase_order_id": po.id, "amount_uzs": 1000000},
            ],
        }
    body, status = pay(data, 6000000, **kwargs)
    assert status == 201, body
    assert sum(row["amount_uzs"] for row in body["data"]["allocations"]) == 6000000
    assert len(body["data"]["allocations"]) == 2
    assert body["data"]["supplier_balance_after_uzs"] == 0
    po.refresh_from_db()
    assert po.amount_paid == 1000000


def test_auto_allocation_uses_opening_date_before_new_invoice_due_date(opening_data):
    data = opening_data
    register(data)
    po, _rcv = receipt(data)
    body, status = pay(data)
    assert status == 201
    assert (
        len(body["data"]["allocations"]) == 1
        and body["data"]["allocations"][0]["allocation_type"] == "OPENING_BALANCE"
    )
    po.refresh_from_db()
    assert po.amount_paid == 0


def test_explicit_foreign_duplicate_and_ambiguous_debt_targets_are_rejected(
    opening_data,
):
    data = opening_data
    registered, _ = register(data)
    opening_id = registered["data"]["opening_balance_id"]
    po, _rcv = receipt(data)
    foreign = Supplier.objects.create(name="Foreign", branch_id="branch2")
    for allocations in (
        [{"opening_balance_id": opening_id + 999999, "amount_uzs": 3000000}],
        [
            {
                "opening_balance_id": opening_id,
                "purchase_order_id": po.id,
                "amount_uzs": 3000000,
            }
        ],
        [
            {"opening_balance_id": opening_id, "amount_uzs": 1000000},
            {"opening_balance_id": opening_id, "amount_uzs": 2000000},
        ],
        [{"purchase_order_id": 1.9, "amount_uzs": 3000000}],
        [{"opening_balance_id": True, "amount_uzs": 3000000}],
    ):
        before = state(data)
        assert pay(data, allocation_mode="EXPLICIT", allocations=allocations)[1] == 422
        assert state(data) == before
    SupplierTransaction.objects.filter(pk=opening_id).update(supplier=foreign)
    assert (
        pay(
            data,
            allocation_mode="EXPLICIT",
            allocations=[{"opening_balance_id": opening_id, "amount_uzs": 3000000}],
        )[1]
        >= 400
    )


def test_generic_adjustment_does_not_implicitly_become_payable_opening_debt(
    opening_data,
):
    data = opening_data
    SupplierTransaction.objects.create(
        supplier=data.supplier,
        branch_id="branch1",
        type="ADJUSTMENT",
        amount=5000000,
        balance_before=0,
        balance_after=5000000,
    )
    before = state(data)
    body, status = pay(data)
    assert status == 422 and body["code"] == "SUPPLIER_PAYMENT_ALLOCATION_INCOMPLETE"
    assert state(data) == before


def test_insufficient_funds_and_audit_failure_roll_back(opening_data, monkeypatch):
    data = opening_data
    before = state(data)
    with monkeypatch.context() as patch:
        patch.setattr(
            AuditLog,
            "record",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("audit failure")),
        )
        with pytest.raises(RuntimeError):
            register(data)
    assert state(data) == before
    register(data)
    before = state(data)
    body, status = SupplierPaymentService.pay(
        data.supplier.id,
        3000000,
        "SAFE",
        actor=data.actor,
        allocation_mode="AUTO_OLDEST_DUE",
    )
    assert status >= 400
    assert state(data) == before


def test_failure_after_first_allocation_rolls_back_whole_payment(
    opening_data, monkeypatch
):
    data = opening_data
    register(data)
    receipt(data)
    before = state(data)
    create = SupplierPaymentAllocation.objects.create
    count = 0

    def fail_second(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("second allocation failed")
        return create(**kwargs)

    monkeypatch.setattr(SupplierPaymentAllocation.objects, "create", fail_second)
    with pytest.raises(RuntimeError):
        pay(data, 6000000)
    assert state(data) == before


def test_received_principal_excludes_approved_supplier_returns(opening_data):
    data = opening_data
    register(data)
    po, receiving = receipt(data)
    correction = PurchaseReceivingCorrection.objects.create(
        receiving=receiving,
        reason="Partial supplier return",
        status="APPROVED",
        requested_by=data.actor,
        reviewed_by=data.actor,
        branch_id="branch1",
    )
    SupplierLedgerService.record_return(
        data.supplier.id,
        Decimal(250000),
        reference_type="PurchaseReceivingCorrection",
        reference_id=correction.id,
        performed_by=data.actor,
    )
    assert SupplierPaymentService._received_principal(po) == 750000
    body, status = pay(
        data,
        1000000,
        allocation_mode="EXPLICIT",
        allocations=[{"purchase_order_id": po.id, "amount_uzs": 1000000}],
    )
    assert status == 422, body
    assert (
        pay(
            data,
            750000,
            allocation_mode="EXPLICIT",
            allocations=[{"purchase_order_id": po.id, "amount_uzs": 750000}],
        )[1]
        == 201
    )


def test_admin_forms_do_not_allow_direct_balance_edits(opening_data):
    from django.test import RequestFactory
    from django.contrib.auth import get_user_model

    request = RequestFactory().get("/admin/stock/supplier/")
    request.user = get_user_model().objects.create_superuser(
        "opening-admin", password="test-password"
    )
    model_admin = admin.site._registry[Supplier]
    assert "current_balance" not in model_admin.get_form(request).base_fields
    assert (
        "current_balance"
        not in model_admin.get_form(request, opening_data.supplier).base_fields
    )


def test_opening_evidence_is_immutable_and_has_database_uniqueness(opening_data):
    data = opening_data
    registered, _ = register(data)
    row = SupplierTransaction.objects.get(pk=registered["data"]["opening_balance_id"])
    row.note = "changed"
    with pytest.raises(TypeError):
        row.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        row.pk = None
        row.uuid = uuid4()
        row.save()
