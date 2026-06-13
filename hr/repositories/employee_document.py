from datetime import timedelta

from django.core.paginator import Paginator
from django.utils import timezone

from base.repositories.base import BaseSyncRepository
from hr.models import EmployeeDocument


class EmployeeDocumentRepository(BaseSyncRepository):
    model = EmployeeDocument

    @classmethod
    def get_for_employee(cls, employee_id):
        return cls.model.objects.filter(
            employee_id=employee_id, is_deleted=False
        )

    @classmethod
    def get_expiring(cls, days):
        today = timezone.now().date()
        end = today + timedelta(days=days)
        return cls.model.objects.filter(
            expiry_date__range=(today, end),
            is_deleted=False,
        )

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user', 'verified_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
