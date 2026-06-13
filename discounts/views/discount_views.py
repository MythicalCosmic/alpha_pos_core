from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from base.security.audit import audit
from base.models import AuditLog
from discounts.services import DiscountTypeService, DiscountService


def _discount_meta(payload):
    """Pull the fraud-relevant subset of a discount payload for the audit
    trail. Skip prose fields (description, secret_word) — they bloat the row
    and the secret_word is, well, secret."""
    if not isinstance(payload, dict):
        return {}
    keep = ('code', 'discount_type_id', 'value', 'is_active', 'is_stackable',
            'usage_limit', 'usage_per_user', 'min_order_amount',
            'start_date', 'end_date')
    return {k: payload.get(k) for k in keep if k in payload}


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def discount_types(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        is_active = request.GET.get('is_active')
        if is_active is not None:
            is_active = is_active.lower() == 'true'

        result, status_code = DiscountTypeService.list(
            page=page, per_page=per_page, is_active=is_active,
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = DiscountTypeService.create(
        name=data.get('name', ''),
        code=data.get('code', ''),
        description=data.get('description', ''),
        discount_method=data.get('discount_method', 'PERCENTAGE'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def discount_type_detail(request, type_id):
    if request.method == "GET":
        result, status_code = DiscountTypeService.get(type_id)
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        result, status_code = DiscountTypeService.delete(type_id)
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = DiscountTypeService.update(type_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def discounts(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        discount_type_id = request.GET.get('discount_type_id')
        if discount_type_id:
            discount_type_id = int(discount_type_id)
        is_active = request.GET.get('is_active')
        if is_active is not None:
            is_active = is_active.lower() == 'true'
        search = request.GET.get('search')

        result, status_code = DiscountService.list(
            page=page, per_page=per_page,
            discount_type_id=discount_type_id,
            is_active=is_active, search=search,
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    data['created_by_id'] = request.user.id
    result, status_code = DiscountService.create(**data)
    if result.get('success'):
        created = (result.get('data') or {}).get('discount') or {}
        audit(
            request,
            AuditLog.Action.DISCOUNT_CREATE,
            target_type='Discount',
            target_id=created.get('id'),
            metadata=_discount_meta(data),
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def discount_detail(request, discount_id):
    if request.method == "GET":
        result, status_code = DiscountService.get(discount_id)
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        result, status_code = DiscountService.delete(discount_id)
        if result.get('success'):
            audit(
                request,
                AuditLog.Action.DISCOUNT_DELETE,
                target_type='Discount',
                target_id=discount_id,
            )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = DiscountService.update(discount_id, **data)
    if result.get('success'):
        audit(
            request,
            AuditLog.Action.DISCOUNT_UPDATE,
            target_type='Discount',
            target_id=discount_id,
            metadata=_discount_meta(data),
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def discount_toggle(request, discount_id):
    result, status_code = DiscountService.toggle(discount_id)
    if result.get('success'):
        new_state = (result.get('data') or {}).get('discount', {}).get('is_active')
        audit(
            request,
            AuditLog.Action.DISCOUNT_UPDATE,
            target_type='Discount',
            target_id=discount_id,
            metadata={'is_active': new_state, 'via': 'toggle'},
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
def discount_stats(request, discount_id):
    result, status_code = DiscountService.get_stats(discount_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def validate_discount(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    code = data.get('code', '')
    order_subtotal = data.get('order_subtotal', 0)
    # Evaluate the per-user redemption cap against the AUTHENTICATED user, not a
    # client-supplied id — otherwise the limit can be checked against an
    # attacker-chosen account and bypassed.
    user_id = request.user.id

    result, status_code = DiscountService.validate_code(
        code=code, order_subtotal=order_subtotal, user_id=user_id,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def apply_discount(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    order_id = data.get('order_id')
    discount_code = data.get('discount_code', '')

    if not order_id:
        return json_response(({
            "success": False,
            "message": "Missing order_id",
            "errors": {"order_id": "order_id is required"},
        }, 422))

    result, status_code = DiscountService.apply_to_order(
        order_id, discount_code, request.user.id,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def remove_discount(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    order_id = data.get('order_id')
    order_discount_id = data.get('order_discount_id')

    if not order_id or not order_discount_id:
        return json_response(({
            "success": False,
            "message": "Missing order_id or order_discount_id",
            "errors": {
                "order_id": "order_id is required",
                "order_discount_id": "order_discount_id is required",
            },
        }, 422))

    result, status_code = DiscountService.remove_from_order(
        order_id, order_discount_id, request.user.id,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
def validate_secret_word(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    word = data.get('word', '')
    order_id = data.get('order_id')

    if not word:
        return json_response(({
            "success": False,
            "message": "Missing word",
            "errors": {"word": "word is required"},
        }, 422))

    result, status_code = DiscountService.validate_secret_word(
        word, order_id, request.user.id,
    )
    return JsonResponse(result, status=status_code)
