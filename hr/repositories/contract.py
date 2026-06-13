from datetime import timedelta

from django.core.paginator import Paginator
from django.utils import timezone

from base.repositories.base import BaseSyncRepository
from hr.models import EmployeeContract


class ContractRepository(BaseSyncRepository):
    model = EmployeeContract

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user', 'renewed_from', 'created_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(
            status=EmployeeContract.Status.ACTIVE, is_deleted=False
        )

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
            status=EmployeeContract.Status.ACTIVE,
            end_date__range=(today, end),
            is_deleted=False,
        )

    @classmethod
    def get_expired(cls):
        today = timezone.now().date()
        return cls.model.objects.filter(
            status=EmployeeContract.Status.ACTIVE,
            end_date__lt=today,
            is_deleted=False,
        )

    @classmethod
    def get_by_contract_number(cls, contract_number):
        return cls.model.objects.filter(
            contract_number=contract_number, is_deleted=False
        ).first()

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
