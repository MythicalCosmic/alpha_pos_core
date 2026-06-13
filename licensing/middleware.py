"""Kill switch — refuses non-allowlisted requests when the license is
unregistered / suspended / expired / offline-grace-exceeded.

Wired at position 1 in MIDDLEWARE (right after corsheaders so 503
responses still carry CORS headers). The position is asserted at boot
in licensing/apps.py.

Hot path: one cache hit per request via `services/state.get_state()`.
DB is consulted only on cache miss (60s TTL) or when the heartbeat
daemon explicitly busts the cache.
"""
import logging

from django.conf import settings
from django.http import JsonResponse

from licensing.services.state import get_state


logger = logging.getLogger(__name__)


# Paths that must work even when the license is dead — otherwise the
# operator can never run setup and the renderer can't show a banner.
# /healthz must stay open for container health probes.
ALLOWLIST_EXACT = frozenset({
    '/healthz',
})

ALLOWLIST_PREFIXES = (
    '/api/licensing/',
)

# Paths that must NOT be processed when unlicensed, but must still return 200
# rather than the 503 kill-switch body. The Telegram webhook treats any
# non-200 as "retry this update" and will re-deliver for hours, building an
# ever-growing backlog that hammers the host. We ack with 200 and silently
# drop the update (no order is created while the license is dead).
ACK_WHEN_BLOCKED_EXACT = frozenset({
    '/api/telegram/webhook/',
})


def _is_allowlisted(path: str) -> bool:
    if path in ALLOWLIST_EXACT:
        return True
    return any(path.startswith(p) for p in ALLOWLIST_PREFIXES)


class LicenseEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Development bypass: when LICENSE_DEV_BYPASS is on (only possible with
        # DEBUG=True — see settings), let everything through with no license /
        # heartbeat / payment. Hard-gated on DEBUG in settings, so a shipped
        # production build can't honor it. Logged once per process so it's
        # never silently disabling the kill switch.
        if getattr(settings, 'LICENSE_DEV_BYPASS', False):
            if not getattr(self, '_bypass_warned', False):
                logger.warning(
                    'LICENSE_DEV_BYPASS is active — license kill switch is '
                    'DISABLED. Development only; never ship with this on.'
                )
                self._bypass_warned = True
            return self.get_response(request)

        # CORS preflight must always pass — otherwise the browser never
        # gets to even send the real request and see our 503 body. CORS
        # headers are added by corsheaders (which runs before us); this
        # is just a no-op so we don't drain license state on OPTIONS.
        if request.method == 'OPTIONS':
            return self.get_response(request)

        if _is_allowlisted(request.path):
            return self.get_response(request)

        state = get_state()
        if state.is_blocked():
            # The Telegram webhook must be acked with 200 even when blocked,
            # or Telegram retry-storms the host. Drop the update unprocessed.
            if request.path in ACK_WHEN_BLOCKED_EXACT:
                logger.info(
                    'license kill switch: acking %s with 200 no-op (status=%s)',
                    request.path, state.status,
                )
                return JsonResponse({'ok': True})
            return self._refuse(request, state)

        return self.get_response(request)

    def _refuse(self, request, state):
        # Log every block so support has a trail. Use INFO level — this is
        # expected behavior, not an error.
        logger.info(
            'license kill switch refused %s %s (status=%s reason=%s)',
            request.method, request.path, state.status, state.reason_code(),
        )
        body = {
            'success': False,
            'code': state.reason_code(),
            'status': state.status,
            'message': self._user_message(state),
            'tenant': {
                'org_name': state.org_name or None,
                'email': state.email or None,
            },
        }
        # The control center can push a banner the operator should see —
        # surface it in the refusal so the client can render it on its
        # error screen too.
        if state.message:
            body['banner'] = state.message
        return JsonResponse(body, status=503)

    @staticmethod
    def _user_message(state) -> str:
        if state.status == 'UNREGISTERED':
            return (
                'This POS install is not registered. The operator must '
                'complete setup at /api/licensing/setup before any '
                'business endpoint will respond.'
            )
        if state.status == 'SUSPENDED':
            return (
                'This license has been suspended. Contact your POS vendor '
                'to restore service.'
            )
        if state.status == 'EXPIRED':
            return (
                'This subscription has expired. Contact your POS vendor '
                'to renew.'
            )
        return (
            'This POS install cannot reach its control center and the '
            'offline grace window has been exceeded. Please check the '
            'internet connection.'
        )
