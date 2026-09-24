from decimal import Decimal

import pytest

from hr.models import SalaryPayment
from hr.services.salary_item_service import SalaryItemService
from hr.services.salary_service import SalaryService
from .test_salary import _employee, _salary

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '-Infinity', 'bad', True, '10000000000'])
def test_salary_create_rejects_invalid_money_without_writes(amount):
    employee = _employee()
    result, status = SalaryService.create(employee.pk, 2026, 9, base_amount=amount)
    assert status == 422, result
    assert not SalaryPayment.objects.exists()


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', 'bad', '10000000000'])
def test_salary_update_rejects_invalid_money_without_writes(amount):
    salary = _salary(_employee(), 2026, 9, Decimal('100'))
    result, status = SalaryService.update(salary.pk, bonus=amount)
    assert status == 422, result
    salary.refresh_from_db()
    assert salary.bonus == 0
    assert salary.net_amount == 100


@pytest.mark.parametrize('operation', ['add_bonus', 'add_deduction', 'set_base'])
@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '10000000000'])
def test_salary_item_rejects_invalid_money(operation, amount):
    salary = _salary(_employee(), 2026, 9, Decimal('100'))
    result, status = getattr(SalaryItemService, operation)(salary.pk, amount)
    assert status == 422, result
    salary.refresh_from_db()
    assert salary.net_amount == 100
    assert not salary.bonuses.exists()
    assert not salary.deductions.exists()


def test_salary_create_rejects_overflowing_net_total():
    result, status = SalaryService.create(_employee().pk, 2026, 9,
                                          base_amount='9999999999.99', bonus=1)
    assert status == 422, result
    assert not SalaryPayment.objects.exists()


def test_salary_bonus_rejects_overflowing_aggregate_without_child_write():
    salary = _salary(_employee(), 2026, 9, Decimal('100'))
    assert SalaryItemService.add_bonus(salary.pk, '6000000000')[1] == 201
    result, status = SalaryItemService.add_bonus(salary.pk, '6000000000')
    assert status == 422, result
    salary.refresh_from_db()
    assert salary.bonus == Decimal('6000000000')
    assert salary.bonuses.count() == 1
