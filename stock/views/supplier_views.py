from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import SupplierService, SupplierStockItemService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def suppliers(request):
    if request.method == "GET":
        result, status_code = SupplierService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            search=request.GET.get("search"),
            active_only=request.GET.get("active_only", "true").lower() == "true",
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = SupplierService.create(**data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def supplier_detail(request, supplier_id):
    if request.method == "GET":
        result, status_code = SupplierService.get(supplier_id)
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        result, status_code = SupplierService.deactivate(supplier_id)
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = SupplierService.update(supplier_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def supplier_items(request, supplier_id):
    if request.method == "GET":
        result, status_code = SupplierService.get(supplier_id, include_items=True, include_stats=False)
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = SupplierStockItemService.add_item(supplier_id=supplier_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["POST"])
@admin_required
def supplier_pay(request, supplier_id):
    """Pay a supplier from SAFE/BANK (bank payments may carry a commission).
    Debits the treasury and reduces what we owe (a PAYMENT ledger row)."""
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    from stock.services.supplier_ledger_service import SupplierLedgerService
    result, status_code = SupplierLedgerService.pay_supplier(
        supplier_id=supplier_id,
        amount=data.get("amount"),
        source_account=data.get("source_account", "SAFE"),
        commission=data.get("commission", 0) or data.get("fee", 0),
        note=data.get("note", ""),
        performed_by=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET"])
@admin_required
def supplier_ledger(request, supplier_id):
    """Supplier balance ledger (purchases, payments, returns, adjustments)."""
    from stock.services.supplier_ledger_service import SupplierLedgerService
    result, status_code = SupplierLedgerService.history(
        supplier_id, page=safe_page(request), per_page=safe_per_page(request, 20),
    )
    return JsonResponse(result, status=status_code)
