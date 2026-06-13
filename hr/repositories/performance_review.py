from django.core.paginator import Paginator

from base.repositories.base import BaseSyncRepository
from hr.models import PerformanceReview


class PerformanceReviewRepository(BaseSyncRepository):
    model = PerformanceReview

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user', 'reviewer'
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
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
