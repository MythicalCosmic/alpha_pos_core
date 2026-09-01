from functools import wraps
from django.http import JsonResponse
from base.helpers.request import (
    SessionCredentialConflict,
    resolve_session_credential,
)
from base.repositories import SessionRepository
from base.security.auth import (
    _ua_matches,
    is_courier_identity,
    session_credential_conflict_response,
)


def _session_role_required(allowed_roles, denied_message):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                session_key, credential_source = resolve_session_credential(request)
            except SessionCredentialConflict:
                return session_credential_conflict_response()
            if not session_key:
                return JsonResponse(
                    {"success": False, "code": "AUTHENTICATION_REQUIRED", "message": "Authentication required"},
                    status=401,
                )
            session = SessionRepository.get_by_session_key(session_key)
            if not session or not session.user_id or session.user_id.is_deleted:
                return JsonResponse(
                    {"success": False, "code": "AUTHENTICATION_INVALID", "message": "Invalid or expired session"},
                    status=401,
                )
            if session.is_expired():
                SessionRepository.invalidate_cache(session_key)
                SessionRepository.delete(session)
                return JsonResponse(
                    {"success": False, "code": "AUTHENTICATION_INVALID", "message": "Invalid or expired session"},
                    status=401,
                )
            # Courier access tokens live in the shared Session table, but are
            # intentionally a different audience.  Checking the linked
            # Courier profile as well as the role closes a role-drift window:
            # changing a courier user back to CASHIER/MANAGER must not turn an
            # already-issued mobile bearer into a POS/admin bearer.
            if is_courier_identity(session.user_id):
                return JsonResponse(
                    {"success": False, "code": "SESSION_AUDIENCE_FORBIDDEN", "message": "Session audience not permitted"},
                    status=403,
                )
            if session.user_id.role not in allowed_roles:
                return JsonResponse(
                    {"success": False, "code": "PERMISSION_DENIED", "message": denied_message},
                    status=403,
                )
            if session.user_id.status != 'ACTIVE':
                return JsonResponse(
                    {"success": False, "code": "ACCOUNT_SUSPENDED", "message": "Account is suspended"},
                    status=403,
                )
            if not _ua_matches(session, request):
                return JsonResponse(
                    {"success": False, "code": "SESSION_CLIENT_MISMATCH", "message": "Session client mismatch"},
                    status=401,
                )
            request.user = session.user_id
            request.session_key = session_key
            request.session_credential_source = credential_source
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def admin_required(view_func):
    # Back-office only. Keep the roles editor on this.
    return _session_role_required(('ADMIN',), "Admin access required")(view_func)


def backoffice_required(view_func):
    """Authenticate active internal back-office users before permission checks.

    ADMIN retains its catalog-wide bypass. MANAGER and WAREHOUSE identities
    receive no implicit access here; each operational endpoint must additionally
    apply ``permission_required`` (or perform an equivalent method-specific
    permission check).
    """
    return _session_role_required(
        ('ADMIN', 'MANAGER', 'WAREHOUSE'), "Back-office access required"
    )(view_func)


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
                    {"success": False, "code": "AUTHENTICATION_REQUIRED", "message": "Authentication required"},
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
                    {"success": False, "code": "PERMISSION_DENIED", "message": "You don't have permission to perform this action"},
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def user_has_permission(user, permission):
    if not user:
        return False
    permissions = user.permissions if isinstance(user.permissions, list) else []
    return user.role == 'ADMIN' or '*' in permissions or permission in permissions


def permission_denied_response(request, permission):
    """Return ``None`` when allowed, otherwise the standard endpoint 403."""
    if user_has_permission(getattr(request, 'user', None), permission):
        return None
    return JsonResponse(
        {
            "success": False,
            "code": "PERMISSION_DENIED",
            "message": "You don't have permission to perform this action",
            "errors": {"permission": permission},
        },
        status=403,
    )


def backoffice_permission_required(*permissions):
    """Authenticate first, then enforce every named catalog permission."""
    def decorator(view_func):
        return backoffice_required(permission_required(*permissions)(view_func))
    return decorator
