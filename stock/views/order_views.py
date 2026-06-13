from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import OrderStockService


@csrf_exempt
@require_POST
@admin_required
def order_stock_deduct(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = OrderStockService.deduct_for_order(
        order_id=data["order_id"],
        order_items=data["order_items"],
        location_id=data["location_id"],
        user_id=request.user.id,
        order_status=data.get("order_status"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def order_stock_reverse(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    data["user_id"] = request.user.id
    result, status = OrderStockService.reverse_deduction(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def order_stock_availability(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = OrderStockService.check_availability(
        order_items=data["order_items"],
        location_id=data["location_id"],
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def order_stock_reserve(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = OrderStockService.reserve_for_order(
        order_id=data["order_id"],
        order_items=data["order_items"],
        location_id=data["location_id"],
        user_id=request.user.id,
    )
    return JsonResponse(result, status=status)
