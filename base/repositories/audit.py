from base.repositories.base import BaseSyncRepository
from base.models import AuditLog


class AuditLogRepository(BaseSyncRepository):
    model = AuditLog

    @classmethod
    def filter_logs(cls, action=None, actor_id=None, target_type=None,
                    target_id=None, date_from=None, date_to=None):
        qs = cls.model.objects.filter(is_deleted=False).select_related('actor')
        if action:
            qs = qs.filter(action=action)
        if actor_id:
            qs = qs.filter(actor_id=actor_id)
        if target_type:
            qs = qs.filter(target_type=target_type)
        if target_id is not None:
            qs = qs.filter(target_id=target_id)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        return qs.order_by('-created_at')
