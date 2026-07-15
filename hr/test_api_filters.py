import secrets
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.models import Session, User
from base.repositories import SessionRepository
from hr.models import (
    Attendance,
    CashTransaction,
    Department,
    Employee,
    Expense,
    ExpenseCategory,
    LeaveType,
    PerformanceReview,
    SalaryPayment,
)


pytestmark = pytest.mark.django_db


def _authenticated_client(user):
    token = secrets.token_hex(32)
    user_agent = "hr-filter-contract-tests"
    Session.objects.create(
        user_id=user,
        ip_address="127.0.0.1",
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT=user_agent)
    client.cookies["session_key"] = token
    return client


@pytest.fixture
def admin_client(admin_user):
    return _authenticated_client(admin_user)


def _employee(first_name, suffix, department, **kwargs):
    user = User.objects.create(
        first_name=first_name,
        last_name=suffix,
        email=f"{first_name.lower()}.{suffix.lower()}@hr-filter.test",
        password="not-used",
        role=User.RoleChoices.USER,
        status=User.UserStatus.ACTIVE,
    )
    return Employee.objects.create(
        user=user,
        department=department,
        position=kwargs.pop("position", "Cook"),
        hire_date=date(2026, 1, 1),
        **kwargs,
    )


def _pagination(response):
    assert response.status_code == 200, response.content
    return response.json()["data"]["pagination"]


def test_employee_filters_apply_before_pagination(admin_client):
    kitchen = Department.objects.create(name="Employee Kitchen")
    office = Department.objects.create(name="Employee Office")
    _employee("Alice", "One", kitchen, contract_type="PART_TIME")
    _employee("Alina", "Two", kitchen, contract_type="PART_TIME")
    _employee("Alice", "Inactive", kitchen, contract_type="PART_TIME", is_active=False)
    _employee("Alice", "Office", office, contract_type="PART_TIME")

    response = admin_client.get("/api/admins/hr/employees/", {
        "search": "ali",
        "department": kitchen.id,
        "type": "part_time",
        "status": "active",
        "per_page": 1,
    })

    payload = response.json()["data"]
    assert _pagination(response)["total"] == 2
    assert len(payload["employees"]) == 1
    assert payload["employees"][0]["department"]["id"] == kitchen.id


def test_department_filters_apply_before_pagination(admin_client):
    Department.objects.create(name="Kitchen East")
    Department.objects.create(name="Kitchen West")
    Department.objects.create(name="Kitchen Closed", is_active=False)
    Department.objects.create(name="Office")

    response = admin_client.get("/api/admins/hr/departments/", {
        "search": "kitchen", "status": "active", "per_page": 1,
    })

    assert _pagination(response)["total"] == 2
    assert len(response.json()["data"]["departments"]) == 1


def test_salary_filters_and_employee_alias(admin_client):
    department = Department.objects.create(name="Salary Department")
    first = _employee("Payroll", "One", department)
    second = _employee("Payroll", "Two", department)
    third = _employee("Other", "Three", department)
    for employee, status in ((first, "PENDING"), (second, "PENDING"), (third, "PAID")):
        SalaryPayment.objects.create(
            employee=employee,
            period_year=2026,
            period_month=7,
            base_amount=Decimal("100"),
            net_amount=Decimal("100"),
            status=status,
        )

    response = admin_client.get("/api/admins/hr/salaries/", {
        "search": "payroll", "status": "pending", "year": 2026,
        "month": 7, "per_page": 1,
    })
    assert _pagination(response)["total"] == 2

    employee_response = admin_client.get(
        "/api/admins/hr/salaries/", {"employee": second.id},
    )
    salaries = employee_response.json()["data"]["salaries"]
    assert [row["employee_id"] for row in salaries] == [second.id]


def test_expense_filters_apply_before_pagination(admin_client, admin_user):
    category = ExpenseCategory.objects.create(name="Fuel")
    other_category = ExpenseCategory.objects.create(name="Rent")
    for description in ("Fuel delivery one", "Fuel delivery two"):
        Expense.objects.create(
            category=category,
            amount=Decimal("10"),
            description=description,
            expense_date=date(2026, 7, 10),
            status="PENDING",
            created_by=admin_user,
        )
    Expense.objects.create(
        category=other_category,
        amount=Decimal("10"),
        description="Fuel but wrong category",
        expense_date=date(2026, 7, 10),
        status="PENDING",
        created_by=admin_user,
    )

    response = admin_client.get("/api/admins/hr/expenses/", {
        "search": "fuel delivery", "status": "pending",
        "type": category.id, "date": "2026-07-10", "per_page": 1,
    })

    assert _pagination(response)["total"] == 2
    assert len(response.json()["data"]["expenses"]) == 1


def test_cash_filters_apply_before_pagination(admin_client, admin_user):
    for description in ("Till correction one", "Till correction two"):
        CashTransaction.objects.create(
            type="WITHDRAWAL",
            amount=Decimal("5"),
            description=description,
            balance_before=Decimal("100"),
            balance_after=Decimal("95"),
            performed_by=admin_user,
        )
    CashTransaction.objects.create(
        type="DEPOSIT",
        amount=Decimal("5"),
        description="Till correction deposit",
        balance_before=Decimal("95"),
        balance_after=Decimal("100"),
        performed_by=admin_user,
    )

    response = admin_client.get("/api/admins/hr/cash/", {
        "search": "till correction", "type": "withdrawal",
        "date": timezone.localdate().isoformat(), "per_page": 1,
    })

    assert _pagination(response)["total"] == 2
    assert len(response.json()["data"]["transactions"]) == 1


@pytest.mark.parametrize(
    ("url", "model", "name_field", "result_key"),
    [
        ("/api/admins/hr/expense-categories/", ExpenseCategory, "Food", "categories"),
        ("/api/admins/hr/leave-types/", LeaveType, "Food", "leave_types"),
    ],
)
def test_catalog_filters_apply_before_pagination(
    admin_client, url, model, name_field, result_key,
):
    model.objects.create(name=f"{name_field} Alpha")
    model.objects.create(name=f"{name_field} Beta")
    model.objects.create(name=f"{name_field} Inactive", is_active=False)
    model.objects.create(name="Unrelated")

    response = admin_client.get(url, {
        "search": name_field.lower(), "status": "active", "per_page": 1,
    })

    assert _pagination(response)["total"] == 2
    assert len(response.json()["data"][result_key]) == 1


def test_review_filters_apply_before_pagination(admin_client, admin_user):
    department = Department.objects.create(name="Review Department")
    employee = _employee("Review", "Employee", department)
    for strengths in ("Great service", "Great teamwork"):
        PerformanceReview.objects.create(
            employee=employee,
            reviewer=admin_user,
            review_period_start=date(2026, 7, 1),
            review_period_end=date(2026, 7, 31),
            strengths=strengths,
            status="SUBMITTED",
        )
    PerformanceReview.objects.create(
        employee=employee,
        reviewer=admin_user,
        review_period_start=date(2026, 7, 1),
        review_period_end=date(2026, 7, 31),
        strengths="Great but draft",
        status="DRAFT",
    )

    response = admin_client.get("/api/admins/hr/reviews/", {
        "employee": employee.id, "search": "great", "status": "submitted",
        "date": "2026-07-10", "per_page": 1,
    })

    assert _pagination(response)["total"] == 2
    assert len(response.json()["data"]["reviews"]) == 1


def test_attendance_daily_report_parses_iso_date_without_500(admin_client):
    department = Department.objects.create(name="Attendance Department")
    employee = _employee("Attendance", "Employee", department)
    Attendance.objects.create(
        employee=employee,
        date=date(2026, 7, 10),
        status=Attendance.Status.PRESENT,
    )

    response = admin_client.get(
        "/api/admins/hr/attendance/daily-report/?date=2026-07-10"
    )
    assert response.status_code == 200, response.content
    assert response.json()["data"]["date"] == "2026-07-10"
    assert response.json()["data"]["stats"]["present"] == 1

    invalid = admin_client.get(
        "/api/admins/hr/attendance/daily-report/?date=not-a-date"
    )
    assert invalid.status_code == 422
    assert "date" in invalid.json()["errors"]
