from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import EmployeeService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def employees(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        result, status = EmployeeService.list(page=page, per_page=per_page)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = EmployeeService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def employee_detail(request, employee_id):
    if request.method == "GET":
        result, status = EmployeeService.get(employee_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = EmployeeService.delete(employee_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = EmployeeService.update(employee_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def employee_stats(request):
    result, status = EmployeeService.get_stats()
    return JsonResponse(result, status=status)
