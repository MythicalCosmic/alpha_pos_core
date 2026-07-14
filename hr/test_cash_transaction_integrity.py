from decimal import Decimal

import pytest
from django.test import override_settings

from base.models import CashRegister
from hr.models import CashTransaction
from hr.repositories import CashTransactionRepository
from hr.services.cash_transaction_service import CashTransactionService


pytestmark = pytest.mark.django_db


@override_settings(BRANCH_ID="branch-a")
def test_noncash_ledger_events_never_move_physical_cash():
    own = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("100.00")
    )
    other = CashRegister.objects.create(
        branch_id="branch-b", current_balance=Decimal("900.00")
    )

    assert CashTransactionService.deposit(
        25, payment_method="UZCARD"
    )[1] == 201
    assert CashTransactionService.withdraw(
        20, payment_method="PAYME"
    )[1] == 201
    assert CashTransactionService.create_for_reference(
        type=CashTransaction.TransactionType.EXPENSE_PAYMENT,
        amount=10,
        payment_method="BANK_TRANSFER",
        reference_type="Expense",
        reference_id=7,
    )[1] == 201

    own.refresh_from_db()
    other.refresh_from_db()
    assert own.current_balance == Decimal("100.00")
    assert other.current_balance == Decimal("900.00")
    rows = list(CashTransaction.objects.order_by("created_at", "pk"))
    assert len(rows) == 3
    assert all(row.branch_id == "branch-a" for row in rows)
    assert all(row.balance_before == Decimal("100.00") for row in rows)
    assert all(row.balance_after == Decimal("100.00") for row in rows)


@override_settings(BRANCH_ID="branch-a")
def test_cash_events_lock_and_move_only_the_configured_branch():
    own = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("100.00")
    )
    other = CashRegister.objects.create(
        branch_id="branch-b", current_balance=Decimal("900.00")
    )

    # Normalization must not provide a way to bypass the CASH-only mutation.
    assert CashTransactionService.deposit(25, payment_method=" cash ")[1] == 201
    assert CashTransactionService.withdraw(40, payment_method="CASH")[1] == 201

    own.refresh_from_db()
    other.refresh_from_db()
    assert own.current_balance == Decimal("85.00")
    assert other.current_balance == Decimal("900.00")


@override_settings(BRANCH_ID="branch-a")
def test_cash_withdrawal_cannot_make_the_locked_drawer_negative():
    register = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("30.00")
    )

    body, status = CashTransactionService.withdraw(
        Decimal("30.01"), payment_method="CASH"
    )

    assert status == 400
    assert "Insufficient cash" in body["message"]
    register.refresh_from_db()
    assert register.current_balance == Decimal("30.00")
    assert not CashTransaction.objects.exists()


@override_settings(BRANCH_ID="branch-a")
def test_unknown_method_is_rejected_before_a_register_or_ledger_row_is_created():
    body, status = CashTransactionService.withdraw(1, payment_method="CRYPTO")

    assert status == 422
    assert "payment_method" in body["errors"]
    assert not CashRegister.objects.exists()
    assert not CashTransaction.objects.exists()


@override_settings(BRANCH_ID="branch-a")
@pytest.mark.parametrize("bad_amount", ["not-a-number", "NaN", "Infinity", "1.001"])
def test_invalid_money_is_rejected_without_side_effects(bad_amount):
    body, status = CashTransactionService.deposit(bad_amount)

    assert status == 422
    assert "amount" in body["errors"]
    assert not CashRegister.objects.exists()
    assert not CashTransaction.objects.exists()


@override_settings(BRANCH_ID="branch-a")
def test_unknown_reference_transaction_type_is_rejected():
    body, status = CashTransactionService.create_for_reference(
        type="MADE_UP", amount=1,
    )

    assert status == 422
    assert "type" in body["errors"]
    assert not CashRegister.objects.exists()
    assert not CashTransaction.objects.exists()


@override_settings(BRANCH_ID="branch-a")
def test_drawer_mutation_rolls_back_if_ledger_write_fails(monkeypatch):
    register = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("50.00")
    )

    def fail_create(_cls, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(
        CashTransactionRepository, "create", classmethod(fail_create)
    )
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        CashTransactionService.withdraw(10, payment_method="CASH")

    register.refresh_from_db()
    assert register.current_balance == Decimal("50.00")
    assert not CashTransaction.objects.exists()
