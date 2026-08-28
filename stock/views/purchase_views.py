from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int, safe_date
from base.helpers.response import json_response
from base.security.idempotency import idempotent
from base.security.permissions import (
    admin_required, backoffice_required, permission_denied_response,
)
from base.security.audit import audit
from base.models import User
from stock.models import PurchaseReceiving, PurchaseReceivingItem
from stock.services import PurchaseOrderService, PurchaseOrderItemService, PurchaseReceivingService


def _deny(request, permission):
    return permission_denied_response(request, permission)


def _owned_draft(request, receiving_id):
    receiving = PurchaseReceiving.objects.filter(
        id=receiving_id, is_deleted=False,
    ).first()
    if not receiving:
        return None, JsonResponse({'success': False, 'message': 'Receiving not found'}, status=404)
    if receiving.status != PurchaseReceiving.Status.DRAFT:
        return None, JsonResponse({
            'success': False, 'message': 'Completed receiving is immutable',
            'errors': {'code': 'receiving_immutable'},
        }, status=409)
    if request.user.role != User.RoleChoices.ADMIN and receiving.received_by_id != request.user.id:
        return None, JsonResponse({
            'success': False, 'message': 'This receiving is not assigned to you',
            'errors': {'code': 'receiving_not_owned'},
        }, status=403)
    return receiving, None


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def purchase_orders(request):
    if request.method == "GET":
        if denied := _deny(request, 'stock.purchase.view'):
            return denied
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

    if denied := _deny(request, 'stock.manage'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseOrderService.create(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@backoffice_required
def purchase_order_detail(request, po_id):
    if request.method == "GET":
        if denied := _deny(request, 'stock.purchase.view'):
            return denied
        result, status_code = PurchaseOrderService.get(po_id)
        return JsonResponse(result, status=status_code)

    if denied := _deny(request, 'stock.manage'):
        return denied
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
@require_http_methods(["GET", "POST"])
@backoffice_required
def purchase_receiving(request, po_id):
    if request.method == 'GET':
        if denied := _deny(request, 'stock.purchase.view'):
            return denied
        rows = PurchaseReceiving.objects.filter(
            purchase_order_id=po_id, is_deleted=False,
        ).select_related('purchase_order__supplier', 'location', 'received_by')
        return JsonResponse({'success': True, 'data': {
            'receivings': [PurchaseReceivingService.serialize(row) for row in rows],
        }})
    if denied := _deny(request, 'stock.receiving.create'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseReceivingService.create(
        purchase_order_id=po_id,
        received_by_id=request.user.id,
        **{k: v for k, v in data.items() if k not in ["received_by_id"]},
    )
    if status_code < 400:
        audit(request, 'RECEIVING_CREATE', target_type='PurchaseReceiving',
              target_id=(result.get('data') or {}).get('id'),
              metadata={'purchase_order_id': po_id, 'new_state': 'DRAFT'})
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def purchase_receiving_items(request, receiving_id):
    if request.method == 'GET':
        if denied := _deny(request, 'stock.batch.view'):
            return denied
        receiving = PurchaseReceiving.objects.select_related(
            'purchase_order__supplier', 'location', 'received_by',
        ).filter(id=receiving_id, is_deleted=False).first()
        if not receiving:
            return JsonResponse({'success': False, 'message': 'Receiving not found'}, status=404)
        return JsonResponse({'success': True, 'data': {
            'receiving': PurchaseReceivingService.serialize(receiving),
        }})
    if denied := _deny(request, 'stock.receiving.update_draft'):
        return denied
    _receiving, ownership_error = _owned_draft(request, receiving_id)
    if ownership_error:
        return ownership_error
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = PurchaseReceivingService.add_item(receiving_id=receiving_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
@backoffice_required
def purchase_receiving_item_detail(request, item_id):
    if denied := _deny(request, 'stock.receiving.update_draft'):
        return denied
    item = PurchaseReceivingItem.objects.select_related('receiving').filter(
        id=item_id, is_deleted=False,
    ).first()
    if not item:
        return JsonResponse({'success': False, 'message': 'Receiving item not found'}, status=404)
    _receiving, ownership_error = _owned_draft(request, item.receiving_id)
    if ownership_error:
        return ownership_error
    if request.method == 'DELETE':
        item.delete()
        return JsonResponse({'success': True, 'message': 'Receiving item removed'})
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = PurchaseReceivingService.update_item(item.id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('stock.receiving.complete', fallback_key_from_request=True)
def purchase_receiving_complete(request, receiving_id):
    if denied := _deny(request, 'stock.receiving.complete'):
        return denied
    receiving = PurchaseReceiving.objects.filter(id=receiving_id, is_deleted=False).first()
    if (receiving and receiving.status == PurchaseReceiving.Status.DRAFT
            and request.user.role != User.RoleChoices.ADMIN
            and receiving.received_by_id != request.user.id):
        return JsonResponse({
            'success': False, 'message': 'This receiving is not assigned to you',
            'errors': {'code': 'receiving_not_owned'},
        }, status=403)
    result, status_code = PurchaseReceivingService.complete(receiving_id)
    if status_code < 400:
        audit(request, 'RECEIVING_COMPLETE', target_type='PurchaseReceiving',
              target_id=receiving_id,
              metadata={'previous_state': 'DRAFT', 'new_state': 'COMPLETED'})
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@backoffice_required
def purchase_receiving_approve_over(request, receiving_id):
    if denied := _deny(request, 'stock.receiving.approve_over'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    reason = str(data.get('reason') or '').strip()
    if not reason:
        return JsonResponse({'success': False, 'message': 'Reason is required',
                             'errors': {'reason': 'Required'}}, status=422)
    receiving = PurchaseReceiving.objects.filter(
        id=receiving_id, status=PurchaseReceiving.Status.DRAFT, is_deleted=False,
    ).first()
    if not receiving:
        return JsonResponse({'success': False, 'message': 'Draft receiving not found'}, status=409)
    if receiving.received_by_id == request.user.id:
        return JsonResponse({
            'success': False, 'message': 'You cannot approve your own receiving adjustment',
            'errors': {'code': 'self_approval_forbidden'},
        }, status=403)
    from django.utils import timezone
    receiving.over_receipt_approved_by = request.user
    receiving.over_receipt_approved_at = timezone.now()
    receiving.over_receipt_reason = reason
    receiving.save(update_fields=[
        'over_receipt_approved_by', 'over_receipt_approved_at',
        'over_receipt_reason', 'updated_at',
    ])
    audit(request, 'RECEIVING_OVER_APPROVE', target_type='PurchaseReceiving',
          target_id=receiving.id, metadata={'reason': reason})
    return JsonResponse({'success': True, 'data': {
        'receiving': PurchaseReceivingService.serialize(receiving),
    }})


@csrf_exempt
@require_POST
@backoffice_required
def purchase_receiving_correction_request(request, receiving_id):
    if denied := _deny(request, 'stock.receiving.create'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = PurchaseReceivingService.request_correction(
        receiving_id, request.user, data.get('reason'),
    )
    if status < 400:
        audit(request, 'RECEIVING_CORRECTION_REQUEST', target_type='PurchaseReceiving',
              target_id=receiving_id, metadata={'reason': data.get('reason')})
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent('stock.receiving.correction.review', fallback_key_from_request=True)
def purchase_receiving_correction_review(request, correction_id, action):
    if denied := _deny(request, 'stock.receiving.correct.approve'):
        return denied
    if action not in ('approve', 'reject'):
        return JsonResponse({'success': False, 'message': 'Unknown action'}, status=404)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    note = data.get('review_note') or data.get('reason')
    result, status = PurchaseReceivingService.review_correction(
        correction_id, request.user, action == 'approve', note,
    )
    if status < 400:
        audit(request, f'RECEIVING_CORRECTION_{action.upper()}',
              target_type='PurchaseReceivingCorrection', target_id=correction_id,
              metadata={'reason': note})
    return JsonResponse(result, status=status)
