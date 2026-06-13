from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import ProductionOrderService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def production_orders(request):
    if request.method == "GET":
        result, status = ProductionOrderService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            status=request.GET.get("status"),
            recipe_id=safe_int(request, "recipe_id"),
            priority=request.GET.get("priority"),
            location_id=safe_int(request, "location_id"),
        )
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ProductionOrderService.create(
        **{k: v for k, v in data.items() if k != "created_by_id"},
        created_by_id=request.user.id,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def production_order_detail(request, order_id):
    if request.method == "GET":
        result, status = ProductionOrderService.get(order_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ProductionOrderService.update(order_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def production_order_action(request, order_id, action):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    if action == "plan":
        from datetime import datetime

        planned_start = None
        if data.get("planned_start"):
            planned_start = datetime.fromisoformat(data["planned_start"])
        result, status = ProductionOrderService.plan(order_id, planned_start)

    elif action == "start":
        result, status = ProductionOrderService.start(order_id, user_id=request.user.id)

    elif action == "complete":
        result, status = ProductionOrderService.complete(
            order_id,
            actual_output_qty=data.get("actual_output_qty"),
            user_id=request.user.id,
            quality_status=data.get("quality_status", "PASSED"),
            notes=data.get("notes", ""),
        )

    elif action == "cancel":
        result, status = ProductionOrderService.cancel(order_id, reason=data.get("reason", ""))

    elif action == "hold":
        result, status = ProductionOrderService.hold(order_id)

    elif action == "resume":
        result, status = ProductionOrderService.resume(order_id)

    else:
        return JsonResponse(
            {"success": False, "message": f"Unknown action: {action}"},
            status=400,
        )

    return JsonResponse(result, status=status)
