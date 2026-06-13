from functools import wraps
from django.http import JsonResponse
from base.helpers.request import get_session_key
from base.repositories import SessionRepository
from base.security.auth import _ua_matches


def _session_role_required(allowed_roles, denied_message):
    """Session-authenticating gate for /api/admins endpoints.

    Validates the session, then checks the user's role against
    `allowed_roles`. Sets request.user/request.session_key on success.
    `admin_required`, `manager_required` and `pos_staff_required` are thin
    wrappers that differ only in which roles they admit — this keeps the
    session-validation logic in one place.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            session_key = get_session_key(request)
            if not session_key:
                return JsonResponse(
                    {"success": False, "message": "Authentication required"},
                    status=401,
                )
            # Use the cached lookup with `select_related('user_id')` instead of
            # the unscoped `first(payload=…)` — saves 2 DB queries per admin
            # request (the session row and the lazy FK access to user_id below).
            session = SessionRepository.get_by_session_key(session_key)
            if not session or not session.user_id or session.user_id.is_deleted:
                return JsonResponse(
                    {"success": False, "message": "Invalid or expired session"},
                    status=401,
                )
            if session.is_expired():
                SessionRepository.invalidate_cache(session_key)
                SessionRepository.delete(session)
                return JsonResponse(
                    {"success": False, "message": "Invalid or expired session"},
                    status=401,
                )
            if session.user_id.role not in allowed_roles:
                return JsonResponse(
                    {"success": False, "message": denied_message},
                    status=403,
                )
            if session.user_id.status != 'ACTIVE':
                return JsonResponse(
                    {"success": False, "message": "Account is suspended"},
                    status=403,
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
    return decorator


def admin_required(view_func):
    # Back-office only. Keep the roles editor on this.
    return _session_role_required(('ADMIN',), "Admin access required")(view_func)


def manager_required(view_func):
    # POS management tier: ADMIN (back office) + MANAGER (in-app settings).
    # MANAGER logs in on the monoblock and runs Settings there; ADMIN can't
    # log into the POS but is admitted so back-office calls keep working.
    return _session_role_required(
        ('ADMIN', 'MANAGER'), "Manager access required"
    )(view_func)


def pos_staff_required(view_func):
    # Anyone operating the till: ADMIN + MANAGER + CASHIER. Used for the
    # manual start/end-shift actions a cashier performs on the POS.
    return _session_role_required(
        ('ADMIN', 'MANAGER', 'CASHIER'), "Staff access required"
    )(view_func)


def permission_required(*permissions):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not hasattr(request, 'user') or request.user is None:
                return JsonResponse(
                    {"success": False, "message": "Authentication required"},
                    status=401,
                )
            user_perms = request.user.permissions or []
            # Coerce non-list values to an empty list. JSONField will accept
            # whatever a writer hands it; a stray string like "***" would
            # otherwise grant wildcard via substring membership.
            if not isinstance(user_perms, list):
                user_perms = []
            if '*' in user_perms or request.user.role == 'ADMIN':
                return view_func(request, *args, **kwargs)
            missing = [p for p in permissions if p not in user_perms]
            if missing:
                return JsonResponse(
                    {"success": False, "message": "You don't have permission to perform this action"},
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
