from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import ServiceResponse, json_response
from base.security.idempotency import idempotent
from base.security.audit import audit
from base.security.permissions import (
    backoffice_required,
    permission_denied_response,
    user_has_permission,
)
from hr.services import ExpenseCategoryService, ExpenseService
from base.models import AuditLog
from hr.views.filters import (
    query_bool,
    query_date_range,
    query_enum,
    query_int,
    query_value,
)


def _scope(request):
    if user_has_permission(request.user, 'expense.request.view_all'):
        return True, None
    if user_has_permission(request.user, 'expense.request.view_own'):
        return False, None
    return False, permission_denied_response(request, 'expense.request.view_own')


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def expense_categories(request):
    permission = (
        'expense.category.view' if request.method == 'GET'
        else 'expense.category.manage'
    )
    if denied := permission_denied_response(request, permission):
        return denied
    if request.method == 'GET':
        result, status = ExpenseCategoryService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            search=query_value(request, 'search'),
            is_active=query_bool(request, 'is_active', 'status'),
            parent_id=query_int(request, 'parent_id'),
            cost_behavior=query_value(request, 'cost_behavior'),
            roots_only=bool(query_bool(request, 'roots_only')),
            actor=request.user,
        )
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    allowed = {
        'name', 'code', 'description', 'budget_limit', 'is_active',
        'reporting_group', 'parent_id', 'cost_behavior', 'sort_order',
        'allowed_sources', 'requires_receipt', 'requires_description',
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        return json_response(ServiceResponse.validation_error({
            field: ['Unknown field.'] for field in unknown
        }))
    result, status = ExpenseCategoryService.create(actor=request.user, **data)
    if result.get('success'):
        category = result['data']['category']
        audit(
            request,
            AuditLog.Action.EXPENSE_CATEGORY_CREATE,
            target_type='ExpenseCategory',
            target_id=category['id'],
            metadata={
                'code': category['code'],
                'name': category['name'],
                'compatibility_route': 'hr',
            },
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'PUT', 'PATCH', 'DELETE'])
@backoffice_required
def expense_category_detail(request, category_id):
    permission = (
        'expense.category.view' if request.method == 'GET'
        else 'expense.category.manage'
    )
    if denied := permission_denied_response(request, permission):
        return denied
    if request.method == 'GET':
        result, status = ExpenseCategoryService.get(
            category_id,
            actor=request.user,
        )
    elif request.method == 'DELETE':
        result, status = ExpenseCategoryService.deactivate(
            category_id,
            actor=request.user,
        )
        if result.get('success'):
            audit(
                request,
                AuditLog.Action.EXPENSE_CATEGORY_DEACTIVATE,
                target_type='ExpenseCategory',
                target_id=category_id,
                metadata={'compatibility_route': 'hr'},
            )
    else:
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
        result, status = ExpenseCategoryService.update(
            category_id,
            actor=request.user,
            **data,
        )
        if result.get('success'):
            audit(
                request,
                AuditLog.Action.EXPENSE_CATEGORY_UPDATE,
                target_type='ExpenseCategory',
                target_id=category_id,
                metadata={
                    'changed_fields': sorted(data),
                    'compatibility_route': 'hr',
                },
            )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@backoffice_required
def expenses(request):
    if request.method == 'POST':
        if denied := permission_denied_response(request, 'expense.request.create'):
            return denied
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
        result, status = ExpenseService.create(actor=request.user, **data)
        return JsonResponse(result, status=status)
    view_all, denied = _scope(request)
    if denied:
        return denied
    date_from, date_to = query_date_range(request)
    result, status = ExpenseService.list(
        page=safe_page(request),
        per_page=safe_per_page(request, 20),
        status=query_enum(request, 'status'),
        category_id=query_int(request, 'category_id', 'category', 'type'),
        category_parent_id=query_int(request, 'category_parent_id'),
        include_subcategories=bool(query_bool(request, 'include_subcategories')),
        cost_behavior=query_value(request, 'cost_behavior'),
        reporting_group=query_value(request, 'reporting_group'),
        source_account=query_value(request, 'source_account', 'requested_source'),
        date_from=date_from,
        date_to=date_to,
        search=query_value(request, 'search'),
        actor=request.user,
        view_all=view_all,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent(
    'expense.request.reclassify.compat',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def expense_reclassify(request):
    for permission in ('expense.category.manage', 'expense.request.approve'):
        if denied := permission_denied_response(request, permission):
            return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    allowed = {
        'expense_ids', 'category_id', 'expected_category_id',
        'reason', 'dry_run',
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        return json_response(ServiceResponse.validation_error({
            field: ['Unknown field.'] for field in unknown
        }))
    result, status = ExpenseService.reclassify_pending(
        expense_ids=data.get('expense_ids'),
        category_id=data.get('category_id'),
        expected_category_id=data.get('expected_category_id'),
        reason=data.get('reason', ''),
        dry_run=data.get('dry_run', True),
        actor=request.user,
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    result.get('data', {}).get('reclassification', {}).pop(
        '_applied_now', None,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['GET', 'PUT', 'DELETE'])
@backoffice_required
def expense_detail(request, expense_id):
    view_all, denied = _scope(request)
    if denied:
        return denied
    if request.method == 'GET':
        result, status = ExpenseService.get(
            expense_id,
            actor=request.user,
            view_all=view_all,
        )
    elif request.method == 'DELETE':
        result, status = ExpenseService.cancel(
            expense_id,
            actor=request.user,
            can_approve=user_has_permission(request.user, 'expense.request.approve'),
            reason='Canceled via compatibility route',
        )
    else:
        if denied := permission_denied_response(request, 'expense.request.create'):
            return denied
        data, error = parse_json_body(request)
        if error:
            return json_response(error)
        result, status = ExpenseService.update(
            expense_id,
            actor=request.user,
            view_all=view_all,
            **data,
        )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def expense_approve(request, expense_id):
    if denied := permission_denied_response(request, 'expense.request.approve'):
        return denied
    result, status = ExpenseService.approve(expense_id, actor=request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def expense_reject(request, expense_id):
    if denied := permission_denied_response(request, 'expense.request.approve'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = ExpenseService.reject(
        expense_id,
        actor=request.user,
        reason=data.get('reason') or data.get('notes'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
@idempotent(
    'expense.request.pay.compat',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def expense_pay(request, expense_id):
    if denied := permission_denied_response(request, 'expense.request.pay'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    source = data.get('source_account')
    if not source and data.get('payment_method'):
        source = (
            'DRAWER' if data['payment_method'] == 'CASH' else 'BANK'
        )
    result, status = ExpenseService.pay(
        expense_id,
        actor=request.user,
        source_account=source,
        fee_uzs=data.get('fee_uzs'),
        fee_percent=data.get('fee_percent'),
        note=data.get('note', ''),
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    return JsonResponse(result, status=status)


@require_GET
@backoffice_required
def expense_stats(request):
    if denied := permission_denied_response(request, 'expense.request.view_all'):
        return denied
    result, status = ExpenseService.get_stats(actor=request.user)
    return JsonResponse(result, status=status)
