import json
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from base.models import User
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from stock.models import (
    Supplier,
    SupplierPaymentAllocation,
    SupplierStockItem,
    SupplierTransaction,
)
from stock.services.purchase_invoices.commands import DirectPurchaseInvoiceService
from stock.services.supplier_opening_balance import (
    PERMISSION,
    REFERENCE,
    SupplierOpeningBalanceService,
)
from .test_contract import pay, receipt, register, state

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("raw", ["5e6", "NaN", "Infinity", "true", "5000000.01"])
def test_http_rejects_invalid_money_without_lossy_json_conversion(opening_data, raw):
    data = opening_data
    payload = json.dumps(data.payload).replace("5000000", raw)
    before = state(data)
    response = data.client.post(
        f"/api/admins/stock/suppliers/{data.supplier.id}/opening-balance/",
        payload,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="invalid-numeric-" + raw,
    )
    assert response.status_code == 422, response.content
    assert "amount_uzs" in response.json()["errors"]
    assert state(data) == before


@pytest.mark.parametrize("branch", ["branch1", "branch2"])
def test_explicit_opening_from_another_supplier_is_forbidden(opening_data, branch):
    data = opening_data
    registered, _ = register(data)
    other = Supplier.objects.create(
        name="Another supplier", current_balance=5000000, branch_id=branch
    )
    actor = User.objects.create(
        first_name="Other reviewer",
        email="other@test.local",
        password="!",
        role="MANAGER",
        status="ACTIVE",
        permissions=DEFAULT_ROLE_PERMISSIONS["MANAGER"],
        branch_id=branch,
    )
    foreign, status = SupplierOpeningBalanceService.register(
        other.id, actor=actor, payload=data.payload, action_id=uuid4()
    )
    assert status == 201
    before = state(data)
    response, status = pay(
        data,
        allocation_mode="EXPLICIT",
        allocations=[
            {
                "opening_balance_id": foreign["data"]["opening_balance_id"],
                "amount_uzs": 3000000,
            }
        ],
    )
    assert status == 422, response
    assert state(data) == before
    assert (
        registered["data"]["opening_balance_id"]
        != foreign["data"]["opening_balance_id"]
    )


def test_opening_and_real_direct_invoice_share_one_payment(opening_data, invoice_data):
    data = opening_data
    register(data)
    source = invoice_data
    link = SupplierStockItem.objects.create(
        supplier=data.supplier,
        stock_item=source.item,
        unit=source.kg,
        price=100000,
        price_is_known=True,
        branch_id="branch1",
    )
    payload = {
        **source.payload,
        "supplier_id": data.supplier.id,
        "lines": [{**source.payload["lines"][0], "supplier_item_id": link.id}],
    }
    body, status = DirectPurchaseInvoiceService.receive(
        actor=data.actor, payload=payload, idempotency_key="opening-plus-invoice"
    )
    assert status == 201, body
    paid, status = pay(data, 5500000)
    assert status == 201, paid
    assert paid["data"]["supplier_balance_after_uzs"] == 0
    assert len(paid["data"]["allocations"]) == 2
    source.level.refresh_from_db()
    assert source.level.quantity == 15


@override_settings(DEPLOYMENT_MODE="cloud")
def test_sync_cannot_create_or_rewrite_verified_opening(opening_data):
    from base.services.sync.receiver import CloudReceiver

    data = opening_data
    registered, _ = register(data)
    row = SupplierTransaction.objects.get(pk=registered["data"]["opening_balance_id"])
    before = state(data)
    for creating in (False, True):
        payload = row.to_sync_dict()
        payload.update(sync_version=row.sync_version + 50, note="Peer rewrite")
        if creating:
            payload["uuid"] = str(uuid4())
        else:
            for field in (
                "reference_type",
                "opening_balance_date",
                "opening_balance_manifest",
            ):
                payload.pop(field, None)
        result = CloudReceiver._create_or_update(
            SupplierTransaction, payload, "branch1"
        )
        assert result.reason_code == "SUPPLIER_OPENING_COMMAND_REQUIRED"
    row.refresh_from_db()
    assert row.note == data.payload["reason"] and state(data) == before


def test_database_allocation_requires_exactly_one_debt_target(opening_data):
    data = opening_data
    register(data)
    po, _ = receipt(data)
    body, status = pay(data)
    assert status == 201
    allocation = SupplierPaymentAllocation.objects.get(
        payment_id=body["data"]["payment_id"]
    )
    for changes in ({"opening_balance": None}, {"purchase_order": po}):
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierPaymentAllocation.objects.filter(pk=allocation.pk).update(**changes)


@pytest.mark.django_db(transaction=True)
def test_upgrade_preserves_unreviewed_balances_and_existing_payment_history(
    opening_data,
):
    data = opening_data
    # Establish a legacy receipt-backed payment before migrating from the old schema.
    data.supplier.current_balance = 0
    data.supplier.save()
    receipt(data)
    assert pay(data, 500000)[1] == 201
    manual = Supplier.objects.create(
        name="Unreviewed manual balance", current_balance=3000000, branch_id="branch1"
    )
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [("stock", "0019_direct_invoice_backfill")]
    try:
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps

        def values():
            return {
                name: list(
                    apps.get_model("stock", name).objects.order_by("id").values()
                )
                for name in (
                    "Supplier",
                    "SupplierTransaction",
                    "SupplierPayment",
                    "SupplierPaymentAllocation",
                )
            }

        before = values()
        output = StringIO()
        call_command("audit_supplier_opening_migration", stdout=output)
        impact = json.loads(output.getvalue())
        assert impact["suppliers_requiring_opening_review"] == 1
        assert (
            impact["financial_rows_to_create"] == 0
            and impact["existing_balances_changed"] is False
        )
        assert values() == before
        OldUser = apps.get_model("base", "User")
        OldUser.objects.filter(pk=data.actor.pk).update(permissions=[])
        MigrationExecutor(connection).migrate(latest)
        assert values() == before
        assert Supplier.objects.get(pk=manual.pk).current_balance == 3000000
        assert not SupplierTransaction.objects.filter(supplier=manual).exists()
        assert not SupplierTransaction.objects.filter(reference_type=REFERENCE).exists()
        assert PERMISSION in User.objects.get(pk=data.actor.pk).permissions
    finally:
        MigrationExecutor(connection).migrate(latest)
