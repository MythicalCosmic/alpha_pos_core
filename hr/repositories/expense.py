from django.db.models import Sum
from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from hr.models import Expense


class ExpenseRepository(BaseSyncRepository):
    model = Expense

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'category', 'created_by', 'approved_by', 'paid_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(
            status=status, is_deleted=False
        )

    @classmethod
    def filter_by_category(cls, category_id):
        return cls.model.objects.filter(
            category_id=category_id, is_deleted=False
        )

    @classmethod
    def filter_by_date_range(cls, start, end):
        return cls.model.objects.filter(
            expense_date__range=(start, end), is_deleted=False
        )

    @classmethod
    def get_stats(cls, start_date, end_date):
        qs = cls.model.objects.filter(
            expense_date__range=(start_date, end_date),
            is_deleted=False,
        )
        by_status = dict(
            qs.values_list('status')
            .annotate(total=Sum('amount'))
        )
        by_category = dict(
            qs.values_list('category__name')
            .annotate(total=Sum('amount'))
        )
        return {
            'by_status': by_status,
            'by_category': by_category,
            'total': qs.aggregate(total=Sum('amount'))['total'] or 0,
            'count': qs.count(),
        }

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator
