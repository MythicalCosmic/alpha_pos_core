import calendar

from django.core.paginator import Paginator

from base.repositories.base import BaseSyncRepository
from hr.models import LeaveRequest

from datetime import date


class LeaveRequestRepository(BaseSyncRepository):
    model = LeaveRequest

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user', 'leave_type', 'approved_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_employee(cls, employee_id):
        return cls.model.objects.filter(
            employee_id=employee_id, is_deleted=False
        )

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        )

    @classmethod
    def filter_by_date_range(cls, start, end):
        return cls.model.objects.filter(
            start_date__lte=end,
            end_date__gte=start,
            is_deleted=False,
        )

    @classmethod
    def get_calendar(cls, year, month):
        month_start = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        month_end = date(year, month, last_day)
        return cls.model.objects.filter(
            status=LeaveRequest.Status.APPROVED,
            start_date__lte=month_end,
            end_date__gte=month_start,
            is_deleted=False,
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
