"""Tests for salary itemization (P7): editable monthly base + bonus/penalty."""
from datetime import date
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


def _employee(base_salary=Decimal('1000000'), email='emp@t.local'):
    from base.models import User
    from hr.models import Department, Employee
    u = User.objects.create(first_name='Emp', last_name='One', email=email,
                            password='x', role='USER', status='ACTIVE')
    d = Department.objects.create(name='Kitchen')
    return Employee.objects.create(
        user=u, department=d, position='Cook', hire_date=date(2025, 1, 1),
        base_salary=base_salary)


def _salary(employee, year, month, base):
    from hr.models import SalaryPayment
    return SalaryPayment.objects.create(
        employee=employee, period_year=year, period_month=month,
        base_amount=base, net_amount=base)


class TestSalaryItemization:
    def test_net_recompute_from_items(self):
        from hr.services.salary_item_service import SalaryItemService
        e = _employee()
        s = _salary(e, 2026, 5, Decimal('1000000'))
        SalaryItemService.add_bonus(s.id, Decimal('200000'), 'good month')
        SalaryItemService.add_deduction(s.id, Decimal('50000'), 'late')
        s.refresh_from_db()
        assert s.bonus == Decimal('200000.00')
        assert s.deduction == Decimal('50000.00')
        assert s.net_amount == Decimal('1150000.00')

    def test_set_base_recomputes_net(self):
        from hr.services.salary_item_service import SalaryItemService
        e = _employee()
        s = _salary(e, 2026, 5, Decimal('1000000'))
        SalaryItemService.add_bonus(s.id, Decimal('100000'))
        SalaryItemService.set_base(s.id, Decimal('1200000'))
        s.refresh_from_db()
        assert s.net_amount == Decimal('1300000.00')

    def test_remove_bonus_recomputes(self):
        from hr.services.salary_item_service import SalaryItemService
        e = _employee()
        s = _salary(e, 2026, 5, Decimal('1000000'))
        SalaryItemService.add_bonus(s.id, Decimal('100000'))
        items, _ = SalaryItemService.items(s.id)
        bonus_id = items['data']['bonuses'][0]['id']
        SalaryItemService.remove_bonus(s.id, bonus_id)
        s.refresh_from_db()
        assert s.net_amount == Decimal('1000000.00')

    def test_generate_payroll_seeds_base_from_prev_month(self):
        from hr.models import SalaryPayment
        from hr.services.salary_service import SalaryService
        e = _employee(Decimal('1000000'))
        # April's base was edited up to 1.5M.
        _salary(e, 2026, 4, Decimal('1500000'))
        result, status = SalaryService.generate_payroll(2026, 5)
        assert status == 200, result
        may = SalaryPayment.objects.get(employee=e, period_year=2026, period_month=5)
        # Seeded from April's base, NOT the employee's standing base_salary.
        assert may.base_amount == Decimal('1500000.00')

    def test_cannot_edit_paid_salary(self):
        from hr.services.salary_item_service import SalaryItemService
        e = _employee()
        s = _salary(e, 2026, 5, Decimal('1000000'))
        s.status = 'PAID'
        s.save(update_fields=['status'])
        result, status = SalaryItemService.add_bonus(s.id, Decimal('100000'))
        assert status >= 400
