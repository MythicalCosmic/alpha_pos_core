"""One policy boundary for waiter sessions on every POS route."""
from functools import wraps

from django.http import JsonResponse

from base.helpers.response import ServiceResponse
from base.models import AppSettings


def current_policy():
    # Read fresh: toggling access must not wait for a process-local cache expiry.
    policy = AppSettings.objects.filter(pk=1).first()
    return policy or AppSettings.load()


def authorize_waiter(user, permission=None):
    if user.role != 'WAITER':
        return None
    policy = current_policy()
    if not policy.waiter_enabled:
        return ({'success': False, 'code': 'WAITER_DISABLED',
                 'message': 'Waiter service is disabled.'}, 403)
    from django.conf import settings
    from base.services.branch_scope import resolve_actor_branch
    branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
    if getattr(settings, 'DEPLOYMENT_MODE', '') == 'local' and branch and resolve_actor_branch(user) != branch:
        return ServiceResponse.forbidden('You are not authorized for this branch')
    permissions = user.permissions if isinstance(user.permissions, list) else []
    if permission and permission not in permissions and '*' not in permissions:
        return ServiceResponse.forbidden('You do not have permission to perform this action')
    if permission == 'order.pay' and policy.waiter_payment_mode != AppSettings.WaiterPaymentMode.PERMITTED_WAITER:
        return ({'success': False, 'code': 'CASHIER_PAYMENT_REQUIRED',
                 'message': 'Request payment collection from a cashier.'}, 403)
    if permission == 'order.refund':
        return ServiceResponse.forbidden('A manager must authorize paid-order refunds')
    return None


def waiter_permission(permission=None):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            denied = authorize_waiter(request.user, permission)
            if denied:
                return JsonResponse(denied[0], status=denied[1])
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


def owns_order(order, user_id):
    if order.waiter_id is not None:
        return order.waiter_id == user_id
    # A rolling upgrade can safely recognize the original creator of a legacy
    # waiter ticket; payment may already have reassigned cashier_id.
    return order.user_id == user_id
