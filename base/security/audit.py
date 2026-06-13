import logging

from base.helpers.request import get_client_ip
from base.models import AuditLog

logger = logging.getLogger(__name__)


def audit(request, action, *, target_type='', target_id=None, metadata=None):
    """Write an AuditLog row from a view, swallowing any error.

    Audit logging must never break the business flow it observes. The caller
    has already completed the underlying action; we only annotate the trail.
    """
    try:
        return AuditLog.record(
            actor=getattr(request, 'user', None),
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
            ip_address=get_client_ip(request) if request else '',
        )
    except Exception:
        logger.exception(
            'audit log write failed (action=%s target=%s:%s)',
            action, target_type, target_id,
        )
        return None
