from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import ContractService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def contracts(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        employee_id = request.GET.get("employee_id")
        status = request.GET.get("status")
        result, status_code = ContractService.list(
            page=page, per_page=per_page, employee_id=employee_id, status=status
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = ContractService.create(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def contract_detail(request, contract_id):
    if request.method == "GET":
        result, status = ContractService.get(contract_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = ContractService.delete(contract_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ContractService.update(contract_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def contract_activate(request, contract_id):
    result, status = ContractService.activate(contract_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def contract_terminate(request, contract_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ContractService.terminate(
        contract_id,
        termination_date=data.get("termination_date"),
        termination_reason=data.get("termination_reason"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def contract_renew(request, contract_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ContractService.renew(
        contract_id,
        new_start_date=data.get("new_start_date"),
        new_end_date=data.get("new_end_date"),
        new_salary=data.get("new_salary"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def contracts_expiring(request):
    days = safe_int(request, "days", 30, minimum=1, maximum=3650)
    result, status = ContractService.get_expiring(days=days)
    return JsonResponse(result, status=status)


# Contract-document attachments are not yet implemented. The view stubs
# below previously called ContractService.list_documents / get_document /
# create_document / delete_document, but the service has never defined
# any of those — every call 500'd with AttributeError. Returning 501 makes
# the unimplemented state honest to API consumers; the routes still exist
# so the absence is discoverable rather than a NoReverseMatch.

@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def contract_documents(request, contract_id):
    return JsonResponse({
        'success': False,
        'message': 'Contract document attachments are not implemented.',
    }, status=501)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
@admin_required
def contract_document_detail(request, contract_id, doc_id):
    return JsonResponse({
        'success': False,
        'message': 'Contract document attachments are not implemented.',
    }, status=501)
