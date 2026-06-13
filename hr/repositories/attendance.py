from django.core.paginator import Paginator

from base.repositories.base import BaseSyncRepository
from hr.models import Attendance


class AttendanceRepository(BaseSyncRepository):
    model = Attendance

    @classmethod
    def get_for_employee_date(cls, employee_id, date):
        return cls.model.objects.filter(
            employee_id=employee_id, date=date, is_deleted=False
        ).first()

    @classmethod
    def get_daily(cls, date):
        return cls.model.objects.filter(
            date=date, is_deleted=False
        ).select_related('employee__user')

    @classmethod
    def get_monthly(cls, employee_id, year, month):
        return cls.model.objects.filter(
            employee_id=employee_id,
            date__year=year,
            date__month=month,
            is_deleted=False,
        )

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
