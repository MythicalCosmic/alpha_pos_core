from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int, safe_date
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import PurchaseOrderService, PurchaseOrderItemService, PurchaseReceivingService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def purchase_orders(request):
    if request.method == "GET":
        date_from = safe_date(request, "date_from")
        date_to = safe_date(request, "date_to")

        result, status_code = PurchaseOrderService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            supplier_id=safe_int(request, "supplier_id"),
            status=request.GET.get("status"),
            date_from=date_from,
            date_to=date_to,
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseOrderService.create(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def purchase_order_detail(request, po_id):
    if request.method == "GET":
        result, status_code = PurchaseOrderService.get(po_id)
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseOrderService.update(po_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def purchase_order_action(request, po_id, action):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    if action == "send":
        result, status_code = PurchaseOrderService.send(po_id)
    elif action == "confirm":
        result, status_code = PurchaseOrderService.confirm(po_id, approved_by_id=request.user.id)
    elif action == "cancel":
        result, status_code = PurchaseOrderService.cancel(po_id, reason=data.get("reason", ""))
    else:
        return JsonResponse(
            {"success": False, "message": f"Unknown action: {action}"},
            status=400,
        )

    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def purchase_order_items(request, po_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseOrderItemService.add(purchase_order_id=po_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PUT", "DELETE"])
@admin_required
def purchase_order_item_detail(request, item_id):
    if request.method == "DELETE":
        result, status_code = PurchaseOrderItemService.remove_item(item_id)
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseOrderItemService.update_item(item_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def purchase_receiving(request, po_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseReceivingService.create(
        purchase_order_id=po_id,
        received_by_id=request.user.id,
        **{k: v for k, v in data.items() if k not in ["received_by_id"]},
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def purchase_receiving_items(request, receiving_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseReceivingService.add_item(receiving_id=receiving_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def purchase_receiving_complete(request, receiving_id):
    result, status_code = PurchaseReceivingService.complete(receiving_id)
    return JsonResponse(result, status=status_code)
