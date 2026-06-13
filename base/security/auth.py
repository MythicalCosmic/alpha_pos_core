from functools import wraps
from django.http import JsonResponse
from base.helpers.request import get_session_key, get_user_agent
from base.repositories import SessionRepository


def _ua_matches(session, request) -> bool:
    """Return True if the request's UA matches the one bound at login.

    We compare the stored UA (truncated to 256 chars by the auth services)
    against the request's UA. A mismatch means the cookie/bearer is being
    replayed from a different client and the session must be rejected. We
    intentionally do not bind IP — POS waiters roam between Wi-Fi and LTE
    and that would lock them out on every network change.
    """
    # Direct comparison so the empty case is handled consistently rather than
    # fail-open: a client that sends no UA stores '' and keeps matching ''
    # (so legitimate no-UA clients aren't locked out), while a token replayed
    # from a client presenting a *different* UA is rejected. Previously an
    # empty stored UA short-circuited to True, letting a stolen token from an
    # empty-UA session skip the replay check entirely.
    stored = (session.user_agent or '').strip()
    return stored == get_user_agent(request).strip()


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        session_key = get_session_key(request)
        if not session_key:
            return JsonResponse(
                {"success": False, "message": "Authentication required"},
                status=401,
            )
        # Use the cached lookup with `select_related('user_id')` — `first()`
        # neither caches nor preloads the user FK, so this saves both a
        # repeat session lookup and a lazy user query per request.
        session = SessionRepository.get_by_session_key(session_key)
        if not session or not session.user_id or session.user_id.is_deleted:
            return JsonResponse(
                {"success": False, "message": "Invalid or expired session"},
                status=401,
            )
        # Suspended / inactive users must lose access even if their session
        # row is still alive. admin_required already enforces this; mirror it
        # here so non-admin roles (CASHIER, WAITER, USER) can't keep mutating
        # state with a cached cookie after being disabled.
        if session.user_id.status != 'ACTIVE':
            SessionRepository.invalidate_cache(session_key)
            SessionRepository.delete(session)
            return JsonResponse(
                {"success": False, "message": "Account is not active"},
                status=403,
            )
        if session.is_expired():
            SessionRepository.invalidate_cache(session_key)
            SessionRepository.delete(session)
            return JsonResponse(
                {"success": False, "message": "Invalid or expired session"},
                status=401,
            )
        if not _ua_matches(session, request):
            return JsonResponse(
                {"success": False, "message": "Session client mismatch"},
                status=401,
            )
        request.user = session.user_id
        request.session_key = session_key
        return view_func(request, *args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not hasattr(request, 'user') or request.user is None:
                return JsonResponse(
                    {"success": False, "message": "Authentication required"},
                    status=401,
                )
            if request.user.role not in roles:
                return JsonResponse(
                    {"success": False, "message": "Insufficient permissions"},
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
