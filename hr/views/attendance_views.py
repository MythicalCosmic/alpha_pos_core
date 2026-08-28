from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import backoffice_permission_required
from hr.services import AttendanceService
from hr.services.operational_audit_service import AttendanceOperations, _parse_date


@csrf_exempt
@require_http_methods(["GET"])
@backoffice_permission_required('attendance.view')
def attendance_list(request):
    page = safe_page(request)
    per_page = safe_per_page(request, 20)
    employee_id = request.GET.get("employee_id")
    date_value = request.GET.get("date")
    status = request.GET.get("status")
    try:
        exact_date = _parse_date(date_value) if date_value else None
        date_from = _parse_date(request.GET.get('date_from')) if request.GET.get('date_from') else exact_date
        date_to = _parse_date(request.GET.get('date_to')) if request.GET.get('date_to') else exact_date
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    result, status_code = AttendanceOperations.list(
        page=page, per_page=per_page, employee_id=employee_id, status=status,
        date_from=date_from, date_to=date_to,
        branch_id=request.GET.get('branch_id') or request.GET.get('location_id'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET"])
@backoffice_permission_required('attendance.view')
def attendance_detail(request, attendance_id):
    # PUT was removed: AttendanceService.update never existed and the
    # PUT path 500'd. Editing attendance after the fact requires a real
    # adjustment workflow with audit trail — to be designed.
    result, status = AttendanceOperations.detail(attendance_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('attendance.record')
def attendance_check_in(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = AttendanceService.check_in(
        employee_id=data.get("employee_id"),
        notes=data.get("notes"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('attendance.record')
def attendance_check_out(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = AttendanceService.check_out(
        employee_id=data.get("employee_id"),
        notes=data.get("notes"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('attendance.view')
def attendance_daily_report(request):
    date = request.GET.get("date")
    result, status = AttendanceService.get_daily_report(date=date)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('attendance.view')
def attendance_monthly_report(request):
    employee_id = request.GET.get("employee_id")
    year = request.GET.get("year")
    month = request.GET.get("month")
    result, status = AttendanceService.get_monthly_report(
        employee_id=employee_id, year=year, month=month
    )
    return JsonResponse(result, status=status)
