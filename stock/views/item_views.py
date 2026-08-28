from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import (
    backoffice_required, backoffice_permission_required, permission_denied_response,
)
from stock.services.item_service import StockItemService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def stock_items(request):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.catalog.view'):
            return denied
        result, status = StockItemService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            search=request.GET.get("search"),
            item_type=request.GET.get("type"),
            category_id=safe_int(request, "category_id"),
            low_stock_only=request.GET.get("low_stock") == "true",
            active_only=request.GET.get("active_only", "true") == "true",
        )
        if request.user.role != 'ADMIN' and result.get('success'):
            for item in (result.get('data') or {}).get('items', []):
                for key in ('cost_price', 'avg_cost_price', 'last_cost_price'):
                    item.pop(key, None)
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = StockItemService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@backoffice_required
def stock_item_detail(request, item_id):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.catalog.view'):
            return denied
        result, status = StockItemService.get(item_id)
        if request.user.role != 'ADMIN' and result.get('success'):
            item = (result.get('data') or {}).get('item') or {}
            for key in ('cost_price', 'avg_cost_price', 'last_cost_price'):
                item.pop(key, None)
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    if request.method == "DELETE":
        result, status = StockItemService.deactivate(item_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = StockItemService.update(item_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('stock.catalog.view')
def stock_item_search(request):
    query = request.GET.get("q", "")
    limit = safe_int(request, "limit", 20, minimum=1, maximum=100)
    result, status = StockItemService.search(query, limit)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('stock.catalog.view')
def stock_item_barcode(request, barcode):
    result, status = StockItemService.find_by_barcode(barcode)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('stock.manage')
def stock_item_stats(request):
    result, status = StockItemService.get_stats()
    return JsonResponse(result, status=status)
