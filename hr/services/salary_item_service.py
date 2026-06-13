"""Itemized salary bonuses/penalties + editable monthly base.

net = base_amount + Σ bonuses − Σ deductions, recomputed whenever the base or any
child row changes. The scalar SalaryPayment.bonus/deduction are kept as the sums
for back-compat with existing serializers/reports.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum

from base.helpers.response import ServiceResponse
from hr.models import SalaryPayment, SalaryBonus, SalaryDeduction


def _to_dec(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


class SalaryItemService:

    @staticmethod
    def _recompute(salary):
        b = salary.bonuses.filter(is_deleted=False).aggregate(s=Sum('amount'))['s'] or Decimal('0')
        d = salary.deductions.filter(is_deleted=False).aggregate(s=Sum('amount'))['s'] or Decimal('0')
        salary.bonus = b
        salary.deduction = d
        salary.net_amount = (salary.base_amount or Decimal('0')) + b - d
        salary.save(update_fields=['bonus', 'deduction', 'net_amount',
                                   'synced_at', 'sync_version'])
        return salary

    @classmethod
    def _locked(cls, salary_id):
        return (SalaryPayment.objects.select_for_update()
                .filter(id=salary_id, is_deleted=False).first())

    @classmethod
    @transaction.atomic
    def add_bonus(cls, salary_id, amount, reason=''):
        salary = cls._locked(salary_id)
        if not salary:
            return ServiceResponse.not_found('Salary not found')
        if salary.status == SalaryPayment.Status.PAID:
            return ServiceResponse.error('Cannot edit a paid salary')
        amt = _to_dec(amount)
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(errors={'amount': 'Must be > 0'})
        SalaryBonus.objects.create(salary=salary, amount=amt, reason=reason or '')
        cls._recompute(salary)
        return ServiceResponse.created(data={
            'net_amount': str(salary.net_amount), 'bonus_total': str(salary.bonus)})

    @classmethod
    @transaction.atomic
    def add_deduction(cls, salary_id, amount, reason=''):
        salary = cls._locked(salary_id)
        if not salary:
            return ServiceResponse.not_found('Salary not found')
        if salary.status == SalaryPayment.Status.PAID:
            return ServiceResponse.error('Cannot edit a paid salary')
        amt = _to_dec(amount)
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(errors={'amount': 'Must be > 0'})
        SalaryDeduction.objects.create(salary=salary, amount=amt, reason=reason or '')
        cls._recompute(salary)
        return ServiceResponse.created(data={
            'net_amount': str(salary.net_amount), 'deduction_total': str(salary.deduction)})

    @classmethod
    @transaction.atomic
    def set_base(cls, salary_id, amount):
        salary = cls._locked(salary_id)
        if not salary:
            return ServiceResponse.not_found('Salary not found')
        if salary.status == SalaryPayment.Status.PAID:
            return ServiceResponse.error('Cannot edit a paid salary')
        amt = _to_dec(amount)
        if amt is None or amt < 0:
            return ServiceResponse.validation_error(errors={'amount': 'Must be >= 0'})
        salary.base_amount = amt
        salary.save(update_fields=['base_amount', 'synced_at', 'sync_version'])
        cls._recompute(salary)
        return ServiceResponse.success(data={'net_amount': str(salary.net_amount)})

    @classmethod
    @transaction.atomic
    def remove_bonus(cls, salary_id, bonus_id):
        salary = cls._locked(salary_id)
        if not salary:
            return ServiceResponse.not_found('Salary not found')
        if salary.status == SalaryPayment.Status.PAID:
            return ServiceResponse.error('Cannot edit a paid salary')
        row = SalaryBonus.objects.filter(id=bonus_id, salary=salary, is_deleted=False).first()
        if row:
            row.delete()  # SyncMixin soft-delete + tombstone
        cls._recompute(salary)
        return ServiceResponse.success(data={'net_amount': str(salary.net_amount)})

    @classmethod
    @transaction.atomic
    def remove_deduction(cls, salary_id, deduction_id):
        salary = cls._locked(salary_id)
        if not salary:
            return ServiceResponse.not_found('Salary not found')
        if salary.status == SalaryPayment.Status.PAID:
            return ServiceResponse.error('Cannot edit a paid salary')
        row = SalaryDeduction.objects.filter(id=deduction_id, salary=salary, is_deleted=False).first()
        if row:
            row.delete()
        cls._recompute(salary)
        return ServiceResponse.success(data={'net_amount': str(salary.net_amount)})

    @staticmethod
    def items(salary_id):
        bonuses = SalaryBonus.objects.filter(salary_id=salary_id, is_deleted=False)
        deductions = SalaryDeduction.objects.filter(salary_id=salary_id, is_deleted=False)
        return ServiceResponse.success(data={
            'bonuses': [{'id': b.id, 'amount': str(b.amount), 'reason': b.reason}
                        for b in bonuses],
            'deductions': [{'id': d.id, 'amount': str(d.amount), 'reason': d.reason}
                           for d in deductions],
        })
