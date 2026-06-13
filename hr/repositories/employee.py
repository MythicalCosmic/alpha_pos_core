from django.db.models import Q, Count
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from hr.models import Employee


class EmployeeRepository(BaseSyncRepository):
    model = Employee

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(
            is_deleted=False, is_active=True
        ).select_related('user', 'department')

    @classmethod
    def get_by_user(cls, user_id):
        return cls.model.objects.filter(
            user_id=user_id, is_deleted=False
        ).first()

    @classmethod
    def get_by_department(cls, department_id):
        return cls.model.objects.filter(
            department_id=department_id, is_deleted=False
        )

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'user', 'department'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def search(cls, queryset, query):
        return queryset.filter(
            Q(user__first_name__icontains=query) |
            Q(user__last_name__icontains=query) |
            Q(position__icontains=query)
        )

    @classmethod
    def has_employee_profile(cls, user_id):
        return cls.model.objects.filter(
            user_id=user_id, is_deleted=False
        ).exists()

    @classmethod
    def get_stats(cls):
        qs = cls.model.objects.filter(is_deleted=False)
        return {
            'total': qs.count(),
            'active': qs.filter(is_active=True).count(),
            'inactive': qs.filter(is_active=False).count(),
            'by_contract_type': dict(
                qs.filter(is_active=True)
                .values_list('contract_type')
                .annotate(count=Count('id'))
            ),
            'by_department': dict(
                qs.filter(is_active=True)
                .values_list('department__name')
                .annotate(count=Count('id'))
            ),
        }

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
