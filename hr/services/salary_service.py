from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from hr.models import SalaryPayment
from hr.repositories import SalaryPaymentRepository, EmployeeRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class SalaryService:

    @classmethod
    def serialize(cls, salary: SalaryPayment) -> Dict[str, Any]:
        data = {
            "id": salary.id,
            "uuid": str(salary.uuid),
            "employee_id": salary.employee_id,
            "employee": None,
            "period_year": salary.period_year,
            "period_month": salary.period_month,
            "base_amount": str(salary.base_amount),
            "bonus": str(salary.bonus),
            "deduction": str(salary.deduction),
            "net_amount": str(salary.net_amount),
            "status": salary.status,
            "status_display": salary.get_status_display(),
            "payment_method": salary.payment_method,
            "payment_method_display": salary.get_payment_method_display(),
            "paid_at": salary.paid_at.isoformat() if salary.paid_at else None,
            "approved_by_id": salary.approved_by_id,
            "approved_by": None,
            "created_by_id": salary.created_by_id,
            "notes": salary.notes,
            "created_at": salary.created_at.isoformat(),
        }

        if salary.employee_id and salary.employee:
            emp = salary.employee
            data["employee"] = {
                "id": emp.id,
                "position": emp.position,
            }
            if emp.user:
                data["employee"]["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }

        if salary.approved_by_id and salary.approved_by:
            data["approved_by"] = {
                "id": salary.approved_by.id,
                "first_name": salary.approved_by.first_name,
                "last_name": salary.approved_by.last_name,
            }

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             employee_id: int = None,
             year: int = None,
             month: int = None,
             status: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = SalaryPayment.objects.filter(
            is_deleted=False
        ).select_related("employee__user", "approved_by", "created_by")

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if year:
            queryset = queryset.filter(period_year=year)

        if month:
            queryset = queryset.filter(period_month=month)

        if status:
            queryset = queryset.filter(status=status)

        queryset = queryset.order_by("-period_year", "-period_month", "employee__user__first_name")

        page_obj, paginator = SalaryPaymentRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "salaries": [cls.serialize(sal) for sal in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in SalaryPayment.Status.choices
            ],
        })

    @classmethod
    def get(cls, salary_id: int) -> Tuple[Dict[str, Any], int]:
        salary = SalaryPaymentRepository.get_with_relations(salary_id)
        if not salary:
            return ServiceResponse.not_found(
                f"Salary payment with id {salary_id} not found"
            )

        return ServiceResponse.success(data={
            "salary": cls.serialize(salary),
        })

    @staticmethod
    def _validate_period(period_year: int, period_month: int):
        # Reject out-of-range months/years. Without this, period_month 0 or 13
        # creates payable rows that the exists_for_period guard treats as
        # distinct from the valid 1..12 months, allowing duplicate payroll.
        errors = {}
        try:
            month = int(period_month)
        except (TypeError, ValueError):
            month = None
        try:
            yr = int(period_year)
        except (TypeError, ValueError):
            yr = None

        if month is None or not (1 <= month <= 12):
            errors["period_month"] = "must be between 1 and 12"
        if yr is None or not (2000 <= yr <= 2100):
            errors["period_year"] = "must be between 2000 and 2100"

        if errors:
            return ServiceResponse.validation_error(errors=errors)
        return None

    @classmethod
    @transaction.atomic
    def create(cls,
               employee_id: int,
               period_year: int,
               period_month: int,
               base_amount: Decimal = None,
               bonus: Decimal = Decimal("0"),
               deduction: Decimal = Decimal("0"),
               payment_method: str = "CASH",
               created_by_id: int = None,
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        period_error = cls._validate_period(period_year, period_month)
        if period_error:
            return period_error

        if SalaryPaymentRepository.exists_for_period(employee_id, period_year, period_month):
            return ServiceResponse.validation_error(
                errors={"period": f"Salary record already exists for {period_year}/{period_month}"},
            )

        if base_amount is None:
            base_amount = employee.base_salary
        else:
            base_amount = Decimal(str(base_amount))

        bonus = Decimal(str(bonus))
        deduction = Decimal(str(deduction))
        # All three components must be non-negative. A negative `deduction`
        # would inflate `net_amount = base + bonus - deduction` and let an
        # admin (or anyone with salary.create) drain the cash drawer at
        # pay-time. Same logic for negative bonus or base.
        if base_amount < 0 or bonus < 0 or deduction < 0:
            return ServiceResponse.validation_error(
                errors={
                    'base_amount': 'must be non-negative' if base_amount < 0 else None,
                    'bonus': 'must be non-negative' if bonus < 0 else None,
                    'deduction': 'must be non-negative' if deduction < 0 else None,
                },
            )
        net_amount = base_amount + bonus - deduction

        salary = SalaryPaymentRepository.create(
            employee_id=employee_id,
            period_year=period_year,
            period_month=period_month,
            base_amount=base_amount,
            bonus=bonus,
            deduction=deduction,
            net_amount=net_amount,
            status=SalaryPayment.Status.PENDING,
            payment_method=payment_method,
            created_by_id=created_by_id,
            notes=notes,
        )

        salary = SalaryPaymentRepository.get_with_relations(salary.pk)

        return ServiceResponse.created(data={
            "id": salary.id,
            "uuid": str(salary.uuid),
            "salary": cls.serialize(salary),
        }, message="Salary payment created")

    @classmethod
    @transaction.atomic
    def generate_payroll(cls,
                         year: int,
                         month: int,
                         created_by_id: int = None) -> Tuple[Dict[str, Any], int]:
        period_error = cls._validate_period(year, month)
        if period_error:
            return period_error

        active_employees = EmployeeRepository.get_active()
        created = 0
        skipped = 0

        # Previous month, for seeding each row's editable base from last month.
        prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)

        for employee in active_employees:
            if SalaryPaymentRepository.exists_for_period(employee.id, year, month):
                skipped += 1
                continue

            # The month's base pre-fills from last month's (edited) base_amount,
            # falling back to the employee's standing base_salary. Editing a
            # month's base never mutates employee.base_salary — it's a snapshot.
            prev = SalaryPayment.objects.filter(
                employee_id=employee.id, period_year=prev_year,
                period_month=prev_month, is_deleted=False,
            ).first()
            base = prev.base_amount if prev else employee.base_salary
            net_amount = base

            # Savepoint + catch so a concurrent generate_payroll that inserted
            # this (employee, year, month) row between exists_for_period() and
            # this create doesn't IntegrityError out and roll back the whole
            # payroll run — just count it as skipped.
            try:
                with transaction.atomic():
                    SalaryPaymentRepository.create(
                        employee_id=employee.id,
                        period_year=year,
                        period_month=month,
                        base_amount=base,
                        bonus=Decimal("0"),
                        deduction=Decimal("0"),
                        net_amount=net_amount,
                        status=SalaryPayment.Status.PENDING,
                        payment_method="CASH",
                        created_by_id=created_by_id,
                    )
                created += 1
            except IntegrityError:
                skipped += 1

        return ServiceResponse.success(data={
            "created": created,
            "skipped": skipped,
            "total_employees": created + skipped,
        }, message=f"Payroll generated: {created} records created, {skipped} skipped")

    @classmethod
    @transaction.atomic
    def update(cls, salary_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        salary = SalaryPaymentRepository.get_with_relations(salary_id)
        if not salary:
            return ServiceResponse.not_found(
                f"Salary payment with id {salary_id} not found"
            )

        if salary.status != SalaryPayment.Status.PENDING:
            return ServiceResponse.error(
                "Can only update salary payments in PENDING status"
            )

        update_fields = []
        for field in ["base_amount", "bonus", "deduction", "payment_method", "notes"]:
            if field in kwargs:
                value = kwargs[field]
                if field in ["base_amount", "bonus", "deduction"]:
                    value = Decimal(str(value))
                    # Same invariant as create(): non-negative. Without this,
                    # PATCH /salaries/<id> {"deduction":"-100000"} inflates
                    # net_amount and drains the cash register on pay.
                    if value < 0:
                        return ServiceResponse.validation_error(
                            errors={field: 'must be non-negative'},
                        )
                setattr(salary, field, value)
                update_fields.append(field)

        if any(f in kwargs for f in ["base_amount", "bonus", "deduction"]):
            salary.net_amount = salary.base_amount + salary.bonus - salary.deduction
            if "net_amount" not in update_fields:
                update_fields.append("net_amount")

        if update_fields:
            salary.save(update_fields=update_fields)

        salary = SalaryPaymentRepository.get_with_relations(salary.pk)

        return ServiceResponse.success(data={
            "salary": cls.serialize(salary),
        }, message="Salary payment updated")

    @classmethod
    @transaction.atomic
    def delete(cls, salary_id: int) -> Tuple[Dict[str, Any], int]:
        salary = SalaryPaymentRepository.get_by_id(salary_id)
        if not salary:
            return ServiceResponse.not_found(
                f"Salary payment with id {salary_id} not found"
            )

        if salary.status != SalaryPayment.Status.PENDING:
            return ServiceResponse.error(
                "Can only delete salary payments in PENDING status"
            )

        salary.is_deleted = True
        salary.save(update_fields=["is_deleted"])

        return ServiceResponse.success(data={
            "id": salary_id,
        }, message="Salary payment deleted")

    @classmethod
    @transaction.atomic
    def approve(cls,
                salary_id: int,
                approved_by_id: int) -> Tuple[Dict[str, Any], int]:
        salary = SalaryPaymentRepository.get_for_update(salary_id)
        if not salary:
            return ServiceResponse.not_found(
                f"Salary payment with id {salary_id} not found"
            )

        if salary.status != SalaryPayment.Status.PENDING:
            return ServiceResponse.error(
                f"Cannot approve salary in {salary.status} status. Must be PENDING."
            )

        salary.status = SalaryPayment.Status.APPROVED
        salary.approved_by_id = approved_by_id
        salary.save(update_fields=["status", "approved_by_id"])

        salary = SalaryPaymentRepository.get_with_relations(salary.pk)
        return ServiceResponse.success(data={
            "salary": cls.serialize(salary),
        }, message="Salary payment approved")

    @classmethod
    @transaction.atomic
    def approve_all(cls,
                    year: int,
                    month: int,
                    approved_by_id: int) -> Tuple[Dict[str, Any], int]:
        pending = SalaryPayment.objects.filter(
            period_year=year,
            period_month=month,
            status=SalaryPayment.Status.PENDING,
            is_deleted=False,
        )

        # Iterate and save() per row instead of bulk .update(), otherwise
        # SyncMixin's save() is bypassed: sync_version isn't bumped and
        # synced_at isn't nulled, so the cloud never learns about these
        # approvals.
        count = 0
        for salary in pending:
            salary.status = SalaryPayment.Status.APPROVED
            salary.approved_by_id = approved_by_id
            salary.save(update_fields=["status", "approved_by_id", "updated_at"])
            count += 1

        if count == 0:
            return ServiceResponse.success(data={
                "approved": 0,
            }, message="No pending salary payments for this period")

        return ServiceResponse.success(data={
            "approved": count,
        }, message=f"{count} salary payment(s) approved")

    @classmethod
    @transaction.atomic
    def pay(cls,
            salary_id: int,
            paid_by_id: int,
            payment_method: str = "CASH") -> Tuple[Dict[str, Any], int]:
        salary = SalaryPaymentRepository.get_for_update(salary_id)
        if not salary:
            return ServiceResponse.not_found(
                f"Salary payment with id {salary_id} not found"
            )

        if salary.status != SalaryPayment.Status.APPROVED:
            return ServiceResponse.error(
                f"Cannot pay salary in {salary.status} status. Must be APPROVED."
            )

        from hr.services.cash_transaction_service import CashTransactionService

        employee_name = ""
        if salary.employee and salary.employee.user:
            user = salary.employee.user
            employee_name = f"{user.first_name} {user.last_name}"

        result, status = CashTransactionService.create_for_reference(
            type="SALARY_PAYMENT",
            amount=salary.net_amount,
            description=f"Salary: {employee_name} - {salary.period_year}/{salary.period_month}",
            payment_method=payment_method,
            reference_type="SalaryPayment",
            reference_id=salary.id,
            performed_by_id=paid_by_id,
        )
        if status >= 400:
            return result, status

        salary.status = SalaryPayment.Status.PAID
        salary.payment_method = payment_method
        salary.paid_at = timezone.now()
        salary.save(update_fields=["status", "payment_method", "paid_at"])

        salary = SalaryPaymentRepository.get_with_relations(salary.pk)

        return ServiceResponse.success(data={
            "salary": cls.serialize(salary),
        }, message="Salary payment completed")

    @classmethod
    def get_payroll_summary(cls,
                            year: int,
                            month: int) -> Tuple[Dict[str, Any], int]:
        summary = SalaryPaymentRepository.get_payroll_summary(year, month)

        by_status = dict(
            SalaryPayment.objects.filter(
                period_year=year,
                period_month=month,
                is_deleted=False,
            ).values_list("status").annotate(count=Sum("net_amount"))
        )

        return ServiceResponse.success(data={
            "year": year,
            "month": month,
            "count": summary["count"],
            "total_base": str(summary["total_base"] or 0),
            "total_bonus": str(summary["total_bonus"] or 0),
            "total_deduction": str(summary["total_deduction"] or 0),
            "total_net": str(summary["total_net"] or 0),
            "by_status": {k: str(v) for k, v in by_status.items()},
        })
