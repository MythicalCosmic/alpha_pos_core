from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int, safe_date
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services.level_service import StockLevelService, StockTransactionService


@csrf_exempt
@require_GET
@admin_required
def stock_levels(request):
    result, status = StockLevelService.get_all(
        page=safe_page(request),
        per_page=safe_per_page(request, 50),
        location_id=safe_int(request, "location_id"),
        category_id=safe_int(request, "category_id"),
        item_type=request.GET.get("item_type"),
        low_stock_only=request.GET.get("low_stock_only", "").lower() == "true",
        search=request.GET.get("search"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def stock_level_item(request, item_id):
    result, status = StockLevelService.get_for_item(item_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def stock_level_location(request, location_id):
    result, status = StockLevelService.get_for_location(location_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def stock_adjust(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = StockLevelService.adjust(**data, user_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def stock_reserve(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = StockLevelService.reserve(**data, user_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def stock_release_reservation(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = StockLevelService.release_reservation(**data, user_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def low_stock(request):
    location_id = safe_int(request, "location_id")
    result, status = StockLevelService.get_low_stock_items(location_id=location_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def transactions(request):
    date_from = safe_date(request, "date_from")
    date_to = safe_date(request, "date_to")

    result, status = StockTransactionService.list(
        page=safe_page(request),
        per_page=safe_per_page(request, 50),
        stock_item_id=safe_int(request, "stock_item_id"),
        location_id=safe_int(request, "location_id"),
        movement_type=request.GET.get("type"),
        date_from=date_from,
        date_to=date_to,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def transaction_history(request, item_id):
    days = safe_int(request, "days", 30, minimum=1, maximum=3650)
    result, status = StockTransactionService.get_item_history(item_id, days)
    return JsonResponse(result, status=status)
