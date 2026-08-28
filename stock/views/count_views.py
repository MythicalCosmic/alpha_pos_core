from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import backoffice_required, permission_denied_response
from stock.models import StockCount
from stock.services import StockCountService, VarianceReasonCodeService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def stock_counts(request):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.count.view'):
            return denied
        result, status = StockCountService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            status=request.GET.get("status"),
            location_id=safe_int(request, "location_id"),
            count_type=request.GET.get("type"),
        )
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.count.create'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # The acting user is always the authenticated admin — never trust a
    # client-supplied counted_by_id (actor spoofing + downstream approval
    # attribution).
    data.pop("counted_by_id", None)
    if request.user.role != 'ADMIN':
        data['auto_adjust'] = False
    result, status = StockCountService.create(**data, counted_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_required
def stock_count_detail(request, count_id):
    if denied := permission_denied_response(request, 'stock.count.view'):
        return denied
    result, status = StockCountService.get(count_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def stock_count_action(request, count_id, action):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    user_id = request.user.id
    count = StockCount.objects.filter(id=count_id, is_deleted=False).first()
    if not count:
        return JsonResponse({'success': False, 'message': 'Stock count not found'}, status=404)

    if action == "start":
        if denied := permission_denied_response(request, 'stock.count.record'):
            return denied
        result, status = StockCountService.start(count_id)
    elif action == "complete":
        if denied := permission_denied_response(request, 'stock.count.record'):
            return denied
        result, status = StockCountService.complete(count_id)
    elif action == "approve":
        if denied := permission_denied_response(request, 'stock.adjustment.approve'):
            return denied
        if count.counted_by_id == request.user.id:
            return JsonResponse({
                'success': False, 'message': 'You cannot approve your own stock count',
                'errors': {'code': 'self_approval_forbidden'},
            }, status=403)
        apply_adjustments = data.get("apply_adjustments", True)
        result, status = StockCountService.approve(count_id, user_id, apply_adjustments)
    elif action == "cancel":
        if denied := permission_denied_response(request, 'stock.manage'):
            return denied
        result, status = StockCountService.cancel(count_id, reason=data.get("reason", ""))
    else:
        return JsonResponse(
            {"success": False, "message": f"Unknown action: {action}"},
            status=400,
        )

    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def stock_count_record(request, count_id):
    if denied := permission_denied_response(request, 'stock.count.record'):
        return denied
    count = StockCount.objects.filter(id=count_id, is_deleted=False).first()
    if (not count or (request.user.role != 'ADMIN'
                      and count.counted_by_id != request.user.id)):
        return JsonResponse({
            'success': False, 'message': 'Stock count is not assigned to you',
            'errors': {'code': 'stock_count_not_owned'},
        }, status=403 if count else 404)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockCountService.record_count(count_id=count_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def variance_codes(request):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.count.view'):
            return denied
        active_only = request.GET.get("active", "true").lower() == "true"
        result, status = VarianceReasonCodeService.list(active_only)
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = VarianceReasonCodeService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@backoffice_required
def variance_code_detail(request, code_id):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.count.view'):
            return denied
        result, status = VarianceReasonCodeService.get(code_id)
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
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
@backoffice_required
def variance_codes_seed(request):
    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    result, status = VarianceReasonCodeService.seed_defaults()
    return JsonResponse(result, status=status)
