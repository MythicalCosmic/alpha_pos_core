from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import EmploymentEventService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def events(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        employee_id = request.GET.get("employee_id")
        event_type = request.GET.get("event_type")
        result, status_code = EmploymentEventService.list(
            page=page, per_page=per_page, employee_id=employee_id, event_type=event_type
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = EmploymentEventService.log_event(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def employee_timeline(request, employee_id):
    page = safe_page(request)
    per_page = safe_per_page(request, 20)
    result, status = EmploymentEventService.get_timeline(
        employee_id, page=page, per_page=per_page
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def event_detail(request, event_id):
    result, status = EmploymentEventService.get_event(event_id)
    return JsonResponse(result, status=status)
