from django.db.models import Q, Count
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from hr.models import Department


class DepartmentRepository(BaseSyncRepository):
    model = Department

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def name_exists(cls, name, exclude_id=None):
        qs = cls.model.objects.filter(name__iexact=name, is_deleted=False)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.exists()

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(name__icontains=query) |
            Q(description__icontains=query)
        )

    @classmethod
    def with_employee_count(cls, queryset):
        return queryset.annotate(employee_count=Count('employees'))

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
