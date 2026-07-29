import json
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client, override_settings

from base.models import CashRegister, Session
from base.repositories import SessionRepository
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


@override_settings(BRANCH_ID="branch-a")
@pytest.mark.parametrize("shift_status", ["ACTIVE", "ENDED"])
def test_direct_cash_movement_is_blocked_while_shift_cash_is_unresolved(
    shift_status,
):
    from django.utils import timezone
    from base.models import Shift, User

    register = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("100.00")
    )
    cashier = User.objects.create(
        email=f"unresolved-{shift_status.lower()}@test.local",
        first_name="Open",
        last_name="Drawer",
        role="CASHIER",
        status="ACTIVE",
        branch_id="branch-a",
        password="!",
    )
    shift = Shift.objects.create(
        user=cashier,
        branch_id="branch-a",
        status=shift_status,
        start_time=timezone.now(),
        end_time=timezone.now() if shift_status == "ENDED" else None,
    )

    for operation in (
        lambda: CashTransactionService.deposit(10, payment_method="CASH"),
        lambda: CashTransactionService.withdraw(10, payment_method="CASH"),
        lambda: CashTransactionService.create_for_reference(
            type=CashTransaction.TransactionType.EXPENSE_PAYMENT,
            amount=10,
            payment_method="CASH",
            reference_type="Expense",
            reference_id=shift.id,
        ),
    ):
        body, status = operation()
        assert status == 422, body
        assert "cash_drawer" in body["errors"]

    register.refresh_from_db()
    assert register.current_balance == Decimal("100.00")
    assert not CashTransaction.objects.exists()


@override_settings(BRANCH_ID="branch-a")
def test_non_cash_hr_ledger_remains_available_during_an_active_shift():
    from django.utils import timezone
    from base.models import Shift, User

    register = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("100.00")
    )
    cashier = User.objects.create(
        email="noncash-open-drawer@test.local",
        first_name="Open",
        last_name="Drawer",
        role="CASHIER",
        status="ACTIVE",
        branch_id="branch-a",
        password="!",
    )
    Shift.objects.create(
        user=cashier,
        branch_id="branch-a",
        status="ACTIVE",
        start_time=timezone.now(),
    )

    body, status = CashTransactionService.withdraw(
        10, payment_method="PAYME",
    )

    assert status == 201, body
    register.refresh_from_db()
    assert register.current_balance == Decimal("100.00")
    assert CashTransaction.objects.get().payment_method == "PAYME"


@override_settings(BRANCH_ID="branch-a")
@pytest.mark.parametrize("payment_kind", ["salary", "expense"])
def test_hr_cash_payment_endpoints_explain_open_shift_block(payment_kind):
    """The workflow-changing guard must be visible at the real HTTP boundary."""
    from django.utils import timezone

    from base.models import Shift, User
    from hr.models import Department, Employee, Expense, SalaryPayment

    register = CashRegister.objects.create(
        branch_id="branch-a", current_balance=Decimal("100.00"),
    )
    admin = User.objects.create(
        email=f"admin-{payment_kind}@test.local",
        first_name="Admin",
        last_name="Operator",
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
        branch_id="branch-a",
        password="!",
    )
    cashier = User.objects.create(
        email=f"cashier-{payment_kind}@test.local",
        first_name="Open",
        last_name="Drawer",
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        branch_id="branch-a",
        password="!",
    )
    Shift.objects.create(
        user=cashier,
        branch_id="branch-a",
        status=Shift.Status.ACTIVE,
        start_time=timezone.now(),
    )

    if payment_kind == "salary":
        employee_user = User.objects.create(
            email="employee-salary@test.local",
            first_name="Salary",
            last_name="Employee",
            role=User.RoleChoices.USER,
            status=User.UserStatus.ACTIVE,
            branch_id="branch-a",
            password="!",
        )
        employee = Employee.objects.create(
            user=employee_user,
            department=Department.objects.create(name="Kitchen"),
            position="Cook",
            hire_date=timezone.localdate(),
        )
        payable = SalaryPayment.objects.create(
            employee=employee,
            period_year=2026,
            period_month=7,
            base_amount=Decimal("25.00"),
            net_amount=Decimal("25.00"),
            status=SalaryPayment.Status.APPROVED,
            approved_by=admin,
            created_by=admin,
        )
        path = f"/api/admins/hr/salaries/{payable.id}/pay/"
    else:
        payable = Expense.objects.create(
            amount=Decimal("25.00"),
            description="Approved HR expense",
            expense_date=timezone.localdate(),
            status=Expense.Status.APPROVED,
            created_by=admin,
            approved_by=admin,
        )
        path = f"/api/admins/hr/expenses/{payable.id}/pay/"

    token = secrets.token_hex(32)
    user_agent = f"hr-open-shift-{payment_kind}"
    Session.objects.create(
        user_id=admin,
        ip_address="127.0.0.1",
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT=user_agent)
    client.cookies["session_key"] = token

    response = client.post(
        path,
        data=json.dumps({"payment_method": "CASH"}),
        content_type="application/json",
    )

    assert response.status_code == 422, response.content
    payload = response.json()
    assert "cash_drawer" in payload["errors"]
    assert "cashbox expense flow" in payload["errors"]["cash_drawer"]
    payable.refresh_from_db()
    assert payable.status == payable.Status.APPROVED
    register.refresh_from_db()
    assert register.current_balance == Decimal("100.00")
    assert not CashTransaction.objects.exists()
