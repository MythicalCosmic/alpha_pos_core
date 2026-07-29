from django.db.models import Sum
from base.repositories.base import BaseSyncRepository
from hr.models import CashTransaction


class CashTransactionRepository(BaseSyncRepository):
    model = CashTransaction

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'performed_by', 'approved_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_type(cls, type):
        return cls.model.objects.filter(
            type=type, is_deleted=False
        )

    @classmethod
    def filter_by_date_range(cls, start, end):
        return cls.model.objects.filter(
            created_at__range=(start, end), is_deleted=False
        )

    @classmethod
    def get_balance_summary(cls, start, end):
        qs = cls.model.objects.filter(
            created_at__range=(start, end), is_deleted=False
        )
        return dict(
            qs.values_list('type')
            .annotate(total=Sum('amount'))
        )
