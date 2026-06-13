from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import IntegrityError, transaction
from django.core.paginator import Paginator
from django.utils import timezone

from base.helpers.response import ServiceResponse
from hr.models import LeaveType, LeaveRequest, LeaveBalance, Employee
from hr.repositories import EmployeeRepository
from hr.repositories.leave_type import LeaveTypeRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class LeaveService:

    # ── LeaveType serializer ──────────────────────────────────────

    @classmethod
    def _serialize_type(cls, leave_type: LeaveType) -> Dict[str, Any]:
        return {
            "id": leave_type.id,
            "uuid": str(leave_type.uuid),
            "name": leave_type.name,
            "short_name": leave_type.short_name,
            "is_paid": leave_type.is_paid,
            "annual_quota": leave_type.annual_quota,
            "max_carryover": leave_type.max_carryover,
            "requires_approval": leave_type.requires_approval,
            "is_active": leave_type.is_active,
            "created_at": leave_type.created_at.isoformat(),
            "updated_at": leave_type.updated_at.isoformat(),
        }

    @classmethod
    def _serialize_request(cls, leave_req: LeaveRequest) -> Dict[str, Any]:
        data = {
            "id": leave_req.id,
            "uuid": str(leave_req.uuid),
            "employee": None,
            "leave_type": None,
            "start_date": leave_req.start_date.isoformat(),
            "end_date": leave_req.end_date.isoformat(),
            "days_count": str(leave_req.days_count),
            "reason": leave_req.reason,
            "status": leave_req.status,
            "status_display": leave_req.get_status_display(),
            "approved_by": None,
            "notes": leave_req.notes,
            "created_at": leave_req.created_at.isoformat(),
            "updated_at": leave_req.updated_at.isoformat(),
        }

        if leave_req.employee_id and leave_req.employee:
            emp = leave_req.employee
            emp_data = {"id": emp.id, "position": emp.position}
            if emp.user:
                emp_data["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }
            data["employee"] = emp_data

        if leave_req.leave_type_id and leave_req.leave_type:
            data["leave_type"] = {
                "id": leave_req.leave_type.id,
                "name": leave_req.leave_type.name,
                "short_name": leave_req.leave_type.short_name,
            }

        if leave_req.approved_by_id and leave_req.approved_by:
            data["approved_by"] = {
                "id": leave_req.approved_by.id,
                "first_name": leave_req.approved_by.first_name,
                "last_name": leave_req.approved_by.last_name,
            }

        return data

    @classmethod
    def _serialize_balance(cls, balance: LeaveBalance) -> Dict[str, Any]:
        return {
            "id": balance.id,
            "uuid": str(balance.uuid),
            "employee_id": balance.employee_id,
            "leave_type": {
                "id": balance.leave_type.id,
                "name": balance.leave_type.name,
            } if balance.leave_type_id and balance.leave_type else None,
            "year": balance.year,
            "allocated_days": str(balance.allocated_days),
            "used_days": str(balance.used_days),
            "carried_over": str(balance.carried_over),
            "remaining_days": str(balance.remaining_days),
            "created_at": balance.created_at.isoformat(),
            "updated_at": balance.updated_at.isoformat(),
        }

    # ── LeaveType CRUD ────────────────────────────────────────────

    @classmethod
    def list_types(cls, is_active: bool = None) -> Tuple[Dict[str, Any], int]:
        queryset = LeaveType.objects.filter(is_deleted=False)

        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)

        queryset = queryset.order_by('name')

        return ServiceResponse.success(data={
            "leave_types": [cls._serialize_type(lt) for lt in queryset],
        })

    @classmethod
    def get_type(cls, type_id: int) -> Tuple[Dict[str, Any], int]:
        leave_type = LeaveTypeRepository.get_by_id(type_id)
        if not leave_type:
            return ServiceResponse.not_found(
                f"Leave type with id {type_id} not found"
            )

        return ServiceResponse.success(data={
            "leave_type": cls._serialize_type(leave_type),
        })

    @classmethod
    @transaction.atomic
    def create_type(cls,
                    name: str,
                    short_name: str = "",
                    annual_quota: int = 0,
                    max_carryover: int = 0,
                    is_paid: bool = True,
                    requires_approval: bool = True) -> Tuple[Dict[str, Any], int]:
        if LeaveTypeRepository.name_exists(name):
            return ServiceResponse.validation_error(
                errors={"name": "A leave type with this name already exists"},
            )

        leave_type = LeaveTypeRepository.create(
            name=name,
            short_name=short_name,
            annual_quota=annual_quota,
            max_carryover=max_carryover,
            is_paid=is_paid,
            requires_approval=requires_approval,
        )

        return ServiceResponse.created(data={
            "leave_type": cls._serialize_type(leave_type),
        }, message="Leave type created")

    @classmethod
    @transaction.atomic
    def update_type(cls, type_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        leave_type = LeaveTypeRepository.get_by_id(type_id)
        if not leave_type:
            return ServiceResponse.not_found(
                f"Leave type with id {type_id} not found"
            )

        allowed_fields = [
            "name", "short_name", "annual_quota", "max_carryover",
            "is_paid", "requires_approval", "is_active",
        ]

        if "name" in kwargs and kwargs["name"] != leave_type.name:
            if LeaveTypeRepository.name_exists(kwargs["name"], exclude_id=type_id):
                return ServiceResponse.validation_error(
                    errors={"name": "A leave type with this name already exists"},
                )

        update_fields = ["updated_at"]
        for field in allowed_fields:
            if field in kwargs:
                setattr(leave_type, field, kwargs[field])
                update_fields.append(field)

        leave_type.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "leave_type": cls._serialize_type(leave_type),
        }, message="Leave type updated")

    @classmethod
    @transaction.atomic
    def delete_type(cls, type_id: int) -> Tuple[Dict[str, Any], int]:
        leave_type = LeaveTypeRepository.get_by_id(type_id)
        if not leave_type:
            return ServiceResponse.not_found(
                f"Leave type with id {type_id} not found"
            )

        leave_type.is_active = False
        leave_type.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "id": type_id,
        }, message="Leave type deactivated")

    # ── LeaveRequest ──────────────────────────────────────────────

    @classmethod
    def list_requests(cls,
                      page: int = 1,
                      per_page: int = 20,
                      employee_id: int = None,
                      status: str = None,
                      leave_type_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = LeaveRequest.objects.filter(
            is_deleted=False
        ).select_related('employee__user', 'leave_type', 'approved_by')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if status:
            queryset = queryset.filter(status=status)

        if leave_type_id:
            queryset = queryset.filter(leave_type_id=leave_type_id)

        queryset = queryset.order_by('-start_date')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "leave_requests": [cls._serialize_request(lr) for lr in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in LeaveRequest.Status.choices
            ],
        })

    @classmethod
    def get_request(cls, leave_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            leave_req = LeaveRequest.objects.select_related(
                'employee__user', 'leave_type', 'approved_by'
            ).get(pk=leave_id, is_deleted=False)
        except LeaveRequest.DoesNotExist:
            return ServiceResponse.not_found(
                f"Leave request with id {leave_id} not found"
            )

        return ServiceResponse.success(data={
            "leave_request": cls._serialize_request(leave_req),
        })

    @classmethod
    @transaction.atomic
    def create_request(cls,
                       employee_id: int,
                       leave_type_id: int,
                       start_date,
                       end_date,
                       reason: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        leave_type = LeaveTypeRepository.get_by_id(leave_type_id)
        if not leave_type:
            return ServiceResponse.not_found("Leave type not found")

        # Reject reverse-dated ranges. Without this, end < start produces a
        # negative `days_count`; approval then credits `balance.used_days +=
        # negative` and inflates the employee's remaining quota arbitrarily.
        if end_date < start_date:
            return ServiceResponse.validation_error(
                errors={'end_date': 'must be on or after start_date'},
            )

        days_count = Decimal(str((end_date - start_date).days + 1))

        # Reject overlapping pending / approved requests for the same
        # employee. Without this the same employee can submit multiple
        # overlapping requests; each one passes the balance check
        # independently (no aggregation) and each approval debits the
        # balance, allowing over-allocation.
        overlapping = LeaveRequest.objects.filter(
            employee_id=employee_id,
            is_deleted=False,
            status__in=(LeaveRequest.Status.PENDING, LeaveRequest.Status.APPROVED),
            start_date__lte=end_date,
            end_date__gte=start_date,
        ).exists()
        if overlapping:
            return ServiceResponse.error(
                "This employee already has a pending or approved leave request "
                "that overlaps with the requested date range.",
            )

        year = start_date.year
        balance = LeaveBalance.objects.filter(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            is_deleted=False,
        ).first()

        if balance:
            remaining = balance.remaining_days
            if days_count > remaining:
                return ServiceResponse.validation_error(
                    errors={"days_count": f"Insufficient leave balance. Remaining: {remaining}, Requested: {days_count}"},
                )
        elif leave_type.annual_quota > 0:
            # Quota-tracked type with no balance row for this year means the
            # employee has zero days available — treat as 0 rather than
            # silently allowing unlimited leave. Types with annual_quota == 0
            # (e.g. unpaid leave) are not balance-tracked and pass through.
            return ServiceResponse.validation_error(
                errors={"days_count": f"No leave balance configured for this type/year ({year})"},
            )

        leave_req = LeaveRequest.objects.create(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            start_date=start_date,
            end_date=end_date,
            days_count=days_count,
            reason=reason,
            status=LeaveRequest.Status.PENDING,
        )

        leave_req = LeaveRequest.objects.select_related(
            'employee__user', 'leave_type', 'approved_by'
        ).get(pk=leave_req.pk)

        return ServiceResponse.created(data={
            "leave_request": cls._serialize_request(leave_req),
        }, message="Leave request created")

    @classmethod
    @transaction.atomic
    def approve(cls,
                leave_id: int,
                approved_by_id: int) -> Tuple[Dict[str, Any], int]:
        # Lock the LeaveRequest row first so concurrent approvals serialize.
        # Without the row lock, two requests could both pass the PENDING check
        # and each debit the balance, double-counting used_days.
        try:
            leave_req = LeaveRequest.objects.select_for_update().get(
                pk=leave_id, is_deleted=False
            )
        except LeaveRequest.DoesNotExist:
            return ServiceResponse.not_found(
                f"Leave request with id {leave_id} not found"
            )

        # Re-check status under the lock — a concurrent approval may have
        # already flipped it.
        if leave_req.status != LeaveRequest.Status.PENDING:
            return ServiceResponse.error(
                f"Cannot approve leave request in {leave_req.status} status. Must be PENDING."
            )

        leave_type = leave_req.leave_type

        # Lock the balance row so concurrent approvals serialize and re-check
        # the available balance under the lock — request-time validation in
        # create_request is unlocked and may have been satisfied for both.
        balance = LeaveBalance.objects.select_for_update().filter(
            employee_id=leave_req.employee_id,
            leave_type_id=leave_req.leave_type_id,
            year=leave_req.start_date.year,
            is_deleted=False,
        ).first()

        if balance:
            remaining = balance.remaining_days
            if leave_req.days_count > remaining:
                return ServiceResponse.validation_error(
                    errors={"days_count": f"Insufficient leave balance. Remaining: {remaining}, Requested: {leave_req.days_count}"},
                )
        elif leave_type and leave_type.annual_quota > 0:
            # Quota-tracked type with no balance row means zero days available.
            # Reject rather than silently approving unlimited leave. Types with
            # annual_quota == 0 (e.g. unpaid leave) are not balance-tracked.
            return ServiceResponse.validation_error(
                errors={"days_count": f"No leave balance configured for this type/year ({leave_req.start_date.year})"},
            )

        leave_req.status = LeaveRequest.Status.APPROVED
        leave_req.approved_by_id = approved_by_id
        leave_req.save(update_fields=["status", "approved_by_id", "updated_at"])

        if balance:
            balance.used_days += leave_req.days_count
            balance.save(update_fields=["used_days", "updated_at"])

        leave_req = LeaveRequest.objects.select_related(
            'employee__user', 'leave_type', 'approved_by'
        ).get(pk=leave_req.pk)

        return ServiceResponse.success(data={
            "leave_request": cls._serialize_request(leave_req),
        }, message="Leave request approved")

    @classmethod
    @transaction.atomic
    def reject(cls,
               leave_id: int,
               approved_by_id: int,
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        try:
            leave_req = LeaveRequest.objects.select_related(
                'employee__user', 'leave_type', 'approved_by'
            ).get(pk=leave_id, is_deleted=False)
        except LeaveRequest.DoesNotExist:
            return ServiceResponse.not_found(
                f"Leave request with id {leave_id} not found"
            )

        if leave_req.status != LeaveRequest.Status.PENDING:
            return ServiceResponse.error(
                f"Cannot reject leave request in {leave_req.status} status. Must be PENDING."
            )

        leave_req.status = LeaveRequest.Status.REJECTED
        leave_req.approved_by_id = approved_by_id
        leave_req.notes = notes
        leave_req.save(update_fields=["status", "approved_by_id", "notes", "updated_at"])

        leave_req = LeaveRequest.objects.select_related(
            'employee__user', 'leave_type', 'approved_by'
        ).get(pk=leave_req.pk)

        return ServiceResponse.success(data={
            "leave_request": cls._serialize_request(leave_req),
        }, message="Leave request rejected")

    @classmethod
    @transaction.atomic
    def cancel(cls, leave_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            leave_req = LeaveRequest.objects.select_related(
                'employee__user', 'leave_type', 'approved_by'
            ).get(pk=leave_id, is_deleted=False)
        except LeaveRequest.DoesNotExist:
            return ServiceResponse.not_found(
                f"Leave request with id {leave_id} not found"
            )

        if leave_req.status not in (LeaveRequest.Status.PENDING, LeaveRequest.Status.APPROVED):
            return ServiceResponse.error(
                f"Cannot cancel leave request in {leave_req.status} status"
            )

        was_approved = leave_req.status == LeaveRequest.Status.APPROVED

        leave_req.status = LeaveRequest.Status.CANCELED
        leave_req.save(update_fields=["status", "updated_at"])

        if was_approved:
            balance = LeaveBalance.objects.filter(
                employee_id=leave_req.employee_id,
                leave_type_id=leave_req.leave_type_id,
                year=leave_req.start_date.year,
                is_deleted=False,
            ).first()

            if balance:
                balance.used_days = max(Decimal("0"), balance.used_days - leave_req.days_count)
                balance.save(update_fields=["used_days", "updated_at"])

        leave_req = LeaveRequest.objects.select_related(
            'employee__user', 'leave_type', 'approved_by'
        ).get(pk=leave_req.pk)

        return ServiceResponse.success(data={
            "leave_request": cls._serialize_request(leave_req),
        }, message="Leave request cancelled")

    # ── Balance ───────────────────────────────────────────────────

    @classmethod
    def get_balance(cls,
                    employee_id: int,
                    year: int = None) -> Tuple[Dict[str, Any], int]:
        if not Employee.objects.filter(pk=employee_id, is_deleted=False).exists():
            return ServiceResponse.not_found("Employee not found")

        if year is None:
            year = timezone.now().year

        balances = LeaveBalance.objects.filter(
            employee_id=employee_id,
            year=year,
            is_deleted=False,
        ).select_related('leave_type')

        return ServiceResponse.success(data={
            "balances": [cls._serialize_balance(b) for b in balances],
            "employee_id": employee_id,
            "year": year,
        })

    @classmethod
    @transaction.atomic
    def initialize_annual_balances(cls, year: int) -> Tuple[Dict[str, Any], int]:
        active_employees = EmployeeRepository.get_active()
        active_types = LeaveTypeRepository.get_active()
        previous_year = year - 1

        created = 0
        skipped = 0

        for employee in active_employees:
            for leave_type in active_types:
                if LeaveBalance.objects.filter(
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year=year,
                    is_deleted=False,
                ).exists():
                    skipped += 1
                    continue

                carried_over = Decimal("0")
                prev_balance = LeaveBalance.objects.filter(
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year=previous_year,
                    is_deleted=False,
                ).first()

                if prev_balance and leave_type.max_carryover > 0:
                    remaining = prev_balance.remaining_days
                    carried_over = min(remaining, Decimal(str(leave_type.max_carryover)))
                    carried_over = max(Decimal("0"), carried_over)

                # Savepoint + catch so a concurrent initialize that created this
                # (employee, leave_type, year) row between our exists() check and
                # this insert doesn't IntegrityError out and roll back the whole
                # batch — just count it as skipped.
                try:
                    with transaction.atomic():
                        LeaveBalance.objects.create(
                            employee_id=employee.id,
                            leave_type_id=leave_type.id,
                            year=year,
                            allocated_days=Decimal(str(leave_type.annual_quota)),
                            used_days=Decimal("0"),
                            carried_over=carried_over,
                        )
                    created += 1
                except IntegrityError:
                    skipped += 1

        return ServiceResponse.success(data={
            "year": year,
            "created": created,
            "skipped": skipped,
        }, message=f"Annual balances initialized: {created} created, {skipped} skipped")

    @classmethod
    def get_calendar(cls,
                     year: int,
                     month: int) -> Tuple[Dict[str, Any], int]:
        import calendar
        _, last_day = calendar.monthrange(year, month)

        from datetime import date
        month_start = date(year, month, 1)
        month_end = date(year, month, last_day)

        leaves = LeaveRequest.objects.filter(
            status=LeaveRequest.Status.APPROVED,
            start_date__lte=month_end,
            end_date__gte=month_start,
            is_deleted=False,
        ).select_related('employee__user', 'leave_type')

        return ServiceResponse.success(data={
            "year": year,
            "month": month,
            "leaves": [cls._serialize_request(lr) for lr in leaves],
            "count": leaves.count(),
        })
