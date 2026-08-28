from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.idempotency import idempotent
from base.security.permissions import (
    backoffice_required, permission_denied_response, user_has_permission,
)
from hr.services.operational_audit_service import (
    AttendanceOperations, DisciplineService, PreparationAuditService,
    WorkScheduleService, _parse_date, _parse_local_datetime,
)


def _payload(request):
    data, error = parse_json_body(request)
    return data, json_response(error) if error else None


def _date_query(request, key, required=False):
    value = request.GET.get(key)
    if not value:
        if required:
            raise ValueError(f'{key} is required')
        return None
    return _parse_date(value, key)


def _or_permission(request, *permissions):
    if any(user_has_permission(request.user, item) for item in permissions):
        return None
    return permission_denied_response(request, permissions[0])


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def work_schedules(request):
    if request.method == 'GET':
        if denied := permission_denied_response(request, 'attendance.view'):
            return denied
        try:
            date_from = _date_query(request, 'date_from')
            date_to = _date_query(request, 'date_to')
        except ValueError as exc:
            return JsonResponse({'success': False, 'message': str(exc)}, status=422)
        result, status = WorkScheduleService.list(
            page=safe_page(request), per_page=safe_per_page(request, 20),
            employee_id=safe_int(request, 'employee_id'),
            date_from=date_from, date_to=date_to,
        )
        return JsonResponse(result, status=status)
    if denied := _or_permission(request, 'attendance.schedule.manage', 'discipline.rule.manage'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = WorkScheduleService.create(data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH'])
@backoffice_required
def work_schedule_detail(request, schedule_id):
    if denied := _or_permission(request, 'attendance.schedule.manage', 'discipline.rule.manage'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = WorkScheduleService.patch(schedule_id, data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def attendance_manual_entry(request):
    if denied := permission_denied_response(request, 'attendance.record'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = AttendanceOperations.manual_entry(data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def attendance_adjustment_request(request, attendance_id):
    if denied := permission_denied_response(request, 'attendance.adjust.request'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = AttendanceOperations.request_adjustment(attendance_id, data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('attendance.adjustment.review', fallback_key_from_request=True)
def attendance_adjustment_review(request, request_id, action):
    if denied := permission_denied_response(request, 'attendance.adjust.approve'):
        return denied
    if action not in ('approve', 'reject'):
        return JsonResponse({'success': False, 'message': 'Unknown action'}, status=404)
    data, error = _payload(request)
    if error:
        return error
    result, status = AttendanceOperations.review_adjustment(
        request_id, request.user, action == 'approve', data.get('review_note'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def attendance_excuse_create(request, attendance_id):
    if denied := permission_denied_response(request, 'attendance.adjust.request'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = AttendanceOperations.create_excuse(attendance_id, data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('attendance.excuse.review', fallback_key_from_request=True)
def attendance_excuse_review(request, excuse_id, action):
    if denied := permission_denied_response(request, 'attendance.adjust.approve'):
        return denied
    if action not in ('approve', 'reject'):
        return JsonResponse({'success': False, 'message': 'Unknown action'}, status=404)
    data, error = _payload(request)
    if error:
        return error
    result, status = AttendanceOperations.review_excuse(
        excuse_id, data, request.user, action == 'approve',
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def attendance_summary(request):
    if denied := permission_denied_response(request, 'attendance.view'):
        return denied
    try:
        date_from = _date_query(request, 'date_from', True)
        date_to = _date_query(request, 'date_to', True)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    if date_to < date_from:
        return JsonResponse({'success': False, 'message': 'date_to must be on or after date_from'}, status=422)
    result, status = AttendanceOperations.summary(
        date_from=date_from, date_to=date_to, page=safe_page(request),
        per_page=safe_per_page(request, 20),
        employee_id=safe_int(request, 'employee_id'),
        branch_id=request.GET.get('branch_id') or request.GET.get('location_id'),
        attendance_status=request.GET.get('attendance_status') or request.GET.get('status'),
        rule_category=request.GET.get('discipline_rule_category'),
        penalty_status=request.GET.get('penalty_status'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def discipline_rules(request):
    if request.method == 'GET':
        if denied := permission_denied_response(request, 'discipline.rule.view'):
            return denied
        active_raw = request.GET.get('active')
        active = None if active_raw is None else active_raw.lower() == 'true'
        result, status = DisciplineService.list_rules(
            page=safe_page(request), per_page=safe_per_page(request, 20),
            active=active, category=request.GET.get('category'),
        )
        return JsonResponse(result, status=status)
    if denied := permission_denied_response(request, 'discipline.rule.manage'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = DisciplineService.create_rule(data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PATCH'])
@backoffice_required
def discipline_rule_detail(request, rule_id):
    if denied := permission_denied_response(request, 'discipline.rule.manage'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    try:
        result, status = DisciplineService.patch_rule(rule_id, data, request.user)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def discipline_cases(request):
    if request.method == 'GET':
        if denied := permission_denied_response(request, 'discipline.case.view'):
            return denied
        try:
            date_from = _date_query(request, 'date_from')
            date_to = _date_query(request, 'date_to')
        except ValueError as exc:
            return JsonResponse({'success': False, 'message': str(exc)}, status=422)
        result, status = DisciplineService.list_cases(
            page=safe_page(request), per_page=safe_per_page(request, 20),
            employee_id=safe_int(request, 'employee_id'), status=request.GET.get('status'),
            date_from=date_from, date_to=date_to, category=request.GET.get('category'),
        )
        return JsonResponse(result, status=status)
    if denied := permission_denied_response(request, 'discipline.case.create'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = DisciplineService.create_case(data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def discipline_case_detail(request, case_id):
    if denied := permission_denied_response(request, 'discipline.case.view'):
        return denied
    result, status = DisciplineService.get_case(case_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('discipline.case.review', fallback_key_from_request=True)
def discipline_case_review(request, case_id, action):
    permission = 'discipline.case.void' if action == 'void' else 'discipline.case.approve'
    if denied := permission_denied_response(request, permission):
        return denied
    if action not in ('approve', 'reject', 'void'):
        return JsonResponse({'success': False, 'message': 'Unknown action'}, status=404)
    data, error = _payload(request)
    if error:
        return error
    reason = data.get('review_note') or data.get('reason')
    if action == 'approve':
        result, status = DisciplineService.approve_case(case_id, request.user, reason)
    elif action == 'reject':
        result, status = DisciplineService.reject_case(case_id, request.user, reason)
    else:
        result, status = DisciplineService.void_case(case_id, request.user, reason)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def preparation_audits(request):
    if denied := permission_denied_response(request, 'prep.audit.view'):
        return denied
    try:
        date_from = _date_query(request, 'date_from')
        date_to = _date_query(request, 'date_to')
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    result, status = PreparationAuditService.list(
        page=safe_page(request), per_page=safe_per_page(request, 20),
        date_from=date_from, date_to=date_to,
        branch_id=request.GET.get('branch_id') or request.GET.get('location_id'),
        performance_status=request.GET.get('performance_status'),
        review_status=request.GET.get('review_status'),
        category_id=safe_int(request, 'category_id'),
        cashier_id=safe_int(request, 'cashier_id') or safe_int(request, 'creator_id'),
        responsible_employee_id=safe_int(request, 'responsible_employee_id'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def preparation_audit_detail(request, audit_id):
    if denied := permission_denied_response(request, 'prep.audit.view'):
        return denied
    result, status = PreparationAuditService.detail(audit_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def preparation_audit_categories(request):
    if denied := permission_denied_response(request, 'prep.audit.view'):
        return denied
    result, status = PreparationAuditService.categories(active_only=True)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('preparation.audit.review', fallback_key_from_request=True)
def preparation_audit_review(request, audit_id):
    if denied := permission_denied_response(request, 'prep.audit.review'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = PreparationAuditService.review(audit_id, data, request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def preparation_audit_reopen(request, audit_id):
    if denied := permission_denied_response(request, 'prep.audit.reopen'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    result, status = PreparationAuditService.reopen(audit_id, request.user, data.get('reason'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def audit_dashboard(request):
    if denied := _or_permission(request, 'prep.audit.view', 'attendance.view'):
        return denied
    try:
        date_from = _date_query(request, 'date_from', True)
        date_to = _date_query(request, 'date_to', True)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    result, status = PreparationAuditService.dashboard(
        date_from=date_from, date_to=date_to,
        branch_id=request.GET.get('branch_id') or request.GET.get('location_id'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('preparation.audit.period.close', fallback_key_from_request=True)
def audit_period_close(request):
    if denied := permission_denied_response(request, 'prep.audit.reopen'):
        return denied
    data, error = _payload(request)
    if error:
        return error
    try:
        period_start = _parse_local_datetime(data.get('period_start'), 'period_start')
        period_end = _parse_local_datetime(data.get('period_end'), 'period_end')
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    if period_end <= period_start:
        return JsonResponse({'success': False, 'message': 'period_end must be later than period_start'}, status=422)
    result, status = PreparationAuditService.close_period(
        period_start, period_end, str(data.get('branch_id') or ''), request.user,
    )
    return JsonResponse(result, status=status)
