from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import AttendanceService


@csrf_exempt
@require_http_methods(["GET"])
@admin_required
def attendance_list(request):
    page = safe_page(request)
    per_page = safe_per_page(request, 20)
    employee_id = request.GET.get("employee_id")
    date = request.GET.get("date")
    status = request.GET.get("status")
    result, status_code = AttendanceService.list(
        page=page, per_page=per_page, employee_id=employee_id, date=date, status=status
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET"])
@admin_required
def attendance_detail(request, attendance_id):
    # PUT was removed: AttendanceService.update never existed and the
    # PUT path 500'd. Editing attendance after the fact requires a real
    # adjustment workflow with audit trail — to be designed.
    result, status = AttendanceService.get(attendance_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
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
@admin_required
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
@admin_required
def attendance_daily_report(request):
    date = request.GET.get("date")
    result, status = AttendanceService.get_daily_report(date=date)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def attendance_monthly_report(request):
    employee_id = request.GET.get("employee_id")
    year = request.GET.get("year")
    month = request.GET.get("month")
    result, status = AttendanceService.get_monthly_report(
        employee_id=employee_id, year=year, month=month
    )
    return JsonResponse(result, status=status)
