from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import LeaveService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def leave_types(request):
    if request.method == "GET":
        result, status = LeaveService.list_types()
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = LeaveService.create_type(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def leave_type_detail(request, type_id):
    if request.method == "GET":
        result, status = LeaveService.get_type(type_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = LeaveService.delete_type(type_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = LeaveService.update_type(type_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def leave_requests(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        employee_id = request.GET.get("employee_id")
        status_filter = request.GET.get("status")
        leave_type_id = request.GET.get("leave_type_id")
        result, status_code = LeaveService.list_requests(
            page=page, per_page=per_page, employee_id=employee_id,
            status=status_filter, leave_type_id=leave_type_id,
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = LeaveService.create_request(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def leave_detail(request, leave_id):
    result, status = LeaveService.get_request(leave_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def leave_approve(request, leave_id):
    result, status = LeaveService.approve(leave_id, approved_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def leave_reject(request, leave_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = LeaveService.reject(leave_id, approved_by_id=request.user.id, notes=data.get("notes", ""))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def leave_cancel(request, leave_id):
    result, status = LeaveService.cancel(leave_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def leave_balances(request):
    employee_id = request.GET.get("employee_id")
    year = request.GET.get("year")
    if not employee_id:
        return JsonResponse(
            {"success": False, "message": "employee_id is required"},
            status=400,
        )
    result, status = LeaveService.get_balance(
        employee_id=int(employee_id),
        year=int(year) if year else None,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def leave_balance_initialize(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    year = data.get("year")
    if not year:
        from django.utils import timezone
        year = timezone.now().year
    result, status = LeaveService.initialize_annual_balances(year=int(year))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def leave_balance_by_employee(request, employee_id):
    year = request.GET.get("year")
    result, status = LeaveService.get_balance(
        employee_id=employee_id,
        year=int(year) if year else None,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def leave_calendar(request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    result, status = LeaveService.get_calendar(year=int(year), month=int(month))
    return JsonResponse(result, status=status)
