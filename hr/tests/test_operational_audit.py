from datetime import timedelta

import pytest
from django.utils import timezone

from base.models import Category, Order, OrderItem, Product
from hr.models import (
    DisciplinaryCase, DisciplinaryRule, Employee, PreparationAudit,
    PreparationAuditCategory, SalaryDeduction, SalaryPayment,
)
from hr.services.operational_audit_service import (
    AttendanceOperations, DisciplineService, PreparationAuditService,
    WorkScheduleService, ensure_preparation_audit,
)


pytestmark = pytest.mark.django_db


def _employee(user, position='Operator'):
    return Employee.objects.create(
        user=user, position=position, hire_date=timezone.localdate(),
        base_salary=1_000_000, branch_id='branch1',
    )


def test_manual_attendance_uses_tashkent_schedule_snapshot(admin_user, cashier_user):
    employee = _employee(cashier_user)
    work_date = timezone.localdate() - timedelta(days=1)
    result, status = WorkScheduleService.create({
        'employee_id': employee.id, 'weekday': work_date.weekday(),
        'scheduled_start_local': '09:00', 'scheduled_end_local': '18:00',
        'grace_minutes': 5, 'effective_from': work_date.isoformat(),
    }, admin_user)
    assert status == 201, result

    result, status = AttendanceOperations.manual_entry({
        'employee_id': employee.id, 'work_date': work_date.isoformat(),
        'check_in_local': f'{work_date.isoformat()}T09:12:00+05:00',
        'check_out_local': f'{work_date.isoformat()}T18:03:00+05:00',
    }, admin_user)
    assert status == 201, result
    row = result['data']['attendance']
    assert row['scheduled_minutes'] == 540
    assert row['worked_minutes'] == 531
    assert row['late_minutes'] == 7
    assert row['early_leave_minutes'] == 0
    assert row['scheduled_start'].endswith('+05:00')
    assert row['timezone'] == 'Asia/Tashkent'


def test_adjustment_requires_a_different_approver(admin_user, cashier_user, other_user):
    employee = _employee(cashier_user)
    day = timezone.localdate() - timedelta(days=1)
    created, _ = AttendanceOperations.manual_entry({
        'employee_id': employee.id, 'work_date': day.isoformat(),
        'check_in_local': f'{day.isoformat()}T09:15:00+05:00',
    }, admin_user)
    attendance_id = created['data']['attendance']['id']
    requested, status = AttendanceOperations.request_adjustment(attendance_id, {
        'requested_check_in': f'{day.isoformat()}T09:00:00+05:00',
        'reason_category': 'DATA_ENTRY_ERROR',
    }, admin_user)
    assert status == 201
    request_id = requested['data']['adjustment_request']['id']
    denied, status = AttendanceOperations.review_adjustment(
        request_id, admin_user, True, 'Verified',
    )
    assert status == 403
    assert denied['errors']['code'] == 'self_approval_forbidden'

    approved, status = AttendanceOperations.review_adjustment(
        request_id, other_user, True, 'Verified against source',
    )
    assert status == 200, approved
    assert approved['data']['adjustment_request']['status'] == 'APPROVED'


def test_disciplinary_approval_links_one_deduction_once(admin_user, cashier_user, other_user):
    employee = _employee(cashier_user)
    now = timezone.localtime()
    rule = DisciplinaryRule.objects.create(
        code='ATT-LATE-01', category='ATTENDANCE', title='Late arrival',
        description='Approved late-arrival policy', default_amount_uzs=100_000,
        effective_from=now.date(), created_by=admin_user, updated_by=admin_user,
    )
    salary = SalaryPayment.objects.create(
        employee=employee, period_year=now.year, period_month=now.month,
        base_amount=1_000_000, net_amount=1_000_000, created_by=admin_user,
        branch_id='branch1',
    )
    created, status = DisciplineService.create_case({
        'employee_id': employee.id, 'rule_id': rule.id,
        'occurred_at': now.isoformat(), 'business_date': now.date().isoformat(),
        'evidence': 'Entry log shows arrival after the scheduled time.',
        'comment': 'Reviewed against the daily attendance record.',
        'status': 'SUBMITTED',
    }, admin_user)
    assert status == 201, created
    case_id = created['data']['discipline_case']['id']
    approved, status = DisciplineService.approve_case(case_id, other_user, 'Approved')
    assert status == 200, approved
    replayed, status = DisciplineService.approve_case(case_id, other_user, 'Approved')
    assert status == 200, replayed
    assert SalaryDeduction.objects.filter(salary=salary, is_deleted=False).count() == 1
    case = DisciplinaryCase.objects.get(id=case_id)
    assert case.rule_code_snapshot == 'ATT-LATE-01'
    assert case.amount_uzs == 100_000
    assert case.salary_deduction_id is not None


def test_preparation_snapshot_and_review_are_idempotent(settings, admin_user, cashier_user):
    settings.EDITION = 'server'
    category = Category.objects.create(name='Kitchen')
    product = Product.objects.create(name='Non burger standart', price=10_000, category=category)
    order = Order.objects.create(
        user=cashier_user, cashier=cashier_user, status=Order.Status.READY,
        subtotal=10_000, total_amount=10_000, branch_id='branch1',
    )
    OrderItem.objects.create(order=order, product=product, quantity=1, price=10_000,
                             branch_id='branch1')
    ready = timezone.now()
    Order.objects.filter(pk=order.pk).update(
        created_at=ready - timedelta(minutes=8), ready_at=ready,
    )
    first = ensure_preparation_audit(order.id)
    second = ensure_preparation_audit(order.id)
    assert first.id == second.id
    assert PreparationAudit.objects.filter(order=order).count() == 1
    assert first.performance_status == 'SLIGHTLY_LATE'
    assert first.review_status == 'PENDING'

    reason = PreparationAuditCategory.objects.get(code='HIGH_ORDER_VOLUME')
    reviewed, status = PreparationAuditService.review(first.id, {
        'category_id': reason.id,
        'comment': 'A verified order surge exceeded normal kitchen capacity.',
    }, admin_user)
    assert status == 201, reviewed
    replayed, status = PreparationAuditService.review(first.id, {
        'category_id': reason.id,
        'comment': 'A changed comment cannot create a duplicate review.',
    }, admin_user)
    assert status == 200, replayed
    first.refresh_from_db()
    assert first.review_status == 'COMPLETED'
    assert first.reviews.filter(is_current=True).count() == 1
