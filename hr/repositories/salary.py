from django.db.models import Sum
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from hr.models import SalaryPayment


class SalaryPaymentRepository(BaseSyncRepository):
    model = SalaryPayment

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'employee__user', 'approved_by', 'created_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_for_update(cls, pk):
        # Row-lock the salary so concurrent approve/pay calls serialize on the
        # status check and cannot double-debit the cash register.
        try:
            return cls.model.objects.select_for_update().get(
                pk=pk, is_deleted=False
            )
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_employee(cls, employee_id):
        return cls.model.objects.filter(
            employee_id=employee_id, is_deleted=False
        )

    @classmethod
    def filter_by_period(cls, year, month):
        return cls.model.objects.filter(
            period_year=year, period_month=month, is_deleted=False
        )

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        )

    @classmethod
    def exists_for_period(cls, employee_id, year, month):
        return cls.model.objects.filter(
            employee_id=employee_id,
            period_year=year,
            period_month=month,
            is_deleted=False,
        ).exists()

    @classmethod
    def get_payroll_summary(cls, year, month):
        qs = cls.model.objects.filter(
            period_year=year, period_month=month, is_deleted=False
        )
        return {
            **qs.aggregate(
                total_base=Sum('base_amount'),
                total_bonus=Sum('bonus'),
                total_deduction=Sum('deduction'),
                total_net=Sum('net_amount'),
            ),
            'count': qs.count(),
        }

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
