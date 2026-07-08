from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Q, Count, Avg, DecimalField
from django.db.models.functions import Coalesce, TruncDate, TruncMonth, TruncYear
from django.core.paginator import Paginator
from decimal import Decimal
from base.repositories.base import BaseSyncRepository
from base.models import Order, DisplayIdCounter, ChefQueueCounter, SequenceCounter


# Wrap kitchen-handoff numbers at this point so the line never has to read
# four-digit numbers off the bumper. 100 matches what admins/waiters used
# pre-fix; the customer surface used to monotonically increase, which the
# kitchen flagged as confusing.
DISPLAY_ID_WRAP_AT = 100


class OrderRepository(BaseSyncRepository):
    model = Order

    @classmethod
    def get_by_status(cls, status):
        return cls.model.objects.filter(is_deleted=False, status=status)

    @classmethod
    def get_by_user(cls, user):
        return cls.model.objects.filter(is_deleted=False, user=user)

    @classmethod
    def get_for_update(cls, order_id):
        try:
            return cls.model.objects.select_for_update().get(id=order_id, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_display_id(cls, display_id):
        try:
            return cls.model.objects.get(display_id=display_id, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_order_type(cls, order_type):
        return cls.model.objects.filter(is_deleted=False, order_type=order_type)

    @classmethod
    def get_open(cls):
        return cls.model.objects.filter(
            is_deleted=False,
            status__in=[Order.Status.OPEN, Order.Status.PREPARING, Order.Status.READY],
        )

    @classmethod
    def get_completed(cls):
        return cls.model.objects.filter(is_deleted=False, status=Order.Status.COMPLETED)

    @classmethod
    def get_unpaid(cls):
        return cls.model.objects.filter(is_deleted=False, is_paid=False).exclude(
            status=Order.Status.CANCELED,
        )

    @classmethod
    def get_by_cashier(cls, cashier):
        return cls.model.objects.filter(is_deleted=False, cashier=cashier)

    @classmethod
    def get_by_delivery_person(cls, delivery_person):
        return cls.model.objects.filter(is_deleted=False, delivery_person=delivery_person)

    @classmethod
    def get_with_relations(cls, include_deleted=False):
        qs = cls.model.objects.all() if include_deleted else cls.model.objects.filter(is_deleted=False)
        return qs.select_related(
            'user', 'cashier', 'delivery_person', 'place', 'table', 'customer'
        ).prefetch_related('items__product__category', 'payments')

    @classmethod
    def get_by_id_with_relations(cls, pk):
        try:
            return cls.model.objects.select_related(
                'user', 'cashier', 'delivery_person', 'place', 'table', 'customer'
            ).prefetch_related('items__product__category', 'payments').get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_last_display_id(cls):
        # Retained for back-compat; new code should call next_display_id().
        last = cls.model.objects.order_by('-id').only('display_id').first()
        if not last or not last.display_id:
            return 0
        return last.display_id

    @classmethod
    def next_display_id(cls, scope=None):
        """Atomically allocate the next display_id for `scope`.

        Replaces the racy `last_id+1` / `(last_id % 100)+1` reads each
        order-create surface used to do. Locks the per-scope counter row
        with select_for_update so two concurrent creates cannot allocate
        the same number. Caller must be inside a transaction (the order
        services already wrap create in @transaction.atomic).

        scope defaults to BRANCH_ID so each branch maintains its own
        kitchen-handoff numbering. Returns 1..DISPLAY_ID_WRAP_AT.
        """
        if scope is None:
            scope = getattr(settings, 'BRANCH_ID', 'default') or 'default'
        with transaction.atomic():
            row, _ = DisplayIdCounter.objects.select_for_update().get_or_create(
                scope=scope, defaults={'value': 0},
            )
            row.value = (row.value % DISPLAY_ID_WRAP_AT) + 1
            row.save(update_fields=['value', 'updated_at'])
            return row.value

    @classmethod
    def next_chef_queue_number(cls, scope=None):
        """Atomically allocate the next MONOTONIC chef-queue number for `scope`.

        Same locked allocator as next_display_id but WITHOUT the wrap: the chef
        display needs an ever-increasing number (never resets to 1 after 100).
        Caller must be inside a transaction (order services wrap create in
        @transaction.atomic). scope defaults to BRANCH_ID.
        """
        if scope is None:
            scope = getattr(settings, 'BRANCH_ID', 'default') or 'default'
        with transaction.atomic():
            row, _ = ChefQueueCounter.objects.select_for_update().get_or_create(
                scope=scope, defaults={'value': 0},
            )
            row.value = row.value + 1
            row.save(update_fields=['value', 'updated_at'])
            return row.value

    @classmethod
    def next_order_number(cls, scope=None):
        """Atomically allocate the next per-BUSINESS-DAY order number (item 4).

        Monotonic within a (branch, business day): never wraps — so two orders the
        same day never share a number — and resets to 1 each business day (the scope
        carries the business date). Stored on the order and synced as a VALUE; the
        counter is per-branch bookkeeping and never propagates to siblings/cloud.
        Caller must be inside a transaction (order services wrap create in
        @transaction.atomic). scope defaults to BRANCH_ID.
        """
        from base.services.business_day import business_date
        branch = scope or getattr(settings, 'BRANCH_ID', 'default') or 'default'
        counter_scope = f'ordernum:{branch}:{business_date().isoformat()}'
        with transaction.atomic():
            row, _ = SequenceCounter.objects.select_for_update().get_or_create(
                scope=counter_scope, defaults={'value': 0},
            )
            row.value = row.value + 1
            row.save(update_fields=['value', 'updated_at'])
            return row.value

    @classmethod
    def paginate(cls, queryset, page=1, per_page=20):
        paginator = Paginator(queryset, per_page)
        return paginator.get_page(page), paginator

    @classmethod
    def build_filtered_queryset(cls, statuses=None, payment_status=None,
                                 category_ids=None, product_ids=None, user_id=None,
                                 cashier_id=None, order_type=None, date_from=None,
                                 date_to=None, order_by='-created_at',
                                 include_deleted=False, customer_id=None,
                                 tod_from=None, tod_to=None):
        qs = cls.get_with_relations(include_deleted=include_deleted)

        # Scope to one client (base.Customer) — powers the returning-client history
        # lookup. customer_id is the client, distinct from user_id (the staff
        # operator who rang the order).
        if customer_id:
            qs = qs.filter(customer_id=customer_id)

        if payment_status:
            payment_status = payment_status.strip().upper()
            # Cancelled orders are never "paid" or "unpaid" work to settle — they
            # are dead. Excluding CANCELED here mirrors get_unpaid() (the cashier's
            # unpaid screen filters via this method, and a cancelled-but-unpaid
            # order used to linger there forever).
            if payment_status == 'PAID':
                qs = qs.filter(is_paid=True).exclude(status=Order.Status.CANCELED)
            elif payment_status == 'UNPAID':
                qs = qs.filter(is_paid=False).exclude(status=Order.Status.CANCELED)

        if statuses:
            valid = [c[0] for c in Order.Status.choices]
            filtered = [s.upper() for s in statuses if s.upper() in valid]
            if filtered:
                qs = qs.filter(status__in=filtered)

        if category_ids:
            qs = qs.filter(items__product__category_id__in=category_ids).distinct()

        if product_ids:
            # Orders that CONTAIN any of these products (mirrors category_ids).
            qs = qs.filter(items__product_id__in=product_ids).distinct()

        if user_id:
            qs = qs.filter(user_id=user_id)

        if cashier_id:
            qs = qs.filter(cashier_id=cashier_id)

        if order_type:
            qs = qs.filter(order_type=order_type.upper())

        if date_from:
            qs = qs.filter(created_at__gte=date_from)

        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        # Time-of-day filter: keep only rows whose LOCAL wall-clock time is within
        # [tod_from, tod_to], applied per day (working-hours window). No-op if both None.
        from base.services.business_day import tod_filter
        qs = tod_filter(qs, tod_from, tod_to, field='created_at')

        return qs.order_by(order_by)

    @classmethod
    def get_stats_aggregate(cls, date_from=None, date_to=None, cashier_id=None,
                            product_ids=None, tod_from=None, tod_to=None):
        qs = cls.model.objects.filter(is_deleted=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        if cashier_id:
            qs = qs.filter(cashier_id=cashier_id)
        if product_ids:
            # Orders CONTAINING any of these products — via a SUBQUERY, not a join,
            # so the Count/Sum aggregates below don't fan out (an order with two
            # matching products would otherwise be counted twice).
            from base.models import OrderItem
            qs = qs.filter(id__in=OrderItem.objects.filter(
                is_deleted=False, product_id__in=product_ids).values('order_id'))
        from base.services.business_day import tod_filter
        qs = tod_filter(qs, tod_from, tod_to, field='created_at')

        return qs.aggregate(
            total=Count('id'),
            open=Count('id', filter=Q(status='OPEN')),
            preparing=Count('id', filter=Q(status='PREPARING')),
            ready=Count('id', filter=Q(status='READY')),
            completed=Count('id', filter=Q(status='COMPLETED')),
            cancelled=Count('id', filter=Q(status='CANCELED')),
            paid=Count('id', filter=Q(is_paid=True)),
            unpaid=Count('id', filter=Q(is_paid=False, status__in=['PREPARING', 'READY', 'COMPLETED'])),
            total_revenue=Coalesce(
                Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')),
                Decimal('0.00'),
                output_field=DecimalField()
            ),
            avg_order_value=Coalesce(
                Avg('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')),
                Decimal('0.00'),
                output_field=DecimalField()
            ),
        )

    @classmethod
    def get_daily_stats(cls, date_from=None, date_to=None, cashier_id=None,
                        tod_from=None, tod_to=None):
        from base.services.business_day import business_day_date_expr, tod_filter
        qs = cls.model.objects.filter(is_deleted=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        if cashier_id:
            qs = qs.filter(cashier_id=cashier_id)
        qs = tod_filter(qs, tod_from, tod_to, field='created_at')

        # Bucket by BUSINESS date (03:00 cutover), not calendar midnight, so the
        # daily series matches the business-day windowing used everywhere else.
        return list(qs.annotate(date=business_day_date_expr('created_at')).values('date').annotate(
            orders=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
            paid=Count('id', filter=Q(is_paid=True)),
            cancelled=Count('id', filter=Q(status='CANCELED')),
        ).order_by('date'))

    @classmethod
    def get_monthly_stats(cls, date_from=None, date_to=None):
        qs = cls.model.objects.filter(is_deleted=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        return list(qs.annotate(month=TruncMonth('created_at')).values('month').annotate(
            orders=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
            paid=Count('id', filter=Q(is_paid=True)),
            cancelled=Count('id', filter=Q(status='CANCELED')),
            avg_order_value=Coalesce(Avg('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
        ).order_by('month'))

    @classmethod
    def get_yearly_stats(cls):
        return list(
            cls.model.objects.filter(is_deleted=False)
            .annotate(year=TruncYear('created_at')).values('year').annotate(
                orders=Count('id'),
                revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
                paid=Count('id', filter=Q(is_paid=True)),
                cancelled=Count('id', filter=Q(status='CANCELED')),
            ).order_by('year')
        )

    @classmethod
    def get_by_cashier_stats(cls, date_from=None, date_to=None):
        qs = cls.model.objects.filter(is_deleted=False, cashier__isnull=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        return list(qs.values(
            'cashier_id', 'cashier__first_name', 'cashier__last_name'
        ).annotate(
            orders=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
            paid=Count('id', filter=Q(is_paid=True)),
            cancelled=Count('id', filter=Q(status='CANCELED')),
        ).order_by('-orders'))

    @classmethod
    def get_by_status_stats(cls, date_from=None, date_to=None):
        qs = cls.model.objects.filter(is_deleted=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        return list(qs.values('status').annotate(
            count=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
        ).order_by('status'))

    @classmethod
    def get_by_order_type_stats(cls, date_from=None, date_to=None):
        qs = cls.model.objects.filter(is_deleted=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        return list(qs.values('order_type').annotate(
            count=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
        ).order_by('order_type'))

    @classmethod
    def get_avg_prep_time(cls, date_from=None, date_to=None):
        # Aggregate in SQL: pre-fix this loaded every row to compute the
        # mean in Python plus a separate count() query. On a busy day with
        # tens of thousands of orders that's a long table scan and a lot of
        # round-trips.
        from django.db.models import F, Avg, ExpressionWrapper, DurationField
        qs = cls.model.objects.filter(
            is_deleted=False, status='READY', ready_at__isnull=False
        )
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)

        result = qs.aggregate(
            avg_duration=Avg(
                ExpressionWrapper(
                    F('ready_at') - F('created_at'),
                    output_field=DurationField(),
                ),
            ),
        )
        avg = result.get('avg_duration')
        if avg is None:
            return None
        return avg.total_seconds()

    @classmethod
    def get_hourly_distribution(cls, date_from=None, date_to=None,
                                tod_from=None, tod_to=None):
        from django.db.models.functions import ExtractHour
        from django.utils import timezone as _tz
        from base.services.business_day import tod_filter
        qs = cls.model.objects.filter(is_deleted=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        qs = tod_filter(qs, tod_from, tod_to, field='created_at')

        # Local hour (Asia/Tashkent) — ExtractHour without tzinfo bucketed on UTC,
        # off by the tz offset from the business-day windowing.
        return list(qs.annotate(hour=ExtractHour('created_at', tzinfo=_tz.get_current_timezone())).values('hour').annotate(
            count=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=Q(is_paid=True) & ~Q(status='CANCELED')), Decimal('0.00'), output_field=DecimalField()),
        ).order_by('hour'))
