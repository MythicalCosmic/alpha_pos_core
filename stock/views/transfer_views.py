from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import StockTransferService, StockTransferItemService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def transfers(request):
    if request.method == "GET":
        result, status = StockTransferService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            status=request.GET.get("status"),
            from_location_id=safe_int(request, "from_location_id"),
            to_location_id=safe_int(request, "to_location_id"),
            transfer_type=request.GET.get("type"),
        )
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    data["requested_by_id"] = request.user.id
    result, status = StockTransferService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def transfer_detail(request, transfer_id):
    if request.method == "GET":
        result, status = StockTransferService.get(transfer_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockTransferService.update(transfer_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def transfer_action(request, transfer_id, action):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    user_id = request.user.id

    if action == "request":
        result, status = StockTransferService.request(transfer_id)
    elif action == "approve":
        result, status = StockTransferService.approve(transfer_id, user_id)
    elif action == "ship":
        result, status = StockTransferService.ship(transfer_id, user_id)
    elif action == "receive":
        received_quantities = data.get("received_quantities", {})
        result, status = StockTransferService.receive(transfer_id, user_id, received_quantities)
    elif action == "cancel":
        result, status = StockTransferService.cancel(transfer_id, reason=data.get("reason", ""))
    else:
        return JsonResponse(
            {"success": False, "message": f"Unknown action: {action}"},
            status=400,
        )

    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def transfer_items(request, transfer_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockTransferItemService.add_item(transfer_id=transfer_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def quick_transfer(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    data["user_id"] = request.user.id
    result, status = StockTransferService.quick_transfer(**data)
    return JsonResponse(result, status=status)
