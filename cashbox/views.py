from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.idempotency import idempotent
from base.security.audit import audit
from base.security.permissions import (
    backoffice_permission_required,
    backoffice_required,
    permission_denied_response,
)
from base.models import AuditLog
from base.services.branch_scope import resolve_actor_branch
from cashbox.services.expense_service import CashboxExpenseService, CashboxCategoryService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_permission_required('expense.direct.pay')
@idempotent(
    'cashbox.expense.direct',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def cashbox_expenses(request, shift_id):
    if request.method == "GET":
        result, status_code = CashboxExpenseService.list_for_shift(
            shift_id, actor=request.user,
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = CashboxExpenseService.create(
        shift_id=shift_id,
        amount=data.get("amount"),
        category_id=data.get("category_id"),
        comment=data.get("comment", ""),
        recipient_user_id=data.get("recipient_user_id"),
        recipient_supplier_id=data.get("recipient_supplier_id"),
        actor=request.user,
        command_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def cashbox_categories(request):
    permission = (
        'expense.category.view' if request.method == 'GET'
        else 'expense.category.manage'
    )
    if denied := permission_denied_response(request, permission):
        return denied
    if request.method == "GET":
        result, status_code = CashboxCategoryService.list()
        return JsonResponse(result, status=status_code)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = CashboxCategoryService.create(
        name=data.get("name", ""),
        sort_order=data.get("sort_order", 0),
        reporting_group=data.get("reporting_group", "REVIEW"),
        code=data.get('code'),
        allowed_sources=data.get('allowed_sources'),
        actor=request.user,
    )
    if result.get('success'):
        category = result['data']
        audit(
            request,
            AuditLog.Action.EXPENSE_CATEGORY_CREATE,
            target_type='ExpenseCategory',
            target_id=category['id'],
            metadata={
                'code': category['code'],
                'name': category['name'],
                'compatibility_route': 'cashbox',
            },
        )
    return JsonResponse(result, status=status_code)


@require_GET
@backoffice_permission_required('expense.direct.pay')
def recipient_search(request):
    """Combined autocomplete over users (staff) and suppliers for the cashbox
    expense recipient field. Returns two grouped lists."""
    from base.models import User
    from stock.models import Supplier
    branch = str(resolve_actor_branch(request.user) or '').strip()
    if not branch:
        return JsonResponse(
            {'success': False, 'message': 'User has no branch ownership'},
            status=403,
        )
    q = (request.GET.get("q") or "").strip()
    users_qs = User.objects.filter(
        is_deleted=False, status='ACTIVE', branch_id=branch,
    )
    suppliers_qs = Supplier.objects.filter(
        is_deleted=False, is_active=True, branch_id=branch,
    )
    if q:
        users_qs = users_qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q))
        suppliers_qs = suppliers_qs.filter(
            Q(name__icontains=q) | Q(phone__icontains=q))
    return JsonResponse({"success": True, "data": {
        "users": [{
            "id": u.id,
            "name": f"{u.first_name} {u.last_name}".strip(),
            "role": u.role,
        } for u in users_qs[:15]],
        "suppliers": [{
            "id": s.id, "name": s.name, "balance": str(s.current_balance),
        } for s in suppliers_qs[:15]],
    }})
