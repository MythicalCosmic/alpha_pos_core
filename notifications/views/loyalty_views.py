"""Admin / cashier endpoints for the loyalty engine.

Two surfaces:
  - settings_view: admins tune thresholds (per-order, per-reward, label)
  - account_view / redeem_view: cashiers look up a customer by phone and
    redeem stamps at the till when a reward is claimed

Accrual is automatic via the OrderService hook; nothing in here mutates
balance except redeem. Lookups are by digits-only phone (we strip a single
leading '+') so cashier-typed and Telegram-sourced numbers both find the
same row.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from base.helpers.request import parse_json_body
from base.security.auth import login_required, role_required
from base.security.audit import audit
from base.security.idempotency import idempotent
from base.security.permissions import admin_required
from base.security.rate_limit import rate_limit, rate_limit_by
from base.models import AuditLog, Customer
from base.services.branch_scope import resolve_actor_branch
from notifications.models import LoyaltyAccount, LoyaltySettings
from notifications.services import loyalty_service


def _serialize_settings(s):
    return {
        'is_enabled': s.is_enabled,
        'stamps_per_completed_order': s.stamps_per_completed_order,
        'stamps_per_reward': s.stamps_per_reward,
        'reward_description': s.reward_description,
    }


def _customer_identity(phone, actor):
    """Return one exact branch-authorized Customer match.

    Legacy duplicate phone rows are ambiguous, so fail closed instead of
    guessing which customer owns the loyalty account.
    """
    branch_id = resolve_actor_branch(actor)
    phone = Customer.normalize_phone(phone)
    if not branch_id or not phone:
        return None
    matches = list(
        Customer.objects.filter(
            branch_id=branch_id,
            phone_number=phone,
            is_deleted=False,
        )
        .order_by("id")
        .values("id", "name")[:2]
    )
    return matches[0] if len(matches) == 1 else None


def _customer_identities(accounts, actor):
    """Batch the list endpoint's exact phone matches without an N+1 query."""
    branch_id = resolve_actor_branch(actor)
    phones = {
        Customer.normalize_phone(account.phone_number)
        for account in accounts
        if Customer.normalize_phone(account.phone_number)
    }
    if not branch_id or not phones:
        return {}
    candidates = Customer.objects.filter(
        branch_id=branch_id,
        phone_number__in=phones,
        is_deleted=False,
    ).order_by("id").values("id", "name", "phone_number")
    grouped = {}
    for customer in candidates:
        grouped.setdefault(customer["phone_number"], []).append(customer)
    return {
        phone: rows[0]
        for phone, rows in grouped.items()
        if len(rows) == 1
    }


def _serialize_account(a, actor=None, customer=None):
    if actor is not None:
        customer = _customer_identity(a.phone_number, actor)
    return {
        'phone_number': a.phone_number,
        'stamps_balance': a.stamps_balance,
        'stamps_earned_total': a.stamps_earned_total,
        'stamps_redeemed_total': a.stamps_redeemed_total,
        'customer_id': customer['id'] if customer else None,
        'customer_name': customer['name'] if customer else None,
        'created_at': a.created_at.isoformat(),
        'updated_at': a.updated_at.isoformat(),
    }


@csrf_exempt
@require_http_methods(['GET', 'PUT'])
@admin_required
def settings_view(request):
    s = LoyaltySettings.load()
    if request.method == 'GET':
        return JsonResponse({'success': True, 'data': _serialize_settings(s)})

    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])

    allowed = {
        'is_enabled', 'stamps_per_completed_order',
        'stamps_per_reward', 'reward_description',
    }
    for key in allowed & set(data.keys()):
        value = data[key]
        # Reject zero / negative thresholds — they'd silently disable
        # accrual or make redemption free.
        if key in {'stamps_per_completed_order', 'stamps_per_reward'}:
            if not isinstance(value, int) or value <= 0:
                return JsonResponse(
                    {'success': False, 'message': f'{key} must be a positive integer'},
                    status=422,
                )
        setattr(s, key, value)
    s.save()
    return JsonResponse({'success': True, 'data': _serialize_settings(s)})


@require_GET
@login_required
@role_required('ADMIN', 'CASHIER')
# Same caps as redeem_view: bound per-IP lookups and per-phone probes so a
# stolen cashier session can't enumerate which phone numbers have accounts.
@rate_limit('loyalty_account', 20, 60)
@rate_limit_by('loyalty_account_phone', 3, 300, lambda r: r.resolver_match.kwargs.get('phone') if r.resolver_match else None)
def account_view(request, phone):
    account = loyalty_service.get_account(phone)
    if not account:
        return JsonResponse(
            {'success': False, 'message': 'No loyalty account for that phone'},
            status=404,
        )
    return JsonResponse({
        'success': True,
        'data': _serialize_account(account, request.user),
    })


@csrf_exempt
@require_POST
@login_required
@role_required('ADMIN', 'CASHIER')
# Cap redemptions per cashier IP and per phone to make balance-draining
# from a stolen cashier session loud (lots of 429s) and slow.
@rate_limit('loyalty_redeem', 20, 60)
@rate_limit_by('loyalty_redeem_phone', 3, 300, lambda r: r.resolver_match.kwargs.get('phone') if r.resolver_match else None)
@idempotent('loyalty.redeem')
def redeem_view(request, phone):
    settings = LoyaltySettings.load()
    if not settings.is_enabled:
        return JsonResponse(
            {'success': False, 'message': 'Loyalty is disabled'},
            status=409,
        )
    # Snapshot the pre-redeem balance for the audit row so a stamp dispute
    # can be reconstructed against the cashier session that performed it.
    before = loyalty_service.get_account(phone)
    stamps_before = before.stamps_balance if before else None

    account = loyalty_service.redeem(
        phone, cashier_id=getattr(getattr(request, 'user', None), 'id', None),
    )
    if not account:
        return JsonResponse(
            {
                'success': False,
                'message': 'Not enough stamps or no account',
            },
            status=409,
        )
    audit(
        request,
        AuditLog.Action.LOYALTY_REDEEM,
        target_type='LoyaltyAccount',
        target_id=account.pk,
        metadata={
            'phone': phone,
            'stamps_before': stamps_before,
            'stamps_after': account.stamps_balance,
            'stamps_per_reward': settings.stamps_per_reward,
        },
    )
    return JsonResponse({
        'success': True,
        'data': _serialize_account(account, request.user),
    })


@require_GET
@admin_required
def list_accounts(request):
    accounts = list(
        LoyaltyAccount.objects.order_by('-stamps_balance')[:100]
    )
    customers = _customer_identities(accounts, request.user)
    return JsonResponse({
        'success': True,
        'data': [
            _serialize_account(
                account,
                customer=customers.get(
                    Customer.normalize_phone(account.phone_number)
                ),
            )
            for account in accounts
        ],
    })
