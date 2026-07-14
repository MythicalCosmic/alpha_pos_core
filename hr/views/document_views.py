from django.core.exceptions import SuspiciousFileOperation
from django.http import JsonResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import DocumentService


# Whitelist of model, file field, and parent-scope combinations available to
# the secure download endpoint. Adding a new file field requires editing this
# map: defense in depth against a download URL reading arbitrary files.
_DOWNLOADABLE_FILES = {
    'employee_document': ('hr.EmployeeDocument', 'file', {}),
    # A soft-deleted contract does not cascade in SQL, so explicitly revoke its
    # child files even if a stale direct download URL is still known.
    'contract_document': (
        'hr.ContractDocument', 'file', {'contract__is_deleted': False},
    ),
    'expense_receipt': ('hr.Expense', 'receipt_file', {}),
}


@csrf_exempt
@require_GET
@admin_required
def secure_download(request, kind, obj_id):
    # Stream a private HR file (passport, contract, receipt) only after
    # admin auth. The file lives outside DEBUG-served static dirs and the
    # filename never appears in the URL: the (kind, id) pair maps via
    # _DOWNLOADABLE_FILES to a known model, FileField, and lifecycle scope.
    from django.apps import apps

    if kind not in _DOWNLOADABLE_FILES:
        return JsonResponse(
            {"success": False, "message": "Document not found"}, status=404
        )

    model_label, field_name, scope = _DOWNLOADABLE_FILES[kind]
    model_cls = apps.get_model(model_label)

    try:
        obj = model_cls.objects.get(pk=obj_id, is_deleted=False, **scope)
    except model_cls.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Document not found"}, status=404
        )

    file_field = getattr(obj, field_name, None)
    if not file_field or not file_field.name:
        return JsonResponse(
            {"success": False, "message": "File not available"}, status=404
        )

    try:
        handle = file_field.open('rb')
    except (FileNotFoundError, OSError, ValueError, SuspiciousFileOperation):
        # The DB row references a file that isn't on this server's disk — e.g.
        # the database was synced/restored to a machine that doesn't have the
        # media, or MEDIA_ROOT differs across machines. Return 404 instead of a
        # 500 traceback.
        return JsonResponse(
            {"success": False, "message": "File not available"}, status=404
        )

    response = FileResponse(
        handle,
        as_attachment=True,
        filename=file_field.name.rsplit('/', 1)[-1],
    )
    # Private personnel records must not be retained by shared browser/proxy
    # caches. ``nosniff`` and sandboxing add defense in depth if a legacy record
    # points at content whose filename or MIME declaration is misleading.
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def documents(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        employee_id = request.GET.get("employee_id")
        document_type = request.GET.get("document_type")
        result, status_code = DocumentService.list(
            page=page, per_page=per_page, employee_id=employee_id, document_type=document_type
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = DocumentService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def document_detail(request, doc_id):
    if request.method == "GET":
        result, status = DocumentService.get(doc_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = DocumentService.delete(doc_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = DocumentService.update(doc_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def document_verify(request, doc_id):
    result, status = DocumentService.verify(doc_id, verified_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def documents_expiring(request):
    days = safe_int(request, "days", 30, minimum=1, maximum=3650)
    result, status = DocumentService.get_expiring(days=days)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def documents_by_employee(request, employee_id):
    result, status = DocumentService.get_by_employee(employee_id)
    return JsonResponse(result, status=status)
