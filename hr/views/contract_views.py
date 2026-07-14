from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import ContractDocumentService, ContractService


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


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def contract_documents(request, contract_id):
    if request.method == "GET":
        result, status = ContractDocumentService.list(contract_id)
        return JsonResponse(result, status=status)

    if request.content_type == "application/json":
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
    else:
        data = request.POST.dict()

    result, status = ContractDocumentService.create(
        contract_id,
        title=data.get("title"),
        document_type=data.get("document_type", "CONTRACT"),
        uploaded_file=request.FILES.get("file"),
        file_url=data.get("file_url", ""),
        uploaded_by_id=request.user.id,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
@admin_required
def contract_document_detail(request, contract_id, doc_id):
    if request.method == "GET":
        result, status = ContractDocumentService.get(contract_id, doc_id)
    else:
        result, status = ContractDocumentService.delete(contract_id, doc_id)
    return JsonResponse(result, status=status)
