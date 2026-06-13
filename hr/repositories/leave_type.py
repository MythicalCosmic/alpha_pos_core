from base.repositories.base import BaseSyncRepository
from hr.models import LeaveType


class LeaveTypeRepository(BaseSyncRepository):
    model = LeaveType

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def name_exists(cls, name, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()
