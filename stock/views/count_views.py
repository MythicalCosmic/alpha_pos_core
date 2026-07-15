from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import StockCountService, VarianceReasonCodeService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def stock_counts(request):
    if request.method == "GET":
        result, status = StockCountService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            status=request.GET.get("status"),
            location_id=safe_int(request, "location_id"),
            count_type=request.GET.get("type"),
        )
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # The acting user is always the authenticated admin — never trust a
    # client-supplied counted_by_id (actor spoofing + downstream approval
    # attribution).
    data.pop("counted_by_id", None)
    result, status = StockCountService.create(**data, counted_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def stock_count_detail(request, count_id):
    result, status = StockCountService.get(count_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def stock_count_action(request, count_id, action):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    user_id = request.user.id

    if action == "start":
        result, status = StockCountService.start(count_id)
    elif action == "complete":
        result, status = StockCountService.complete(count_id)
    elif action == "approve":
        apply_adjustments = data.get("apply_adjustments", True)
        result, status = StockCountService.approve(count_id, user_id, apply_adjustments)
    elif action == "cancel":
        result, status = StockCountService.cancel(count_id, reason=data.get("reason", ""))
    else:
        return JsonResponse(
            {"success": False, "message": f"Unknown action: {action}"},
            status=400,
        )

    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def stock_count_record(request, count_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockCountService.record_count(count_id=count_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def variance_codes(request):
    if request.method == "GET":
        active_only = request.GET.get("active", "true").lower() == "true"
        result, status = VarianceReasonCodeService.list(active_only)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = VarianceReasonCodeService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def variance_code_detail(request, code_id):
    if request.method == "GET":
        result, status = VarianceReasonCodeService.get(code_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = VarianceReasonCodeService.delete(code_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = VarianceReasonCodeService.update(code_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def variance_codes_seed(request):
    result, status = VarianceReasonCodeService.seed_defaults()
    return JsonResponse(result, status=status)
