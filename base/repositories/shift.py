from django.core.paginator import Paginator
from base.repositories.base import BaseSyncRepository
from base.models import ShiftTemplate, Shift, CashReconciliation


class ShiftTemplateRepository(BaseSyncRepository):
    model = ShiftTemplate

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class ShiftRepository(BaseSyncRepository):
    model = Shift

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'user', 'shift_template'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_active_for_user(cls, user_id):
        return cls.model.objects.filter(
            is_deleted=False,
            user_id=user_id,
            status='ACTIVE',
            end_time__isnull=True,
        ).first()

    @classmethod
    def filter_by_status(cls, status):
        return cls.model.objects.filter(is_deleted=False, status=status)

    @classmethod
    def filter_by_user(cls, user_id):
        return cls.model.objects.filter(is_deleted=False, user_id=user_id)

    @classmethod
    def filter_by_date_range(cls, start, end):
        qs = cls.model.objects.filter(is_deleted=False)
        if start:
            qs = qs.filter(start_time__gte=start)
        if end:
            qs = qs.filter(start_time__lte=end)
        return qs

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator


class CashReconciliationRepository(BaseSyncRepository):
    model = CashReconciliation

    @classmethod
    def get_for_shift(cls, shift_id):
        try:
            return cls.model.objects.select_related(
                'reconciled_by'
            ).get(shift_id=shift_id, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'reconciled_by'
            ).get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None
