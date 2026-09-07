from uuid import uuid4

import pytest
from django.db import connection

from base.models import User
from stock.models import SupplierPayment, SupplierPaymentAllocation, SupplierTransaction
from stock.services.supplier_ledger_service import SupplierPaymentService
from stock.services.supplier_opening_balance import (
    REFERENCE,
    SupplierOpeningBalanceService,
)
from stock.tests.purchase_invoices.test_concurrency import race
from .test_contract import register

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Requires independent PostgreSQL transactions",
    ),
]


@pytest.mark.parametrize("same_key", [True, False])
def test_simultaneous_opening_registrations_post_one_debt(opening_data, same_key):
    data = opening_data
    actions = [uuid4(), uuid4()]
    results = race(
        lambda index: SupplierOpeningBalanceService.register(
            data.supplier.id,
            actor=User.objects.get(pk=data.actor.pk),
            payload=dict(data.payload),
            action_id=actions[0 if same_key else index],
        )
    )
    assert sorted(status for _, status in results) == (
        [201, 201] if same_key else [201, 409]
    ), results
    if same_key:
        assert results[0] == results[1]
    assert SupplierTransaction.objects.filter(reference_type=REFERENCE).count() == 1
    data.supplier.refresh_from_db()
    assert data.supplier.current_balance == 5000000


@pytest.mark.parametrize("same_key", [True, False])
def test_simultaneous_opening_payments_cannot_overpay(opening_data, same_key):
    data = opening_data
    register(data)
    actions = [uuid4(), uuid4()]
    results = race(
        lambda index: SupplierPaymentService.pay(
            data.supplier.id,
            3000000,
            "BANK",
            actor=User.objects.get(pk=data.actor.pk),
            allocation_mode="AUTO_OLDEST_DUE",
            action_id=actions[0 if same_key else index],
        )
    )
    assert sorted(status for _, status in results) == (
        [201, 201] if same_key else [201, 422]
    ), results
    if same_key:
        assert results[0] == results[1]
    assert (
        SupplierPayment.objects.count()
        == SupplierPaymentAllocation.objects.count()
        == 1
    )
    data.supplier.refresh_from_db()
    data.bank.refresh_from_db()
    assert data.supplier.current_balance == 2000000 and data.bank.balance == 7000000


@pytest.mark.parametrize("same_key", [True, False])
def test_simultaneous_opening_payment_reversals_restore_once(opening_data, same_key):
    data = opening_data
    register(data)
    posted, status = SupplierPaymentService.pay(
        data.supplier.id,
        3000000,
        "BANK",
        actor=data.actor,
        allocation_mode="AUTO_OLDEST_DUE",
    )
    assert status == 201
    actions = [uuid4(), uuid4()]
    results = race(
        lambda index: SupplierPaymentService.reverse(
            posted["data"]["payment_id"],
            actor=User.objects.get(pk=data.actor.pk),
            reason="Test reversal",
            action_id=actions[0 if same_key else index],
        )
    )
    assert sorted(status for _, status in results) == (
        [200, 200] if same_key else [200, 409]
    ), results
    if same_key:
        assert results[0] == results[1]
    assert SupplierTransaction.objects.filter(type="PAYMENT_REVERSAL").count() == 1
    data.supplier.refresh_from_db()
    data.bank.refresh_from_db()
    assert data.supplier.current_balance == 5000000 and data.bank.balance == 10000000
