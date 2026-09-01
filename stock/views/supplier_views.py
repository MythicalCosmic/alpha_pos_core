from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.http_validation import (
    QueryValidationError,
    iso_date,
    optional_int,
    positive_int,
)
from base.security.idempotency import idempotent
from base.security.audit import audit
from base.security.permissions import (
    backoffice_permission_required,
    backoffice_required,
    permission_denied_response,
)
from base.services.branch_scope import resolve_actor_branch
from base.models import AuditLog
from stock.services import SupplierService, SupplierStockItemService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def suppliers(request):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.supplier.view'):
            return denied
        result, status_code = SupplierService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            search=request.GET.get("search"),
            active_only=request.GET.get("active_only", "true").lower() == "true",
            branch_id=resolve_actor_branch(request.user),
        )
        return JsonResponse(result, status=status_code)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = SupplierService.create(
        **data,
        branch_id=resolve_actor_branch(request.user),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@backoffice_required
def supplier_detail(request, supplier_id):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.supplier.view'):
            return denied
        result, status_code = SupplierService.get(
            supplier_id,
            branch_id=resolve_actor_branch(request.user),
        )
        return JsonResponse(result, status=status_code)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    if request.method == "DELETE":
        result, status_code = SupplierService.deactivate(
            supplier_id,
            branch_id=resolve_actor_branch(request.user),
        )
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = SupplierService.update(
        supplier_id,
        branch_id=resolve_actor_branch(request.user),
        **data,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def supplier_items(request, supplier_id):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.supplier.view'):
            return denied
        result, status_code = SupplierService.get(
            supplier_id,
            include_items=True,
            include_stats=False,
            branch_id=resolve_actor_branch(request.user),
        )
        return JsonResponse(result, status=status_code)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    from stock.models import Supplier
    if not Supplier.objects.filter(
        pk=supplier_id,
        branch_id=resolve_actor_branch(request.user),
        is_deleted=False,
    ).exists():
        return JsonResponse(
            {'success': False, 'message': 'Supplier not found'},
            status=404,
        )
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = SupplierStockItemService.add_item(supplier_id=supplier_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["POST"])
@backoffice_permission_required('stock.supplier.pay')
@idempotent(
    'stock.supplier.payment',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def supplier_pay(request, supplier_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    from stock.services.supplier_ledger_service import SupplierLedgerService
    result, status_code = SupplierLedgerService.pay_supplier(
        supplier_id=supplier_id,
        amount_uzs=data.get('amount_uzs', data.get('amount')),
        source_account=data.get('source_account'),
        fee_uzs=data.get(
            'fee_uzs', data.get('commission', data.get('fee', 0))
        ),
        allocation_mode=data.get('allocation_mode', 'AUTO_OLDEST_DUE'),
        allocations=data.get('allocations'),
        note=data.get("note", ""),
        performed_by=request.user,
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
        request_hash=getattr(request, 'idempotency_request_hash', ''),
    )
    if result.get('success'):
        payment = result['data']
        audit(
            request,
            AuditLog.Action.SUPPLIER_PAYMENT,
            target_type='SupplierPayment',
            target_id=payment['payment_id'],
            metadata={
                'supplier_id': supplier_id,
                'principal_uzs': payment['principal_uzs'],
                'fee_uzs': payment['fee_uzs'],
                'source_account': payment['source_account'],
                'treasury_transaction_id': payment['treasury_transaction_id'],
                'supplier_transaction_id': payment['supplier_transaction_id'],
            },
        )
    return JsonResponse(result, status=status_code)


@require_http_methods(["GET"])
@backoffice_permission_required('stock.supplier.balance.view')
def supplier_payment_detail(request, supplier_id, payment_id):
    from stock.services.supplier_ledger_service import SupplierPaymentService

    result, status_code = SupplierPaymentService.get(
        payment_id,
        supplier_id=supplier_id,
        actor=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["POST"])
@backoffice_permission_required('stock.supplier.pay')
@idempotent(
    'stock.supplier.payment.reverse',
    required=True,
    expose_action_id=True,
    recover_inflight_after_seconds=5,
)
def supplier_payment_reverse(request, supplier_id, payment_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    from stock.services.supplier_ledger_service import SupplierPaymentService

    result, status_code = SupplierPaymentService.reverse(
        payment_id,
        supplier_id=supplier_id,
        actor=request.user,
        reason=data.get('reason'),
        action_id=getattr(request, 'idempotency_action_id', None),
        idempotency_key=getattr(request, 'idempotency_key', ''),
    )
    if result.get('success'):
        payment = result['data']
        audit(
            request,
            AuditLog.Action.SUPPLIER_PAYMENT_REVERSE,
            target_type='SupplierPayment',
            target_id=payment_id,
            metadata={
                'supplier_id': supplier_id,
                'principal_uzs': payment['principal_uzs'],
                'fee_uzs': payment['fee_uzs'],
                'source_account': payment['source_account'],
                'reason': data.get('reason'),
            },
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET"])
@backoffice_permission_required('stock.supplier.balance.view')
def supplier_ledger(request, supplier_id):
    try:
        page = positive_int(request.GET, 'page', 1)
        per_page = positive_int(request.GET, 'per_page', 25, maximum=100)
        date_from = iso_date(request.GET, 'date_from')
        date_to = iso_date(request.GET, 'date_to')
        reference_id = optional_int(request.GET, 'reference_id')
    except QueryValidationError as exc:
        return JsonResponse({
            'success': False,
            'code': 'FILTER_VALIDATION_ERROR',
            'message': 'One or more filters are invalid.',
            'errors': exc.errors,
        }, status=422)
    from stock.services.supplier_ledger_service import SupplierLedgerService
    result, status_code = SupplierLedgerService.history(
        supplier_id,
        page=page,
        per_page=per_page,
        actor=request.user,
        date_from=date_from,
        date_to=date_to,
        transaction_type=request.GET.get('type') or request.GET.get('transaction_type'),
        source_account=request.GET.get('source_account'),
        reference_type=request.GET.get('reference_type'),
        reference_id=reference_id,
        search=request.GET.get('search'),
        source_reference=request.GET.get('source_reference'),
    )
    return JsonResponse(result, status=status_code)
