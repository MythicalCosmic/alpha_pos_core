from functools import wraps

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int, safe_date
from base.helpers.response import json_response
from base.security.audit import audit
from base.security.idempotency import idempotent
from base.security.permissions import admin_required, backoffice_permission_required
from base.services.branch_scope import resolve_actor_branch
from stock.models import StockItem, StockLocation
from stock.services.level_service import StockLevelService, StockTransactionService


def _body_id(data, field, *, required=True):
    value = data.get(field)
    if value in (None, '') and not required:
        return None, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    if isinstance(value, bool) or parsed <= 0 or str(parsed) != str(value).strip():
        return None, JsonResponse({
            'success': False,
            'code': 'VALIDATION_ERROR',
            'message': 'Validation failed',
            'errors': {field: ['Use a positive integer.']},
        }, status=422)
    return parsed, None


def _adjustment_scope_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
        stock_item_id, field_error = _body_id(data, 'stock_item_id')
        if field_error:
            return field_error
        location_id, field_error = _body_id(data, 'location_id')
        if field_error:
            return field_error
        branch_id = str(resolve_actor_branch(request.user) or '').strip()
        if not branch_id:
            return JsonResponse({
                'success': False,
                'code': 'BRANCH_SCOPE_REQUIRED',
                'message': 'Stock adjustment branch could not be resolved.',
            }, status=403)
        item_is_owned = StockItem.objects.filter(
            pk=stock_item_id, branch_id=branch_id,
            is_deleted=False, is_active=True,
        ).exists()
        location_is_owned = StockLocation.objects.filter(
            pk=location_id, branch_id=branch_id,
            is_deleted=False, is_active=True,
        ).exists()
        if not item_is_owned or not location_is_owned:
            return JsonResponse({
                'success': False,
                'code': 'STOCK_SCOPE_FORBIDDEN',
                'message': 'Stock item or location is outside the authorized branch.',
            }, status=403)
        request.stock_adjustment_branch_id = branch_id
        request.stock_adjustment_item_id = stock_item_id
        request.stock_adjustment_location_id = location_id
        return view_func(request, *args, **kwargs)
    return wrapper


@csrf_exempt
@require_GET
@backoffice_permission_required('stock.level.view')
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
@backoffice_permission_required('stock.level.view')
def stock_level_item(request, item_id):
    result, status = StockLevelService.get_for_item(item_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('stock.level.view')
def stock_level_location(request, location_id):
    result, status = StockLevelService.get_for_location(location_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('stock.adjustment.approve')
@_adjustment_scope_required
@idempotent(
    'stock.adjust', required=True, expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def stock_adjust(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    stock_item_id = request.stock_adjustment_item_id
    location_id = request.stock_adjustment_location_id
    unit_id, field_error = _body_id(data, 'unit_id', required=False)
    if field_error:
        return field_error
    batch_id, field_error = _body_id(data, 'batch_id', required=False)
    if field_error:
        return field_error
    branch_id = request.stock_adjustment_branch_id
    movement_type = str(data.get('movement_type') or '').strip().upper()
    allowed = {'ADJUSTMENT_PLUS', 'ADJUSTMENT_MINUS', 'WASTE', 'SPOILAGE'}
    reason = str(data.get('reason') or data.get('notes') or '').strip()
    if not reason:
        return JsonResponse({
            'success': False,
            'code': 'STOCK_ADJUSTMENT_REASON_REQUIRED',
            'message': 'A stock adjustment reason is required.',
            'errors': {'reason': ['This field is required.']},
        }, status=422)
    reference_type = (
        'StockWaste' if movement_type in {'WASTE', 'SPOILAGE'}
        else 'StockAdjustment'
    )
    result, status = StockLevelService.adjust(
        stock_item_id=stock_item_id,
        location_id=location_id,
        quantity=data.get('quantity'),
        movement_type=movement_type,
        user_id=request.user.id,
        unit_id=unit_id,
        batch_id=batch_id,
        reference_type=reference_type,
        notes=reason,
        branch_id=branch_id,
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
        allowed_movement_types=allowed,
        strict=True,
    )
    if status < 400:
        row = result.get('data') or {}
        audit(
            request,
            'STOCK_WASTE' if movement_type in {'WASTE', 'SPOILAGE'}
            else 'STOCK_ADJUSTMENT',
            target_type='StockTransaction',
            target_id=row.get('transaction_id'),
            metadata={
                'branch_id': branch_id,
                'location_id': data.get('location_id'),
                'stock_item_id': data.get('stock_item_id'),
                'movement_type': movement_type,
                'quantity': data.get('quantity'),
                'total_cost_uzs': row.get('total_cost_uzs'),
                'reason': reason,
                'idempotency_key': getattr(request, 'idempotency_key', ''),
            },
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('stock.adjustment.approve')
@idempotent(
    'stock.adjust.reverse', required=True, expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def stock_adjust_reverse(request, transaction_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    branch_id = str(resolve_actor_branch(request.user) or '').strip()
    if not branch_id:
        return JsonResponse({
            'success': False,
            'code': 'BRANCH_SCOPE_REQUIRED',
            'message': 'Stock adjustment branch could not be resolved.',
        }, status=403)
    result, status = StockLevelService.reverse_adjustment(
        transaction_id,
        actor=request.user,
        branch_id=branch_id,
        reason=data.get('reason'),
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    if status < 400:
        row = result.get('data') or {}
        audit(
            request, 'STOCK_ADJUSTMENT_REVERSE',
            target_type='StockTransaction',
            target_id=row.get('transaction_id'),
            metadata={
                'branch_id': branch_id,
                'reversal_of_transaction_id': transaction_id,
                'reason': data.get('reason'),
                'idempotency_key': getattr(request, 'idempotency_key', ''),
            },
        )
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
@backoffice_permission_required('stock.level.view')
def low_stock(request):
    location_id = safe_int(request, "location_id")
    result, status = StockLevelService.get_low_stock_items(location_id=location_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@backoffice_permission_required('stock.batch.view')
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
@backoffice_permission_required('stock.batch.view')
def transaction_history(request, item_id):
    days = safe_int(request, "days", 30, minimum=1, maximum=3650)
    result, status = StockTransactionService.get_item_history(item_id, days)
    return JsonResponse(result, status=status)
