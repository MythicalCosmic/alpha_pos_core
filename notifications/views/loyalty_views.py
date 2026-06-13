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
from base.models import AuditLog
from notifications.models import LoyaltyAccount, LoyaltySettings
from notifications.services import loyalty_service


def _serialize_settings(s):
    return {
        'is_enabled': s.is_enabled,
        'stamps_per_completed_order': s.stamps_per_completed_order,
        'stamps_per_reward': s.stamps_per_reward,
        'reward_description': s.reward_description,
    }


def _serialize_account(a):
    return {
        'phone_number': a.phone_number,
        'stamps_balance': a.stamps_balance,
        'stamps_earned_total': a.stamps_earned_total,
        'stamps_redeemed_total': a.stamps_redeemed_total,
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
    return JsonResponse({'success': True, 'data': _serialize_account(account)})


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
    return JsonResponse({'success': True, 'data': _serialize_account(account)})


@require_GET
@admin_required
def list_accounts(request):
    accounts = LoyaltyAccount.objects.order_by('-stamps_balance')[:100]
    return JsonResponse({
        'success': True,
        'data': [_serialize_account(a) for a in accounts],
    })
