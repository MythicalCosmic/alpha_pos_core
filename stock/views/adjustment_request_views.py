from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.audit import audit
from base.security.permissions import (
    backoffice_required, permission_denied_response, user_has_permission,
)
from stock.services.adjustment_request_service import StockAdjustmentRequestService


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def adjustment_requests(request):
    if denied := permission_denied_response(request, 'stock.adjustment.request'):
        return denied
    if request.method == 'GET':
        result, status = StockAdjustmentRequestService.list(
            actor=request.user, page=safe_page(request),
            per_page=safe_per_page(request, 20), status=request.GET.get('status'),
            view_all=user_has_permission(request.user, 'stock.adjustment.approve'),
        )
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    data.pop('requested_by', None)
    result, status = StockAdjustmentRequestService.create(
        **data, requested_by=request.user,
    )
    if status < 400:
        row = (result.get('data') or {}).get('adjustment_request') or {}
        audit(request, 'STOCK_ADJUSTMENT_REQUEST', target_type='StockAdjustmentRequest',
              target_id=row.get('id'), metadata={'new_state': 'PENDING'})
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def adjustment_review(request, request_id, action):
    if denied := permission_denied_response(request, 'stock.adjustment.approve'):
        return denied
    if action not in ('approve', 'reject'):
        return JsonResponse({'success': False, 'message': 'Unknown action'}, status=404)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = StockAdjustmentRequestService.review(
        request_id, reviewer=request.user, approve=action == 'approve',
        note=data.get('review_note') or data.get('reason'),
    )
    if status < 400:
        audit(request, f'STOCK_ADJUSTMENT_{action.upper()}',
              target_type='StockAdjustmentRequest', target_id=request_id,
              metadata={'previous_state': 'PENDING', 'new_state': action.upper(),
                        'reason': data.get('review_note') or data.get('reason')})
    return JsonResponse(result, status=status)
