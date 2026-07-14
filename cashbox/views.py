from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import pos_staff_required, admin_required
from cashbox.services.expense_service import CashboxExpenseService, CashboxCategoryService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@pos_staff_required
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
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def cashbox_categories(request):
    if request.method == "GET":
        result, status_code = CashboxCategoryService.list()
        return JsonResponse(result, status=status_code)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = CashboxCategoryService.create(
        name=data.get("name", ""), sort_order=data.get("sort_order", 0))
    return JsonResponse(result, status=status_code)


@require_GET
@pos_staff_required
def recipient_search(request):
    """Combined autocomplete over users (staff) and suppliers for the cashbox
    expense recipient field. Returns two grouped lists."""
    from base.models import User
    from stock.models import Supplier
    branch = str(request.user.branch_id or '').strip()
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
