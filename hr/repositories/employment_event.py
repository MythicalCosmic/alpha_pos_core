from django.core.paginator import Paginator

from base.repositories.base import BaseSyncRepository
from hr.models import EmploymentEvent


class EmploymentEventRepository(BaseSyncRepository):
    model = EmploymentEvent

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user', 'created_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_timeline(cls, employee_id):
        return cls.model.objects.filter(
            employee_id=employee_id, is_deleted=False
        ).order_by('-event_date')

    @classmethod
    def filter_by_type(cls, event_type):
        return cls.model.objects.filter(
            event_type=event_type, is_deleted=False
        )

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
