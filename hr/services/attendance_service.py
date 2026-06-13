from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.core.paginator import Paginator
from django.utils import timezone

from base.helpers.response import ServiceResponse
from hr.models import Attendance
from hr.repositories import EmployeeRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class AttendanceService:

    @classmethod
    def _serialize(cls, attendance: Attendance) -> Dict[str, Any]:
        data = {
            "id": attendance.id,
            "uuid": str(attendance.uuid),
            "employee": None,
            "date": attendance.date.isoformat(),
            "check_in": attendance.check_in.isoformat() if attendance.check_in else None,
            "check_out": attendance.check_out.isoformat() if attendance.check_out else None,
            "status": attendance.status,
            "status_display": attendance.get_status_display(),
            "source": attendance.source,
            "source_display": attendance.get_source_display(),
            "work_hours": str(attendance.work_hours),
            "overtime_hours": str(attendance.overtime_hours),
            "notes": attendance.notes,
            "created_at": attendance.created_at.isoformat(),
            "updated_at": attendance.updated_at.isoformat(),
        }

        if attendance.employee_id and attendance.employee:
            emp = attendance.employee
            emp_data = {"id": emp.id, "position": emp.position}
            if emp.user:
                emp_data["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }
            data["employee"] = emp_data

        return data

    @classmethod
    @transaction.atomic
    def auto_check_in(cls, user_id: int) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_user(user_id)
        if not employee:
            return ServiceResponse.not_found("Employee profile not found for this user")

        today = timezone.localdate()
        existing = Attendance.objects.filter(
            employee_id=employee.id, date=today, is_deleted=False
        ).first()

        if existing and existing.check_in:
            return ServiceResponse.success(data={
                "attendance": cls._serialize(existing),
                "already_checked_in": True,
            }, message="Already checked in for today")

        if existing:
            existing.check_in = timezone.now()
            existing.source = Attendance.Source.AUTO_POS
            existing.status = Attendance.Status.PRESENT
            existing.save(update_fields=["check_in", "source", "status", "updated_at"])
            attendance = existing
        else:
            attendance = Attendance.objects.create(
                employee_id=employee.id,
                date=today,
                check_in=timezone.now(),
                source=Attendance.Source.AUTO_POS,
                status=Attendance.Status.PRESENT,
            )

        attendance = Attendance.objects.select_related(
            'employee__user'
        ).get(pk=attendance.pk)

        return ServiceResponse.success(data={
            "attendance": cls._serialize(attendance),
            "already_checked_in": False,
        }, message="Checked in successfully")

    # Auto-checkout caps at this many hours; sessions longer than this are
    # almost certainly stale (forgot to check out yesterday) and need manual
    # reconciliation rather than booking a 24h+ shift.
    MAX_SESSION_HOURS = Decimal("16")

    @classmethod
    def _compute_work_hours(cls, check_in_dt, check_out_dt):
        # Returns (work_hours, error_message_or_None). Rejects negative
        # durations and sessions longer than MAX_SESSION_HOURS so a forgotten
        # check-out from a previous day can't book an absurd shift.
        delta_seconds = (check_out_dt - check_in_dt).total_seconds()
        if delta_seconds < 0:
            return None, "check_out is before check_in"
        work_hours = (Decimal(delta_seconds) / Decimal(3600)).quantize(Decimal("0.01"))
        if work_hours > cls.MAX_SESSION_HOURS:
            return None, (
                f"Session duration {work_hours}h exceeds {cls.MAX_SESSION_HOURS}h cap; "
                f"reconcile attendance manually."
            )
        return work_hours, None

    @classmethod
    @transaction.atomic
    def auto_check_out(cls, user_id: int) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_user(user_id)
        if not employee:
            return ServiceResponse.not_found("Employee profile not found for this user")

        today = timezone.localdate()
        attendance = Attendance.objects.filter(
            employee_id=employee.id, date=today, is_deleted=False
        ).first()

        if not attendance:
            return ServiceResponse.not_found("No attendance record found for today")

        if not attendance.check_in:
            return ServiceResponse.error("Cannot check out without checking in first")

        now = timezone.now()
        work_hours, err = cls._compute_work_hours(attendance.check_in, now)
        if err:
            return ServiceResponse.error(err)

        attendance.check_out = now
        overtime = max(Decimal("0"), work_hours - Decimal("8"))

        attendance.work_hours = work_hours
        attendance.overtime_hours = overtime
        attendance.save(update_fields=[
            "check_out", "work_hours", "overtime_hours", "updated_at",
        ])

        attendance = Attendance.objects.select_related(
            'employee__user'
        ).get(pk=attendance.pk)

        return ServiceResponse.success(data={
            "attendance": cls._serialize(attendance),
        }, message="Checked out successfully")

    @classmethod
    @transaction.atomic
    def check_in(cls,
                 employee_id: int,
                 notes: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        today = timezone.localdate()
        # Lock the (employee, date) row if it exists so two concurrent
        # check-in POSTs serialize. Without the lock the second request
        # raced past the duplicate guard and hit unique_together at the
        # DB layer, returning an uncaught IntegrityError → 500.
        from django.db import IntegrityError
        existing = Attendance.objects.select_for_update().filter(
            employee_id=employee_id, date=today, is_deleted=False
        ).first()

        if existing and existing.check_in:
            return ServiceResponse.error("Employee already checked in for today")

        if existing:
            existing.check_in = timezone.now()
            existing.source = Attendance.Source.MANUAL
            existing.status = Attendance.Status.PRESENT
            existing.notes = notes
            existing.save(update_fields=["check_in", "source", "status", "notes", "updated_at"])
            attendance = existing
        else:
            try:
                attendance = Attendance.objects.create(
                    employee_id=employee_id,
                    date=today,
                    check_in=timezone.now(),
                    source=Attendance.Source.MANUAL,
                    status=Attendance.Status.PRESENT,
                    notes=notes,
                )
            except IntegrityError:
                return ServiceResponse.error("Employee already checked in for today")

        attendance = Attendance.objects.select_related(
            'employee__user'
        ).get(pk=attendance.pk)

        return ServiceResponse.success(data={
            "attendance": cls._serialize(attendance),
        }, message="Manual check-in recorded")

    @classmethod
    @transaction.atomic
    def check_out(cls,
                  employee_id: int,
                  notes: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        today = timezone.localdate()
        attendance = Attendance.objects.filter(
            employee_id=employee_id, date=today, is_deleted=False
        ).first()

        if not attendance:
            return ServiceResponse.not_found("No attendance record found for today")

        if not attendance.check_in:
            return ServiceResponse.error("Cannot check out without checking in first")

        now = timezone.now()
        work_hours, err = cls._compute_work_hours(attendance.check_in, now)
        if err:
            return ServiceResponse.error(err)

        attendance.check_out = now
        overtime = max(Decimal("0"), work_hours - Decimal("8"))

        attendance.work_hours = work_hours
        attendance.overtime_hours = overtime
        if notes:
            attendance.notes = notes
        attendance.save(update_fields=[
            "check_out", "work_hours", "overtime_hours", "notes", "updated_at",
        ])

        attendance = Attendance.objects.select_related(
            'employee__user'
        ).get(pk=attendance.pk)

        return ServiceResponse.success(data={
            "attendance": cls._serialize(attendance),
        }, message="Manual check-out recorded")

    @classmethod
    @transaction.atomic
    def mark_absent(cls,
                    employee_id: int,
                    date=None,
                    notes: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        if date is None:
            date = timezone.localdate()

        existing = Attendance.objects.filter(
            employee_id=employee_id, date=date, is_deleted=False
        ).first()

        if existing:
            return ServiceResponse.error(
                f"Attendance record already exists for {date.isoformat()}"
            )

        attendance = Attendance.objects.create(
            employee_id=employee_id,
            date=date,
            status=Attendance.Status.ABSENT,
            source=Attendance.Source.MANUAL,
            notes=notes,
        )

        attendance = Attendance.objects.select_related(
            'employee__user'
        ).get(pk=attendance.pk)

        return ServiceResponse.created(data={
            "attendance": cls._serialize(attendance),
        }, message="Absence recorded")

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             employee_id: int = None,
             date=None,
             status: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = Attendance.objects.filter(
            is_deleted=False
        ).select_related('employee__user')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if date:
            queryset = queryset.filter(date=date)

        if status:
            queryset = queryset.filter(status=status)

        queryset = queryset.order_by('-date', 'employee__user__first_name')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "attendances": [cls._serialize(a) for a in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in Attendance.Status.choices
            ],
        })

    @classmethod
    def get(cls, attendance_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            attendance = Attendance.objects.select_related(
                'employee__user'
            ).get(pk=attendance_id, is_deleted=False)
        except Attendance.DoesNotExist:
            return ServiceResponse.not_found(
                f"Attendance record with id {attendance_id} not found"
            )

        return ServiceResponse.success(data={
            "attendance": cls._serialize(attendance),
        })

    @classmethod
    def get_daily_report(cls, date=None) -> Tuple[Dict[str, Any], int]:
        if date is None:
            date = timezone.localdate()

        records = Attendance.objects.filter(
            date=date, is_deleted=False
        ).select_related('employee__user')

        # Single aggregate instead of six full scans of the same row set.
        from django.db.models import Count, Q
        stats = records.aggregate(
            total=Count('id'),
            present=Count('id', filter=Q(status=Attendance.Status.PRESENT)),
            absent=Count('id', filter=Q(status=Attendance.Status.ABSENT)),
            late=Count('id', filter=Q(status=Attendance.Status.LATE)),
            half_day=Count('id', filter=Q(status=Attendance.Status.HALF_DAY)),
            on_leave=Count('id', filter=Q(status=Attendance.Status.ON_LEAVE)),
        )

        return ServiceResponse.success(data={
            "date": date.isoformat(),
            "attendances": [cls._serialize(a) for a in records],
            "stats": stats,
        })

    @classmethod
    def get_monthly_report(cls,
                           employee_id: int,
                           year: int,
                           month: int) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        records = Attendance.objects.filter(
            employee_id=employee_id,
            date__year=year,
            date__month=month,
            is_deleted=False,
        ).select_related('employee__user').order_by('date')

        days_present = records.filter(status=Attendance.Status.PRESENT).count()
        days_absent = records.filter(status=Attendance.Status.ABSENT).count()
        days_late = records.filter(status=Attendance.Status.LATE).count()

        totals = records.aggregate(
            total_hours=Sum('work_hours'),
            total_overtime=Sum('overtime_hours'),
        )

        return ServiceResponse.success(data={
            "employee_id": employee_id,
            "year": year,
            "month": month,
            "attendances": [cls._serialize(a) for a in records],
            "summary": {
                "days_present": days_present,
                "days_absent": days_absent,
                "days_late": days_late,
                "total_records": records.count(),
                "total_hours": str(totals["total_hours"] or Decimal("0")),
                "total_overtime": str(totals["total_overtime"] or Decimal("0")),
            },
        })
