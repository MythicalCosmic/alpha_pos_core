from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.models import Order
from hr.models import (
    Attendance, AttendanceAdjustmentRequest, AttendanceExcuse,
    DisciplinaryCase, DisciplinaryRule, Employee, EmployeeWorkSchedule,
    OperationalAuditEvent, PreparationAudit, PreparationAuditCategory,
    PreparationAuditPeriodClose, PreparationAuditReview, SalaryDeduction,
    SalaryPayment,
)

LOCAL_TZ = ZoneInfo('Asia/Tashkent')


def _iso(value):
    return value.astimezone(LOCAL_TZ).isoformat() if value else None


def _parse_date(value, field='date'):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f'{field} must use YYYY-MM-DD')


def _parse_time(value, field):
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f'{field} must use HH:MM[:SS]')


def _parse_local_datetime(value, field, *, allow_none=False):
    if value in (None, '') and allow_none:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f'{field} must be an ISO-8601 datetime')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f'{field} must include the Asia/Tashkent UTC+05:00 offset')
    local = parsed.astimezone(LOCAL_TZ)
    if parsed.utcoffset() != local.utcoffset():
        raise ValueError(f'{field} must use the Asia/Tashkent UTC+05:00 offset')
    return local


def _page(qs, page, per_page):
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page)
    return page_obj, {
        'page': page_obj.number, 'per_page': per_page, 'total': paginator.count,
        'total_pages': paginator.num_pages, 'has_next': page_obj.has_next(),
        'has_previous': page_obj.has_previous(),
    }


def _actor(user):
    if not user:
        return None
    return {'id': user.id, 'name': f'{user.first_name} {user.last_name}'.strip()}


def record_event(entity, action, actor, previous='', new='', reason='', metadata=None):
    return OperationalAuditEvent.objects.create(
        entity_type=entity.__class__.__name__, entity_id=entity.pk,
        action=action, actor=actor, previous_state=previous or '',
        new_state=new or '', reason=reason or '', metadata=metadata or {},
    )


def event_history(entity):
    return [{
        'action': row.action, 'actor': _actor(row.actor),
        'occurred_at': _iso(row.occurred_at),
        'previous_state': row.previous_state, 'new_state': row.new_state,
        'reason': row.reason, 'metadata': row.metadata,
    } for row in OperationalAuditEvent.objects.filter(
        entity_type=entity.__class__.__name__, entity_id=entity.pk,
    ).select_related('actor')]


def _schedule_payload(row):
    return {
        'id': row.id, 'employee_id': row.employee_id, 'weekday': row.weekday,
        'scheduled_start_local': row.scheduled_start_local.isoformat(),
        'scheduled_end_local': row.scheduled_end_local.isoformat(),
        'is_overnight': row.is_overnight, 'grace_minutes': row.grace_minutes,
        'effective_from': row.effective_from.isoformat(),
        'effective_to': row.effective_to.isoformat() if row.effective_to else None,
        'created_by': _actor(row.created_by), 'updated_by': _actor(row.updated_by),
        'created_at': _iso(row.created_at), 'updated_at': _iso(row.updated_at),
        'timezone': 'Asia/Tashkent',
    }


class WorkScheduleService:
    @staticmethod
    def list(*, page=1, per_page=20, employee_id=None, date_from=None, date_to=None):
        qs = EmployeeWorkSchedule.objects.filter(is_deleted=False).select_related(
            'employee__user', 'created_by', 'updated_by',
        )
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if date_from:
            qs = qs.filter(Q(effective_to__isnull=True) | Q(effective_to__gte=date_from))
        if date_to:
            qs = qs.filter(effective_from__lte=date_to)
        rows, pagination = _page(qs.order_by('employee_id', 'weekday', '-effective_from'), page, per_page)
        return ServiceResponse.success(data={
            'work_schedules': [_schedule_payload(row) for row in rows],
            'pagination': pagination, 'timezone': 'Asia/Tashkent',
        })

    @staticmethod
    @transaction.atomic
    def create(payload, actor):
        errors = {}
        try:
            employee = Employee.objects.get(id=payload.get('employee_id'), is_deleted=False)
        except Employee.DoesNotExist:
            employee = None
            errors['employee_id'] = 'Employee not found'
        try:
            weekday = int(payload.get('weekday'))
            if weekday not in range(7):
                raise ValueError
        except (TypeError, ValueError):
            weekday = None
            errors['weekday'] = 'Must be between 0 and 6'
        try:
            start = _parse_time(payload.get('scheduled_start_local'), 'scheduled_start_local')
            end = _parse_time(payload.get('scheduled_end_local'), 'scheduled_end_local')
            start_date = _parse_date(payload.get('effective_from'), 'effective_from')
            end_date = _parse_date(payload.get('effective_to'), 'effective_to') if payload.get('effective_to') else None
        except ValueError as exc:
            errors['schedule'] = str(exc)
            start = end = start_date = end_date = None
        try:
            grace = int(payload.get('grace_minutes', 0))
            if grace < 0:
                raise ValueError
        except (TypeError, ValueError):
            grace = 0
            errors['grace_minutes'] = 'Must be a non-negative integer'
        overnight = bool(payload.get('is_overnight', False))
        if start and end and end <= start and not overnight:
            errors['is_overnight'] = 'Required when end time is not after start time'
        if start_date and end_date and end_date < start_date:
            errors['effective_to'] = 'Must be on or after effective_from'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        overlap = EmployeeWorkSchedule.objects.select_for_update().filter(
            employee=employee, weekday=weekday, is_deleted=False,
            effective_from__lte=end_date or date.max,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=start_date)).exists()
        if overlap:
            return ({'success': False, 'message': 'Schedule effective range overlaps an existing rule',
                     'errors': {'code': 'schedule_overlap'}}, 409)
        row = EmployeeWorkSchedule.objects.create(
            employee=employee, weekday=weekday, scheduled_start_local=start,
            scheduled_end_local=end, is_overnight=overnight,
            grace_minutes=grace, effective_from=start_date, effective_to=end_date,
            created_by=actor, updated_by=actor, branch_id=employee.branch_id,
        )
        record_event(row, 'CREATE', actor, new='ACTIVE')
        return ServiceResponse.created(data={'work_schedule': _schedule_payload(row)})

    @classmethod
    @transaction.atomic
    def patch(cls, schedule_id, payload, actor):
        row = EmployeeWorkSchedule.objects.select_for_update().filter(
            id=schedule_id, is_deleted=False,
        ).first()
        if not row:
            return ServiceResponse.not_found('Work schedule not found')
        today = timezone.localdate()
        if row.effective_from <= today:
            try:
                new_from = _parse_date(payload.get('effective_from'), 'effective_from')
            except ValueError as exc:
                return ServiceResponse.validation_error(errors={'effective_from': str(exc)})
            if new_from <= today:
                return ServiceResponse.validation_error(
                    errors={'effective_from': 'Historical schedules can only be changed prospectively'},
                )
            original_to = row.effective_to
            row.effective_to = new_from - timedelta(days=1)
            row.updated_by = actor
            row.save(update_fields=['effective_to', 'updated_by', 'updated_at'])
            new_payload = {
                'employee_id': row.employee_id, 'weekday': row.weekday,
                'scheduled_start_local': row.scheduled_start_local.isoformat(),
                'scheduled_end_local': row.scheduled_end_local.isoformat(),
                'is_overnight': row.is_overnight, 'grace_minutes': row.grace_minutes,
                'effective_from': new_from.isoformat(),
                'effective_to': original_to.isoformat() if original_to else None,
            }
            new_payload.update(payload)
            result, status = cls.create(new_payload, actor)
            if status < 400:
                record_event(row, 'SUPERSEDE', actor, previous='ACTIVE', new='CLOSED',
                             reason=f'Prospective replacement from {new_from}')
            return result, status
        allowed = {
            'scheduled_start_local', 'scheduled_end_local', 'is_overnight',
            'grace_minutes', 'effective_from', 'effective_to',
        }
        merged = {
            'employee_id': row.employee_id, 'weekday': row.weekday,
            'scheduled_start_local': row.scheduled_start_local.isoformat(),
            'scheduled_end_local': row.scheduled_end_local.isoformat(),
            'is_overnight': row.is_overnight, 'grace_minutes': row.grace_minutes,
            'effective_from': row.effective_from.isoformat(),
            'effective_to': row.effective_to.isoformat() if row.effective_to else None,
        }
        merged.update({key: value for key, value in payload.items() if key in allowed})
        row.delete()
        return cls.create(merged, actor)


def _schedule_for(employee_id, work_date):
    return EmployeeWorkSchedule.objects.filter(
        employee_id=employee_id, weekday=work_date.weekday(), is_deleted=False,
        effective_from__lte=work_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=work_date)).order_by('-effective_from').first()


def _set_schedule_snapshot(attendance, schedule):
    if not schedule:
        attendance.scheduled_start = None
        attendance.scheduled_end = None
        attendance.grace_minutes = 0
        attendance.scheduled_minutes = 0
        return
    start = datetime.combine(attendance.date, schedule.scheduled_start_local, LOCAL_TZ)
    end_date = attendance.date + timedelta(days=1 if schedule.is_overnight else 0)
    end = datetime.combine(end_date, schedule.scheduled_end_local, LOCAL_TZ)
    attendance.scheduled_start = start
    attendance.scheduled_end = end
    attendance.grace_minutes = schedule.grace_minutes
    attendance.scheduled_minutes = max(0, int((end - start).total_seconds() // 60))


def _recalculate_attendance(attendance):
    if attendance.check_in and attendance.check_out:
        attendance.worked_minutes = max(0, int((attendance.check_out - attendance.check_in).total_seconds() // 60))
    else:
        attendance.worked_minutes = 0
    attendance.work_hours = (Decimal(attendance.worked_minutes) / Decimal(60)).quantize(Decimal('0.01'))
    attendance.overtime_minutes = max(0, attendance.worked_minutes - attendance.scheduled_minutes)
    attendance.overtime_hours = (Decimal(attendance.overtime_minutes) / Decimal(60)).quantize(Decimal('0.01'))
    attendance.late_minutes = 0
    attendance.early_leave_minutes = 0
    if attendance.scheduled_start and attendance.check_in:
        allowed = attendance.scheduled_start + timedelta(minutes=attendance.grace_minutes)
        attendance.late_minutes = max(0, int((attendance.check_in - allowed).total_seconds() // 60))
    if attendance.scheduled_end and attendance.check_out:
        attendance.early_leave_minutes = max(0, int((attendance.scheduled_end - attendance.check_out).total_seconds() // 60))
    if not attendance.check_in:
        attendance.status = Attendance.Status.ABSENT
    elif attendance.late_minutes:
        attendance.status = Attendance.Status.LATE
    else:
        attendance.status = Attendance.Status.PRESENT


def _attendance_payload(row, detail=False):
    approved_excuses = row.excuses.filter(status=AttendanceExcuse.Status.APPROVED, is_deleted=False)
    excused_late = sum(x.excused_late_minutes for x in approved_excuses)
    excused_early = sum(x.excused_early_leave_minutes for x in approved_excuses)
    data = {
        'id': row.id, 'employee': {
            'id': row.employee_id,
            'full_name': f'{row.employee.user.first_name} {row.employee.user.last_name}'.strip(),
            'is_active': row.employee.is_active,
        },
        'work_date': row.date.isoformat(), 'date': row.date.isoformat(),
        'scheduled_start': _iso(row.scheduled_start), 'scheduled_end': _iso(row.scheduled_end),
        'grace_minutes': row.grace_minutes, 'check_in': _iso(row.check_in),
        'check_out': _iso(row.check_out), 'worked_minutes': row.worked_minutes,
        'scheduled_minutes': row.scheduled_minutes, 'overtime_minutes': row.overtime_minutes,
        'late_minutes': row.late_minutes, 'early_leave_minutes': row.early_leave_minutes,
        'excused_late_minutes': min(row.late_minutes, excused_late),
        'excused_early_leave_minutes': min(row.early_leave_minutes, excused_early),
        'has_approved_excuse': approved_excuses.exists(),
        'has_adjustment': row.adjustment_requests.exists(), 'status': row.status,
        'source': row.source, 'notes': row.notes, 'created_by': _actor(row.created_by),
        'created_at': _iso(row.created_at), 'updated_at': _iso(row.updated_at),
        'timezone': 'Asia/Tashkent',
    }
    cases = row.disciplinary_cases.all()
    data['penalty_summary'] = {
        'count': cases.count(),
        'approved_total_uzs': sum(c.amount_uzs for c in cases if c.status == DisciplinaryCase.Status.APPROVED),
        'pending_total_uzs': sum(c.amount_uzs for c in cases if c.status in (
            DisciplinaryCase.Status.DRAFT, DisciplinaryCase.Status.SUBMITTED,
            DisciplinaryCase.Status.APPROVED_PENDING_PAYROLL,
        )),
    }
    if detail:
        data['adjustment_requests'] = [AttendanceOperations.serialize_adjustment(x) for x in row.adjustment_requests.select_related('requested_by', 'reviewed_by')]
        data['excuses'] = [AttendanceOperations.serialize_excuse(x) for x in row.excuses.select_related('submitted_by', 'reviewer')]
        data['history'] = event_history(row)
    return data


def _validate_attendance_times(work_date, check_in, check_out, scheduled_end=None):
    errors = {}
    if not check_in and check_out:
        errors['check_in_local'] = 'Required when check_out is provided'
    if check_in and check_in.astimezone(LOCAL_TZ).date() != work_date:
        errors['check_in_local'] = 'Must fall on the business work_date'
    if check_in and check_out and check_out <= check_in:
        errors['check_out_local'] = 'Must be later than check_in'
    if check_out and check_out.astimezone(LOCAL_TZ).date() != work_date:
        overnight_ok = bool(scheduled_end and scheduled_end.astimezone(LOCAL_TZ).date() > work_date)
        if not overnight_ok:
            errors['check_out_local'] = 'Overnight work requires an overnight schedule'
    future_limit = timezone.now() + timedelta(minutes=5)
    if check_in and check_in > future_limit:
        errors['check_in_local'] = 'Future timestamps require a manager exception'
    if check_out and check_out > future_limit:
        errors['check_out_local'] = 'Future timestamps require a manager exception'
    return errors


class AttendanceOperations:
    @staticmethod
    def list(*, page=1, per_page=20, employee_id=None, status=None,
             date_from=None, date_to=None, branch_id=None):
        qs = Attendance.objects.filter(is_deleted=False).select_related(
            'employee__user', 'created_by',
        ).prefetch_related('excuses', 'adjustment_requests', 'disciplinary_cases')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if branch_id:
            qs = qs.filter(employee__user__branch_id=branch_id)
        rows, pagination = _page(qs.order_by('-date', 'employee__user__first_name'), page, per_page)
        return ServiceResponse.success(data={
            'attendances': [_attendance_payload(row) for row in rows],
            'pagination': pagination, 'timezone': 'Asia/Tashkent',
        })

    @staticmethod
    def detail(attendance_id):
        row = Attendance.objects.select_related('employee__user', 'created_by').filter(
            id=attendance_id, is_deleted=False,
        ).first()
        if not row:
            return ServiceResponse.not_found('Attendance not found')
        return ServiceResponse.success(data={'attendance': _attendance_payload(row, detail=True)})

    @staticmethod
    @transaction.atomic
    def manual_entry(payload, actor):
        errors = {}
        try:
            employee = Employee.objects.get(id=payload.get('employee_id'), is_deleted=False)
        except Employee.DoesNotExist:
            employee = None
            errors['employee_id'] = 'Employee not found'
        try:
            work_date = _parse_date(payload.get('work_date'), 'work_date')
            check_in = _parse_local_datetime(payload.get('check_in_local'), 'check_in_local', allow_none=True)
            check_out = _parse_local_datetime(payload.get('check_out_local'), 'check_out_local', allow_none=True)
        except ValueError as exc:
            errors['datetime'] = str(exc)
            work_date = check_in = check_out = None
        if not check_in and not check_out:
            errors['check_in_local'] = 'At least a check-in is required'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        schedule = _schedule_for(employee.id, work_date)
        candidate = Attendance(employee=employee, date=work_date)
        _set_schedule_snapshot(candidate, schedule)
        errors = _validate_attendance_times(work_date, check_in, check_out, candidate.scheduled_end)
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        if Attendance.objects.select_for_update().filter(
            employee=employee, date=work_date, is_deleted=False,
        ).exists():
            return ({'success': False, 'message': 'Attendance already exists; use an adjustment request',
                     'errors': {'code': 'attendance_already_exists'}}, 409)
        candidate.check_in = check_in
        candidate.check_out = check_out
        candidate.source = Attendance.Source.MANUAL
        candidate.notes = str(payload.get('notes') or '').strip()
        candidate.created_by = actor
        candidate.branch_id = employee.branch_id
        _recalculate_attendance(candidate)
        try:
            candidate.save()
        except IntegrityError:
            return ({'success': False, 'message': 'Attendance already exists; use an adjustment request',
                     'errors': {'code': 'attendance_already_exists'}}, 409)
        record_event(candidate, 'MANUAL_ENTRY', actor, new=candidate.status)
        candidate = Attendance.objects.select_related('employee__user', 'created_by').get(pk=candidate.pk)
        return ServiceResponse.created(data={'attendance': _attendance_payload(candidate, detail=True)})

    @staticmethod
    def serialize_adjustment(row):
        return {
            'id': row.id, 'attendance_id': row.attendance_id,
            'original_check_in': _iso(row.original_check_in),
            'original_check_out': _iso(row.original_check_out),
            'requested_check_in': _iso(row.requested_check_in),
            'requested_check_out': _iso(row.requested_check_out),
            'reason_category': row.reason_category, 'reason_text': row.reason_text,
            'status': row.status, 'requested_by': _actor(row.requested_by),
            'requested_at': _iso(row.requested_at), 'reviewed_by': _actor(row.reviewed_by),
            'reviewed_at': _iso(row.reviewed_at), 'review_note': row.review_note,
        }

    @classmethod
    @transaction.atomic
    def request_adjustment(cls, attendance_id, payload, actor):
        attendance = Attendance.objects.select_for_update().filter(
            id=attendance_id, is_deleted=False,
        ).first()
        if not attendance:
            return ServiceResponse.not_found('Attendance not found')
        try:
            proposed_in = _parse_local_datetime(payload.get('requested_check_in'), 'requested_check_in', allow_none=True)
            proposed_out = _parse_local_datetime(payload.get('requested_check_out'), 'requested_check_out', allow_none=True)
        except ValueError as exc:
            return ServiceResponse.validation_error(errors={'datetime': str(exc)})
        if proposed_in is None and proposed_out is None:
            return ServiceResponse.validation_error(errors={'requested_time': 'One proposed timestamp is required'})
        final_in = proposed_in if proposed_in is not None else attendance.check_in
        final_out = proposed_out if proposed_out is not None else attendance.check_out
        errors = _validate_attendance_times(attendance.date, final_in, final_out, attendance.scheduled_end)
        category = payload.get('reason_category')
        if category not in AttendanceAdjustmentRequest.ReasonCategory.values:
            errors['reason_category'] = 'Invalid category'
        reason = str(payload.get('reason_text') or '').strip()
        if category == AttendanceAdjustmentRequest.ReasonCategory.OTHER and not reason:
            errors['reason_text'] = 'Required for OTHER'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        if attendance.adjustment_requests.filter(status=AttendanceAdjustmentRequest.Status.PENDING, is_deleted=False).exists():
            return ({'success': False, 'message': 'A pending adjustment already exists',
                     'errors': {'code': 'pending_adjustment_exists'}}, 409)
        row = AttendanceAdjustmentRequest.objects.create(
            attendance=attendance, original_check_in=attendance.check_in,
            original_check_out=attendance.check_out, requested_check_in=proposed_in,
            requested_check_out=proposed_out, reason_category=category,
            reason_text=reason, requested_by=actor, branch_id=attendance.branch_id,
        )
        record_event(row, 'REQUEST', actor, new=row.status, reason=reason)
        return ServiceResponse.created(data={'adjustment_request': cls.serialize_adjustment(row)})

    @classmethod
    @transaction.atomic
    def review_adjustment(cls, request_id, actor, approve, note):
        row = AttendanceAdjustmentRequest.objects.select_for_update().select_related(
            'attendance__employee__user', 'requested_by', 'reviewed_by',
        ).filter(id=request_id, is_deleted=False).first()
        if not row:
            return ServiceResponse.not_found('Attendance adjustment not found')
        if row.status != AttendanceAdjustmentRequest.Status.PENDING:
            return ({'success': False, 'message': 'Adjustment already reviewed',
                     'errors': {'code': 'adjustment_already_reviewed'}}, 409)
        if row.requested_by_id == actor.id:
            return ({'success': False, 'message': 'You cannot approve your own adjustment request',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        note = str(note or '').strip()
        if not approve and not note:
            return ServiceResponse.validation_error(errors={'review_note': 'Required for rejection'})
        previous = row.status
        row.reviewed_by = actor
        row.reviewed_at = timezone.now()
        row.review_note = note
        if approve:
            attendance = Attendance.objects.select_for_update().get(pk=row.attendance_id)
            attendance.check_in = row.requested_check_in if row.requested_check_in is not None else attendance.check_in
            attendance.check_out = row.requested_check_out if row.requested_check_out is not None else attendance.check_out
            errors = _validate_attendance_times(
                attendance.date, attendance.check_in, attendance.check_out, attendance.scheduled_end,
            )
            if errors:
                return ServiceResponse.validation_error(errors=errors)
            _recalculate_attendance(attendance)
            attendance.save(update_fields=[
                'check_in', 'check_out', 'worked_minutes', 'work_hours',
                'overtime_minutes', 'overtime_hours', 'late_minutes',
                'early_leave_minutes', 'status', 'updated_at',
            ])
            row.status = AttendanceAdjustmentRequest.Status.APPROVED
            record_event(attendance, 'ADJUST', actor, previous='RECORDED', new='ADJUSTED', reason=note,
                         metadata={'adjustment_request_id': row.id})
        else:
            row.status = AttendanceAdjustmentRequest.Status.REJECTED
        row.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note', 'updated_at'])
        record_event(row, 'APPROVE' if approve else 'REJECT', actor,
                     previous=previous, new=row.status, reason=note)
        return ServiceResponse.success(data={'adjustment_request': cls.serialize_adjustment(row)})

    @staticmethod
    def serialize_excuse(row):
        return {
            'id': row.id, 'attendance_id': row.attendance_id,
            'employee_id': row.employee_id, 'category': row.category,
            'description': row.description, 'status': row.status,
            'excused_late_minutes': row.excused_late_minutes,
            'excused_early_leave_minutes': row.excused_early_leave_minutes,
            'excused_absence_minutes': row.excused_absence_minutes,
            'submitted_by': _actor(row.submitted_by), 'submitted_at': _iso(row.submitted_at),
            'reviewer': _actor(row.reviewer), 'reviewed_at': _iso(row.reviewed_at),
            'review_note': row.review_note,
        }

    @classmethod
    @transaction.atomic
    def create_excuse(cls, attendance_id, payload, actor):
        attendance = Attendance.objects.select_for_update().filter(id=attendance_id, is_deleted=False).first()
        if not attendance:
            return ServiceResponse.not_found('Attendance not found')
        category = payload.get('category')
        description = str(payload.get('description') or '').strip()
        errors = {}
        if category not in AttendanceExcuse.Category.values:
            errors['category'] = 'Invalid category'
        if category == AttendanceExcuse.Category.OTHER and not description:
            errors['description'] = 'Required for OTHER'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        row = AttendanceExcuse.objects.create(
            attendance=attendance, employee=attendance.employee,
            submitted_by=actor, category=category, description=description,
            branch_id=attendance.branch_id,
        )
        record_event(row, 'SUBMIT', actor, new=row.status, reason=description)
        return ServiceResponse.created(data={'excuse': cls.serialize_excuse(row)})

    @classmethod
    @transaction.atomic
    def review_excuse(cls, excuse_id, payload, actor, approve):
        row = AttendanceExcuse.objects.select_for_update().select_related(
            'attendance', 'submitted_by', 'reviewer',
        ).filter(id=excuse_id, is_deleted=False).first()
        if not row:
            return ServiceResponse.not_found('Attendance excuse not found')
        if row.status != AttendanceExcuse.Status.PENDING:
            return ({'success': False, 'message': 'Excuse already reviewed',
                     'errors': {'code': 'excuse_already_reviewed'}}, 409)
        if row.submitted_by_id == actor.id:
            return ({'success': False, 'message': 'You cannot approve your own excuse',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        note = str(payload.get('review_note') or '').strip()
        if not approve and not note:
            return ServiceResponse.validation_error(errors={'review_note': 'Required for rejection'})
        if approve:
            try:
                row.excused_late_minutes = min(
                    row.attendance.late_minutes,
                    int(payload.get('excused_late_minutes', row.attendance.late_minutes)),
                )
                row.excused_early_leave_minutes = min(
                    row.attendance.early_leave_minutes,
                    int(payload.get('excused_early_leave_minutes', row.attendance.early_leave_minutes)),
                )
                row.excused_absence_minutes = max(0, int(payload.get(
                    'excused_absence_minutes', row.attendance.scheduled_minutes
                    if row.attendance.status == Attendance.Status.ABSENT else 0,
                )))
            except (TypeError, ValueError):
                return ServiceResponse.validation_error(errors={'excused_minutes': 'Must be integers'})
            row.status = AttendanceExcuse.Status.APPROVED
        else:
            row.status = AttendanceExcuse.Status.REJECTED
        row.reviewer = actor
        row.reviewed_at = timezone.now()
        row.review_note = note
        row.save(update_fields=[
            'status', 'excused_late_minutes', 'excused_early_leave_minutes',
            'excused_absence_minutes', 'reviewer', 'reviewed_at',
            'review_note', 'updated_at',
        ])
        record_event(row, 'APPROVE' if approve else 'REJECT', actor,
                     previous='PENDING', new=row.status, reason=note)
        return ServiceResponse.success(data={'excuse': cls.serialize_excuse(row)})

    @staticmethod
    def summary(*, date_from, date_to, page=1, per_page=20, employee_id=None,
                branch_id=None, attendance_status=None, rule_category=None,
                penalty_status=None):
        employees = Employee.objects.filter(is_deleted=False).select_related('user')
        if employee_id:
            employees = employees.filter(id=employee_id)
        if branch_id:
            employees = employees.filter(user__branch_id=branch_id)
        rows, pagination = _page(employees.order_by('user__first_name', 'user__last_name'), page, per_page)
        output = []
        filtered_totals = {
            'scheduled_minutes': 0, 'worked_minutes': 0, 'overtime_minutes': 0,
            'late_minutes': 0, 'early_leave_minutes': 0, 'absences': 0,
            'approved_penalty_total_uzs': 0, 'pending_penalty_total_uzs': 0,
        }
        for employee in rows:
            attendance_qs = Attendance.objects.filter(
                employee=employee, date__range=(date_from, date_to), is_deleted=False,
            ).prefetch_related('excuses')
            if attendance_status:
                attendance_qs = attendance_qs.filter(status=attendance_status)
            attendance = list(attendance_qs)
            by_date = {row.date: row for row in attendance}
            scheduled_total = worked = overtime = late = early = absences = 0
            cursor = date_from
            while cursor <= date_to:
                record = by_date.get(cursor)
                if record:
                    scheduled_total += record.scheduled_minutes
                    worked += record.worked_minutes
                    overtime += record.overtime_minutes
                    late += record.late_minutes
                    early += record.early_leave_minutes
                    if record.status == Attendance.Status.ABSENT:
                        absences += 1
                else:
                    schedule = _schedule_for(employee.id, cursor)
                    if schedule:
                        start = datetime.combine(cursor, schedule.scheduled_start_local, LOCAL_TZ)
                        end = datetime.combine(cursor + timedelta(days=1 if schedule.is_overnight else 0), schedule.scheduled_end_local, LOCAL_TZ)
                        scheduled_total += max(0, int((end - start).total_seconds() // 60))
                        absences += 1
                cursor += timedelta(days=1)
            excuses = AttendanceExcuse.objects.filter(
                employee=employee, attendance__date__range=(date_from, date_to), is_deleted=False,
            )
            cases = DisciplinaryCase.objects.filter(
                employee=employee, business_date__range=(date_from, date_to),
            )
            if rule_category:
                cases = cases.filter(rule_category_snapshot=rule_category)
            if penalty_status:
                cases = cases.filter(status=penalty_status)
            case_status = {key: 0 for key, _ in DisciplinaryCase.Status.choices}
            for status_name in cases.values_list('status', flat=True):
                case_status[status_name] = case_status.get(status_name, 0) + 1
            approved = cases.filter(status=DisciplinaryCase.Status.APPROVED).aggregate(v=Sum('amount_uzs'))['v'] or 0
            pending = cases.filter(status__in=[
                DisciplinaryCase.Status.DRAFT, DisciplinaryCase.Status.SUBMITTED,
                DisciplinaryCase.Status.APPROVED_PENDING_PAYROLL,
            ]).aggregate(v=Sum('amount_uzs'))['v'] or 0
            excuse_counts = {status_name: excuses.filter(status=status_name).count()
                              for status_name, _ in AttendanceExcuse.Status.choices}
            item = {
                'employee': {'id': employee.id,
                             'full_name': f'{employee.user.first_name} {employee.user.last_name}'.strip(),
                             'is_active': employee.is_active},
                'scheduled_minutes': scheduled_total, 'worked_minutes': worked,
                'overtime_minutes': overtime, 'late_minutes': late,
                'early_leave_minutes': early, 'absences': absences,
                'excuses': excuse_counts, 'penalties': case_status,
                'approved_penalty_total_uzs': int(approved),
                'pending_penalty_total_uzs': int(pending),
            }
            output.append(item)
            for key in ('scheduled_minutes', 'worked_minutes', 'overtime_minutes',
                        'late_minutes', 'early_leave_minutes', 'absences'):
                filtered_totals[key] += item[key]
            filtered_totals['approved_penalty_total_uzs'] += int(approved)
            filtered_totals['pending_penalty_total_uzs'] += int(pending)
        return ServiceResponse.success(data={
            'employees': output, 'pagination': pagination, 'totals': filtered_totals,
            'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
            'timezone': 'Asia/Tashkent',
        })


def _rule_payload(row):
    return {
        'id': row.id, 'code': row.code, 'category': row.category,
        'title': row.title, 'description': row.description,
        'default_amount_uzs': row.default_amount_uzs,
        'is_active': row.is_active, 'effective_from': row.effective_from.isoformat(),
        'effective_to': row.effective_to.isoformat() if row.effective_to else None,
        'requires_evidence': row.requires_evidence,
        'requires_comment': row.requires_comment,
        'created_by': _actor(row.created_by), 'updated_by': _actor(row.updated_by),
        'created_at': _iso(row.created_at), 'updated_at': _iso(row.updated_at),
    }


def _case_payload(row, detail=False):
    data = {
        'id': row.id, 'employee': {
            'id': row.employee_id,
            'full_name': f'{row.employee.user.first_name} {row.employee.user.last_name}'.strip(),
        },
        'occurred_at': _iso(row.occurred_at), 'business_date': row.business_date.isoformat(),
        'rule_id': row.rule_id, 'rule_snapshot': {
            'code': row.rule_code_snapshot, 'title': row.rule_title_snapshot,
            'category': row.rule_category_snapshot,
            'default_amount_uzs': row.rule_amount_snapshot_uzs,
        },
        'amount_uzs': row.amount_uzs, 'evidence': row.evidence,
        'comment': row.comment, 'attendance_id': row.attendance_id,
        'attendance_excuse_id': row.attendance_excuse_id,
        'preparation_audit_id': row.preparation_audit_id,
        'excuse': {'text': row.excuse_text, 'review_state': row.excuse_review_state},
        'status': row.status, 'created_by': _actor(row.created_by),
        'created_at': _iso(row.created_at), 'reviewed_by': _actor(row.reviewed_by),
        'reviewed_at': _iso(row.reviewed_at), 'review_note': row.review_note,
        'void_reason': row.void_reason,
        'payroll_period': ({'year': row.payroll_period_year, 'month': row.payroll_period_month}
                           if row.payroll_period_year else None),
        'salary_deduction_id': row.salary_deduction_id,
    }
    if detail:
        data['history'] = event_history(row)
    return data


class DisciplineService:
    @staticmethod
    def list_rules(*, page=1, per_page=20, active=None, category=None):
        qs = DisciplinaryRule.objects.select_related('created_by', 'updated_by')
        if active is not None:
            qs = qs.filter(is_active=active)
        if category:
            qs = qs.filter(category=category)
        rows, pagination = _page(qs, page, per_page)
        return ServiceResponse.success(data={
            'discipline_rules': [_rule_payload(row) for row in rows],
            'pagination': pagination,
        })

    @staticmethod
    @transaction.atomic
    def create_rule(payload, actor):
        errors = {}
        code = str(payload.get('code') or '').strip().upper()
        category = payload.get('category')
        title = str(payload.get('title') or '').strip()
        description = str(payload.get('description') or '').strip()
        if not code:
            errors['code'] = 'Required'
        if category not in DisciplinaryRule.Category.values:
            errors['category'] = 'Invalid category'
        if not title:
            errors['title'] = 'Required'
        if not description:
            errors['description'] = 'Required'
        try:
            amount = int(payload.get('default_amount_uzs', 0))
            if amount < 0 or isinstance(payload.get('default_amount_uzs'), bool):
                raise ValueError
        except (TypeError, ValueError):
            amount = 0
            errors['default_amount_uzs'] = 'Must be a non-negative whole UZS integer'
        try:
            effective_from = _parse_date(payload.get('effective_from'), 'effective_from')
            effective_to = _parse_date(payload.get('effective_to'), 'effective_to') if payload.get('effective_to') else None
        except ValueError as exc:
            effective_from = effective_to = None
            errors['effective_range'] = str(exc)
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        if DisciplinaryRule.objects.filter(code=code).exists():
            return ({'success': False, 'message': 'Rule code already exists',
                     'errors': {'code': 'immutable_unique_code'}}, 409)
        row = DisciplinaryRule.objects.create(
            code=code, category=category, title=title, description=description,
            default_amount_uzs=amount, is_active=bool(payload.get('is_active', True)),
            effective_from=effective_from, effective_to=effective_to,
            requires_evidence=bool(payload.get('requires_evidence', amount > 0)),
            requires_comment=bool(payload.get('requires_comment', amount > 0)),
            created_by=actor, updated_by=actor,
        )
        record_event(row, 'CREATE', actor, new='ACTIVE' if row.is_active else 'INACTIVE')
        return ServiceResponse.created(data={'discipline_rule': _rule_payload(row)})

    @staticmethod
    @transaction.atomic
    def patch_rule(rule_id, payload, actor):
        row = DisciplinaryRule.objects.select_for_update().filter(id=rule_id).first()
        if not row:
            return ServiceResponse.not_found('Disciplinary rule not found')
        if 'code' in payload and str(payload['code']).strip().upper() != row.code:
            return ServiceResponse.validation_error(errors={'code': 'Rule code is immutable'})
        if row.effective_from <= timezone.localdate() and any(
            key in payload for key in ('category', 'title', 'description', 'default_amount_uzs')
        ):
            requested_effective = payload.get('effective_from')
            if not requested_effective or _parse_date(requested_effective) <= timezone.localdate():
                return ServiceResponse.validation_error(errors={
                    'effective_from': 'Policy text/amount changes must be prospective',
                })
        for field in ('category', 'title', 'description', 'is_active',
                      'requires_evidence', 'requires_comment'):
            if field in payload:
                setattr(row, field, payload[field])
        if 'default_amount_uzs' in payload:
            try:
                amount = int(payload['default_amount_uzs'])
                if amount < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return ServiceResponse.validation_error(errors={'default_amount_uzs': 'Must be a whole UZS integer'})
            row.default_amount_uzs = amount
        for field in ('effective_from', 'effective_to'):
            if field in payload:
                setattr(row, field, _parse_date(payload[field], field) if payload[field] else None)
        row.updated_by = actor
        row.save()
        record_event(row, 'UPDATE', actor, previous='CONFIGURED', new='CONFIGURED')
        return ServiceResponse.success(data={'discipline_rule': _rule_payload(row)})

    @staticmethod
    def list_cases(*, page=1, per_page=20, employee_id=None, status=None,
                   date_from=None, date_to=None, category=None):
        qs = DisciplinaryCase.objects.select_related(
            'employee__user', 'rule', 'created_by', 'reviewed_by', 'salary_deduction',
        )
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(business_date__gte=date_from)
        if date_to:
            qs = qs.filter(business_date__lte=date_to)
        if category:
            qs = qs.filter(rule_category_snapshot=category)
        rows, pagination = _page(qs, page, per_page)
        return ServiceResponse.success(data={
            'discipline_cases': [_case_payload(row) for row in rows],
            'pagination': pagination,
        })

    @staticmethod
    @transaction.atomic
    def create_case(payload, actor):
        errors = {}
        try:
            employee = Employee.objects.get(id=payload.get('employee_id'), is_deleted=False)
        except Employee.DoesNotExist:
            employee = None
            errors['employee_id'] = 'Employee not found'
        try:
            rule = DisciplinaryRule.objects.get(id=payload.get('rule_id'))
        except DisciplinaryRule.DoesNotExist:
            rule = None
            errors['rule_id'] = 'Rule not found'
        try:
            occurred_at = _parse_local_datetime(payload.get('occurred_at'), 'occurred_at')
            business_date = _parse_date(payload.get('business_date'), 'business_date')
        except ValueError as exc:
            occurred_at = business_date = None
            errors['occurred_at'] = str(exc)
        evidence = str(payload.get('evidence') or '').strip()
        comment = str(payload.get('comment') or '').strip()
        status = payload.get('status', DisciplinaryCase.Status.DRAFT)
        if status not in (DisciplinaryCase.Status.DRAFT, DisciplinaryCase.Status.SUBMITTED):
            errors['status'] = 'Must be DRAFT or SUBMITTED'
        if rule and status == DisciplinaryCase.Status.SUBMITTED:
            if rule.requires_evidence and not evidence:
                errors['evidence'] = 'Required by this rule'
            if rule.requires_comment and not comment:
                errors['comment'] = 'Required by this rule'
        try:
            amount = int(payload.get('amount_uzs', rule.default_amount_uzs if rule else 0))
            if amount < 0 or isinstance(payload.get('amount_uzs'), bool):
                raise ValueError
        except (TypeError, ValueError):
            amount = 0
            errors['amount_uzs'] = 'Must be a non-negative whole UZS integer'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        if not rule.is_active or business_date < rule.effective_from or (rule.effective_to and business_date > rule.effective_to):
            return ServiceResponse.validation_error(errors={'rule_id': 'Rule was not effective on business_date'})
        attendance = Attendance.objects.filter(id=payload.get('attendance_id'), employee=employee).first() if payload.get('attendance_id') else None
        excuse = AttendanceExcuse.objects.filter(id=payload.get('attendance_excuse_id'), attendance=attendance).first() if payload.get('attendance_excuse_id') else None
        prep = PreparationAudit.objects.filter(id=payload.get('preparation_audit_id')).first() if payload.get('preparation_audit_id') else None
        row = DisciplinaryCase.objects.create(
            employee=employee, occurred_at=occurred_at, business_date=business_date,
            rule=rule, rule_code_snapshot=rule.code, rule_title_snapshot=rule.title,
            rule_category_snapshot=rule.category,
            rule_amount_snapshot_uzs=rule.default_amount_uzs, amount_uzs=amount,
            evidence=evidence, comment=comment, attendance=attendance,
            attendance_excuse=excuse, preparation_audit=prep,
            excuse_text=str(payload.get('excuse_text') or '').strip(),
            excuse_review_state=str(payload.get('excuse_review_state') or '').strip(),
            status=status, created_by=actor,
            payroll_period_year=payload.get('payroll_period_year'),
            payroll_period_month=payload.get('payroll_period_month'),
        )
        record_event(row, 'CREATE', actor, new=row.status)
        return ServiceResponse.created(data={'discipline_case': _case_payload(row, detail=True)})

    @staticmethod
    def get_case(case_id):
        row = DisciplinaryCase.objects.select_related(
            'employee__user', 'rule', 'created_by', 'reviewed_by', 'salary_deduction',
        ).filter(id=case_id).first()
        if not row:
            return ServiceResponse.not_found('Disciplinary case not found')
        return ServiceResponse.success(data={'discipline_case': _case_payload(row, detail=True)})

    @classmethod
    @transaction.atomic
    def approve_case(cls, case_id, actor, note=''):
        row = DisciplinaryCase.objects.select_for_update().select_related(
            'employee__user', 'rule', 'created_by', 'reviewed_by',
            'attendance_excuse', 'salary_deduction',
        ).filter(id=case_id).first()
        if not row:
            return ServiceResponse.not_found('Disciplinary case not found')
        if row.status in (DisciplinaryCase.Status.APPROVED,
                          DisciplinaryCase.Status.APPROVED_PENDING_PAYROLL):
            return ServiceResponse.success(data={'discipline_case': _case_payload(row, detail=True)})
        if row.status != DisciplinaryCase.Status.SUBMITTED:
            return ({'success': False, 'message': 'Only submitted cases can be approved',
                     'errors': {'code': 'invalid_case_transition'}}, 409)
        if row.created_by_id == actor.id:
            return ({'success': False, 'message': 'You cannot approve your own disciplinary case',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        if row.rule.requires_evidence and not row.evidence.strip():
            return ServiceResponse.validation_error(errors={'evidence': 'Required by rule'})
        if row.rule.requires_comment and not row.comment.strip():
            return ServiceResponse.validation_error(errors={'comment': 'Required by rule'})
        approved_excuse = None
        if row.attendance_id:
            approved_excuse = AttendanceExcuse.objects.filter(
                attendance_id=row.attendance_id, status=AttendanceExcuse.Status.APPROVED,
                is_deleted=False,
            ).first()
        if approved_excuse and row.amount_uzs:
            return ({'success': False,
                     'message': 'Approved excuse conflicts with this attendance penalty',
                     'errors': {'code': 'approved_excuse_conflict', 'excuse_id': approved_excuse.id}}, 409)
        year = row.payroll_period_year or row.business_date.year
        month = row.payroll_period_month or row.business_date.month
        row.payroll_period_year = year
        row.payroll_period_month = month
        salary = SalaryPayment.objects.select_for_update().filter(
            employee=row.employee, period_year=year, period_month=month,
            is_deleted=False,
        ).first()
        if salary and salary.status == SalaryPayment.Status.PAID:
            return ({'success': False, 'message': 'Paid salary cannot be changed',
                     'errors': {'code': 'salary_already_paid'}}, 409)
        if row.amount_uzs and salary:
            deduction = SalaryDeduction.objects.create(
                salary=salary, amount=Decimal(row.amount_uzs),
                reason=f'{row.rule_code_snapshot}: {row.rule_title_snapshot}',
                branch_id=salary.branch_id,
            )
            row.salary_deduction = deduction
            from hr.services.salary_item_service import SalaryItemService
            SalaryItemService._recompute(salary)
            row.status = DisciplinaryCase.Status.APPROVED
        elif row.amount_uzs:
            row.status = DisciplinaryCase.Status.APPROVED_PENDING_PAYROLL
        else:
            row.status = DisciplinaryCase.Status.APPROVED
        row.reviewed_by = actor
        row.reviewed_at = timezone.now()
        row.review_note = str(note or '').strip()
        row.save()
        record_event(row, 'APPROVE', actor, previous='SUBMITTED', new=row.status, reason=row.review_note)
        return ServiceResponse.success(data={'discipline_case': _case_payload(row, detail=True)})

    @staticmethod
    @transaction.atomic
    def reject_case(case_id, actor, reason):
        row = DisciplinaryCase.objects.select_for_update().select_related(
            'employee__user', 'rule', 'created_by', 'reviewed_by',
        ).filter(id=case_id).first()
        if not row:
            return ServiceResponse.not_found('Disciplinary case not found')
        if row.created_by_id == actor.id:
            return ({'success': False, 'message': 'You cannot reject your own disciplinary case',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        reason = str(reason or '').strip()
        if not reason:
            return ServiceResponse.validation_error(errors={'reason': 'Required'})
        if row.status != DisciplinaryCase.Status.SUBMITTED:
            return ({'success': False, 'message': 'Only submitted cases can be rejected',
                     'errors': {'code': 'invalid_case_transition'}}, 409)
        row.status = DisciplinaryCase.Status.REJECTED
        row.reviewed_by = actor
        row.reviewed_at = timezone.now()
        row.review_note = reason
        row.save()
        record_event(row, 'REJECT', actor, previous='SUBMITTED', new=row.status, reason=reason)
        return ServiceResponse.success(data={'discipline_case': _case_payload(row, detail=True)})

    @staticmethod
    @transaction.atomic
    def void_case(case_id, actor, reason):
        row = DisciplinaryCase.objects.select_for_update().select_related(
            'employee__user', 'rule', 'created_by', 'reviewed_by', 'salary_deduction__salary',
        ).filter(id=case_id).first()
        if not row:
            return ServiceResponse.not_found('Disciplinary case not found')
        if row.created_by_id == actor.id:
            return ({'success': False, 'message': 'You cannot void your own disciplinary case',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        reason = str(reason or '').strip()
        if not reason:
            return ServiceResponse.validation_error(errors={'reason': 'Required'})
        if row.status not in (DisciplinaryCase.Status.APPROVED,
                              DisciplinaryCase.Status.APPROVED_PENDING_PAYROLL):
            return ({'success': False, 'message': 'Only approved cases can be voided',
                     'errors': {'code': 'invalid_case_transition'}}, 409)
        previous = row.status
        if row.salary_deduction_id:
            salary = SalaryPayment.objects.select_for_update().get(pk=row.salary_deduction.salary_id)
            if salary.status == SalaryPayment.Status.PAID:
                return ({'success': False, 'message': 'Paid salary cannot be changed',
                         'errors': {'code': 'salary_already_paid'}}, 409)
            row.salary_deduction.delete()
            from hr.services.salary_item_service import SalaryItemService
            SalaryItemService._recompute(salary)
        row.status = DisciplinaryCase.Status.VOIDED
        row.void_reason = reason
        row.reviewed_by = actor
        row.reviewed_at = timezone.now()
        row.save()
        record_event(row, 'VOID', actor, previous=previous, new=row.status, reason=reason)
        return ServiceResponse.success(data={'discipline_case': _case_payload(row, detail=True)})

    @staticmethod
    @transaction.atomic
    def attach_pending_for_salary(salary):
        if salary.status == SalaryPayment.Status.PAID:
            return 0
        cases = DisciplinaryCase.objects.select_for_update().filter(
            employee=salary.employee,
            payroll_period_year=salary.period_year,
            payroll_period_month=salary.period_month,
            status=DisciplinaryCase.Status.APPROVED_PENDING_PAYROLL,
            salary_deduction__isnull=True,
        )
        attached = 0
        for row in cases:
            deduction = SalaryDeduction.objects.create(
                salary=salary, amount=Decimal(row.amount_uzs),
                reason=f'{row.rule_code_snapshot}: {row.rule_title_snapshot}',
                branch_id=salary.branch_id,
            )
            row.salary_deduction = deduction
            row.status = DisciplinaryCase.Status.APPROVED
            row.save(update_fields=['salary_deduction', 'status', 'updated_at'])
            attached += 1
        if attached:
            from hr.services.salary_item_service import SalaryItemService
            SalaryItemService._recompute(salary)
        return attached


def ensure_preparation_audit(order_id):
    order = Order.objects.filter(id=order_id).first()
    if not order or not order.created_at or not order.ready_at:
        return None
    elapsed = int((order.ready_at - order.created_at).total_seconds())
    if elapsed < 0:
        return None
    from notifications.preparation import classify_preparation, preparation_target_for_order
    names = order.items.filter(is_deleted=False, product_id__isnull=False).values_list('product__name', flat=True)
    target = preparation_target_for_order(names)
    if target is None:
        performance = PreparationAudit.PerformanceStatus.UNTRACKED
        review_required = False
        target_seconds = None
        target_name = ''
    else:
        performance = classify_preparation(elapsed, target).key
        review_required = performance in (
            PreparationAudit.PerformanceStatus.SLIGHTLY_LATE,
            PreparationAudit.PerformanceStatus.VERY_LATE,
        )
        target_seconds = target.maximum_seconds
        target_name = target.display
    defaults = {
        'branch_id': order.branch_id or '',
        'created_at_snapshot': order.created_at,
        'ready_at_snapshot': order.ready_at,
        'elapsed_seconds': elapsed,
        'target_seconds': target_seconds,
        'target_name_snapshot': target_name,
        'performance_status': performance,
        'review_required': review_required,
        'review_status': (PreparationAudit.ReviewStatus.PENDING if review_required
                          else PreparationAudit.ReviewStatus.NOT_REQUIRED),
    }
    try:
        with transaction.atomic():
            audit, _created = PreparationAudit.objects.get_or_create(order=order, defaults=defaults)
            return audit
    except IntegrityError:
        return PreparationAudit.objects.filter(order=order).first()


def _prep_review_payload(row):
    return {
        'id': row.id, 'category': {'id': row.category_id, 'code': row.category.code,
                                    'name': row.category.name},
        'comment': row.comment,
        'responsible_employee_id': row.responsible_employee_id,
        'disciplinary_case_id': row.linked_disciplinary_case_id,
        'reviewed_by': _actor(row.reviewed_by), 'reviewed_at': _iso(row.reviewed_at),
        'is_current': row.is_current, 'reopened_by': _actor(row.reopened_by),
        'reopened_at': _iso(row.reopened_at), 'reopen_reason': row.reopen_reason,
    }


def _prep_payload(row, detail=False):
    current = next((x for x in row.reviews.all() if x.is_current), None)
    data = {
        'id': row.id, 'order': {'id': row.order_id, 'display_id': row.order.display_id,
                                'cashier_id': row.order.cashier_id},
        'branch_id': row.branch_id, 'created_at_snapshot': _iso(row.created_at_snapshot),
        'ready_at_snapshot': _iso(row.ready_at_snapshot),
        'elapsed_seconds': row.elapsed_seconds, 'target_seconds': row.target_seconds,
        'target_name_snapshot': row.target_name_snapshot,
        'performance_status': row.performance_status,
        'review_required': row.review_required, 'review_status': row.review_status,
        'review': _prep_review_payload(current) if current else None,
        'reviewer': _actor(row.reviewer), 'reviewed_at': _iso(row.reviewed_at),
        'timezone': 'Asia/Tashkent',
    }
    if detail:
        data['review_history'] = [_prep_review_payload(x) for x in row.reviews.all()]
        data['history'] = event_history(row)
    return data


class PreparationAuditService:
    @staticmethod
    def list(*, page=1, per_page=20, date_from=None, date_to=None,
             branch_id=None, performance_status=None, review_status=None,
             category_id=None, cashier_id=None, responsible_employee_id=None):
        qs = PreparationAudit.objects.select_related('order__cashier', 'reviewer').prefetch_related(
            'reviews__category', 'reviews__reviewed_by', 'reviews__responsible_employee',
            'reviews__linked_disciplinary_case',
        )
        if date_from:
            qs = qs.filter(ready_at_snapshot__date__gte=date_from)
        if date_to:
            qs = qs.filter(ready_at_snapshot__date__lte=date_to)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if performance_status:
            qs = qs.filter(performance_status=performance_status)
        if review_status:
            qs = qs.filter(review_status=review_status)
        if category_id:
            qs = qs.filter(reviews__category_id=category_id, reviews__is_current=True)
        if cashier_id:
            qs = qs.filter(order__cashier_id=cashier_id)
        if responsible_employee_id:
            qs = qs.filter(reviews__responsible_employee_id=responsible_employee_id,
                           reviews__is_current=True)
        rows, pagination = _page(qs.distinct(), page, per_page)
        return ServiceResponse.success(data={
            'preparation_audits': [_prep_payload(row) for row in rows],
            'pagination': pagination, 'timezone': 'Asia/Tashkent',
        })

    @staticmethod
    def detail(audit_id):
        row = PreparationAudit.objects.select_related('order__cashier', 'reviewer').prefetch_related(
            'reviews__category', 'reviews__reviewed_by', 'reviews__reopened_by',
        ).filter(id=audit_id).first()
        if not row:
            return ServiceResponse.not_found('Preparation audit not found')
        return ServiceResponse.success(data={'preparation_audit': _prep_payload(row, detail=True)})

    @staticmethod
    @transaction.atomic
    def review(audit_id, payload, actor):
        row = PreparationAudit.objects.select_for_update().select_related('order__cashier').filter(id=audit_id).first()
        if not row:
            return ServiceResponse.not_found('Preparation audit not found')
        current = PreparationAuditReview.objects.select_for_update().select_related(
            'category', 'reviewed_by',
        ).filter(preparation_audit=row, is_current=True).first()
        if current:
            return ServiceResponse.success(data={'review': _prep_review_payload(current)},
                                           message='Review already completed')
        if not row.review_required:
            return ({'success': False, 'message': 'This preparation audit does not require review',
                     'errors': {'code': 'review_not_required'}}, 409)
        category = PreparationAuditCategory.objects.filter(
            id=payload.get('category_id'), is_active=True,
        ).first()
        comment = str(payload.get('comment') or '').strip()
        errors = {}
        if not category:
            errors['category_id'] = 'Active category not found'
        if not 10 <= len(comment) <= 1000:
            errors['comment'] = 'Must contain 10 to 1,000 characters'
        if category and category.code == 'OTHER' and not comment:
            errors['comment'] = 'Must explain the OTHER reason'
        responsible = None
        if payload.get('responsible_employee_id'):
            responsible = Employee.objects.filter(id=payload['responsible_employee_id'], is_deleted=False).first()
            if not responsible:
                errors['responsible_employee_id'] = 'Employee not found'
        case = None
        if payload.get('disciplinary_case_id'):
            case = DisciplinaryCase.objects.filter(id=payload['disciplinary_case_id']).first()
            if not case:
                errors['disciplinary_case_id'] = 'Disciplinary case not found'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        review = PreparationAuditReview.objects.create(
            preparation_audit=row, category=category, comment=comment,
            responsible_employee=responsible, linked_disciplinary_case=case,
            reviewed_by=actor,
        )
        row.review_status = PreparationAudit.ReviewStatus.COMPLETED
        row.reviewer = actor
        row.reviewed_at = review.reviewed_at
        row.save(update_fields=['review_status', 'reviewer', 'reviewed_at'])
        record_event(row, 'REVIEW', actor, previous='PENDING', new='COMPLETED',
                     reason=comment, metadata={'category': category.code})
        return ServiceResponse.created(data={'review': _prep_review_payload(review)})

    @staticmethod
    @transaction.atomic
    def reopen(audit_id, actor, reason):
        row = PreparationAudit.objects.select_for_update().select_related('order__cashier').filter(id=audit_id).first()
        if not row:
            return ServiceResponse.not_found('Preparation audit not found')
        reason = str(reason or '').strip()
        if not reason:
            return ServiceResponse.validation_error(errors={'reason': 'Required'})
        if row.review_status not in (PreparationAudit.ReviewStatus.COMPLETED,
                                     PreparationAudit.ReviewStatus.EXCUSED):
            return ({'success': False, 'message': 'Only completed reviews can be reopened',
                     'errors': {'code': 'invalid_review_transition'}}, 409)
        review = PreparationAuditReview.objects.select_for_update().filter(
            preparation_audit=row, is_current=True,
        ).first()
        if review:
            review.is_current = False
            review.reopened_by = actor
            review.reopened_at = timezone.now()
            review.reopen_reason = reason
            review.save(update_fields=['is_current', 'reopened_by', 'reopened_at', 'reopen_reason'])
        previous = row.review_status
        row.review_status = PreparationAudit.ReviewStatus.PENDING
        row.reviewer = None
        row.reviewed_at = None
        row.reopened_by = actor
        row.reopened_at = timezone.now()
        row.reopen_reason = reason
        row.save()
        record_event(row, 'REOPEN', actor, previous=previous, new='PENDING', reason=reason)
        return ServiceResponse.success(data={'preparation_audit': _prep_payload(row, detail=True)})

    @staticmethod
    def categories(active_only=True):
        qs = PreparationAuditCategory.objects.all()
        if active_only:
            qs = qs.filter(is_active=True)
        return ServiceResponse.success(data={'categories': [
            {'id': row.id, 'code': row.code, 'name': row.name, 'is_active': row.is_active}
            for row in qs
        ]})

    @staticmethod
    def dashboard(*, date_from, date_to, branch_id=None):
        qs = PreparationAudit.objects.filter(ready_at_snapshot__date__range=(date_from, date_to))
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        counts = {key: qs.filter(performance_status=key).count()
                  for key, _ in PreparationAudit.PerformanceStatus.choices}
        pending_yellow = qs.filter(
            performance_status=PreparationAudit.PerformanceStatus.SLIGHTLY_LATE,
            review_status=PreparationAudit.ReviewStatus.PENDING,
        ).count()
        pending_red = qs.filter(
            performance_status=PreparationAudit.PerformanceStatus.VERY_LATE,
            review_status=PreparationAudit.ReviewStatus.PENDING,
        ).count()
        categories = list(
            PreparationAuditReview.objects.filter(
                preparation_audit__in=qs, is_current=True,
            ).values('category__code').annotate(count=Count('id')).order_by('category__code')
        )
        required = qs.filter(review_required=True).count()
        completed = qs.filter(review_required=True, review_status__in=[
            PreparationAudit.ReviewStatus.COMPLETED, PreparationAudit.ReviewStatus.EXCUSED,
        ]).count()
        return ServiceResponse.success(data={
            'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
            'counts': counts, 'pending_yellow_count': pending_yellow,
            'pending_red_count': pending_red,
            'pending_review_count': pending_yellow + pending_red,
            'categories': categories, 'review_required_count': required,
            'review_completed_count': completed,
            'review_completion_percent': round(completed * 100 / required, 2) if required else 100,
            'timezone': 'Asia/Tashkent',
        })

    @staticmethod
    @transaction.atomic
    def close_period(period_start, period_end, branch_id, actor):
        pending = PreparationAudit.objects.select_for_update().filter(
            ready_at_snapshot__gte=period_start, ready_at_snapshot__lt=period_end,
            review_required=True, review_status=PreparationAudit.ReviewStatus.PENDING,
        )
        if branch_id:
            pending = pending.filter(branch_id=branch_id)
        count = pending.count()
        if count:
            return ({'success': False,
                     'message': 'Pending yellow/red preparation reviews must be completed',
                     'errors': {'code': 'pending_preparation_reviews'},
                     'pending_review_count': count}, 409)
        close, created = PreparationAuditPeriodClose.objects.get_or_create(
            branch_id=branch_id or '', period_start=period_start, period_end=period_end,
            defaults={'closed_by': actor},
        )
        return ServiceResponse.success(data={
            'period_close_id': close.id, 'closed_at': _iso(close.closed_at),
            'already_closed': not created, 'timezone': 'Asia/Tashkent',
        })
