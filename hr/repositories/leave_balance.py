from base.repositories.base import BaseSyncRepository
from hr.models import LeaveBalance


class LeaveBalanceRepository(BaseSyncRepository):
    model = LeaveBalance

    @classmethod
    def get_for_employee(cls, employee_id, year):
        return cls.model.objects.filter(
            employee_id=employee_id, year=year, is_deleted=False
        ).select_related('leave_type')

    @classmethod
    def get_for_employee_and_type(cls, employee_id, leave_type_id, year):
        return cls.model.objects.filter(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            is_deleted=False,
        ).first()

    @classmethod
    def exists_for_period(cls, employee_id, leave_type_id, year):
        return cls.model.objects.filter(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            is_deleted=False,
        ).exists()
