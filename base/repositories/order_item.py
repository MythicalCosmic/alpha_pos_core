from datetime import timedelta
from django.db.models import (
    Sum, F, Q, Count, DecimalField, Case, When, Value, IntegerField,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal
from base.repositories.base import BaseSyncRepository
from base.models import OrderItem
from base.services.revenue import net_line_revenue
from base.services.refund_lines import (
    REFUND_EVENT_ALIAS, refund_item_events, refund_line_quantity,
    refund_line_revenue,
)

# How far back "top selling / popular" looks when ranking products.
POPULAR_WINDOW_DAYS = 30


class OrderItemRepository(BaseSyncRepository):
    model = OrderItem

    @classmethod
    def get_by_order(cls, order):
        return cls.model.objects.filter(is_deleted=False, order=order)

    @classmethod
    def get_by_order_id(cls, order_id):
        return cls.model.objects.filter(is_deleted=False, order_id=order_id)

    @classmethod
    def get_by_product(cls, product):
        return cls.model.objects.filter(is_deleted=False, product=product)

    @classmethod
    def get_by_order_with_product(cls, order_id):
        return cls.model.objects.filter(
            is_deleted=False, order_id=order_id
        ).select_related('product__category')

    @classmethod
    def get_unready_by_order(cls, order_id):
        return cls.model.objects.filter(
            is_deleted=False, order_id=order_id, ready_at__isnull=True
        )

    @classmethod
    def get_existing_unready(cls, order_id, product_id):
        return cls.model.objects.filter(
            is_deleted=False, order_id=order_id, product_id=product_id,
            ready_at__isnull=True,
        ).first()

    @classmethod
    def calculate_order_total(cls, order):
        return order.items.filter(is_deleted=False).aggregate(
            total=Coalesce(
                Sum(F('price') * F('quantity'), output_field=DecimalField(max_digits=12, decimal_places=2)),
                Decimal('0.00')
            )
        )['total']

    @classmethod
    def get_top_products(cls, date_from=None, date_to=None, limit=20):
        # Revenue/sales basis: only PAID, non-cancelled orders — otherwise open
        # carts and cancelled tickets inflate product/category revenue and the
        # popularity ranking (these feed the admin top-products/category stats).
        qs = cls.model.objects.filter(
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__isnull=False,
        )
        if date_from:
            qs = qs.filter(order__paid_at__gte=date_from)
        if date_to:
            qs = qs.filter(order__paid_at__lte=date_to)

        sales = list(qs.values(
            'product_id', 'product__name', 'product__category__name'
        ).annotate(
            total_qty=Sum('quantity'),
            total_revenue=Coalesce(
                Sum(net_line_revenue()),
                Decimal('0.00')
            ),
            order_count=Count('order_id', distinct=True),
        ))
        return cls._net_refunded_item_rows(
            sales, ('product_id', 'product__name', 'product__category__name'),
            date_from, date_to, sort_key='total_qty', reverse=True, limit=limit,
        )

    @classmethod
    def apply_popularity_order(cls, queryset, days=POPULAR_WINDOW_DAYS,
                              fallback_order_by='-created_at'):
        """Re-order a Product `queryset` so the best sellers come first.

        Ranks products by units sold over the last `days`; products with no
        recent sales fall to the bottom, ordered by `fallback_order_by`. This is
        the "top selling" filter the products endpoints default to (popular=True)
        and it composes with any category/search filter already applied — so it
        works "even with categories" (top sellers *within* the chosen category
        float up). Pass popular=False at the endpoint to skip it.
        """
        window_start = timezone.now() - timedelta(days=days)
        ordered_ids = [t['product_id'] for t in
                       cls.get_top_products(date_from=window_start, limit=500)]
        if not ordered_ids:
            # Nothing sold in the window yet — keep the requested ordering.
            return queryset.order_by(fallback_order_by)
        rank = Case(
            *[When(id=pid, then=Value(i)) for i, pid in enumerate(ordered_ids)],
            default=Value(len(ordered_ids)),
            output_field=IntegerField(),
        )
        return queryset.annotate(_pop_rank=rank).order_by('_pop_rank', fallback_order_by)

    @classmethod
    def get_least_sold_products(cls, date_from=None, date_to=None, limit=20):
        # Revenue/sales basis: only PAID, non-cancelled orders — otherwise open
        # carts and cancelled tickets inflate product/category revenue and the
        # popularity ranking (these feed the admin top-products/category stats).
        qs = cls.model.objects.filter(
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__isnull=False,
        )
        if date_from:
            qs = qs.filter(order__paid_at__gte=date_from)
        if date_to:
            qs = qs.filter(order__paid_at__lte=date_to)

        sales = list(qs.values(
            'product_id', 'product__name', 'product__category__name'
        ).annotate(
            total_qty=Sum('quantity'),
            total_revenue=Coalesce(
                Sum(net_line_revenue()),
                Decimal('0.00')
            ),
            order_count=Count('order_id', distinct=True),
        ))
        return cls._net_refunded_item_rows(
            sales, ('product_id', 'product__name', 'product__category__name'),
            date_from, date_to, sort_key='total_qty', reverse=False, limit=limit,
        )

    @classmethod
    def get_product_category_stats(cls, date_from=None, date_to=None):
        # Revenue/sales basis: only PAID, non-cancelled orders — otherwise open
        # carts and cancelled tickets inflate product/category revenue and the
        # popularity ranking (these feed the admin top-products/category stats).
        qs = cls.model.objects.filter(
            is_deleted=False, order__is_deleted=False, order__is_paid=True,
            order__paid_at__isnull=False,
        )
        if date_from:
            qs = qs.filter(order__paid_at__gte=date_from)
        if date_to:
            qs = qs.filter(order__paid_at__lte=date_to)

        sales = list(qs.values(
            'product__category_id', 'product__category__name'
        ).annotate(
            total_qty=Sum('quantity'),
            total_revenue=Coalesce(
                Sum(net_line_revenue()),
                Decimal('0.00')
            ),
            order_count=Count('order_id', distinct=True),
        ))
        return cls._net_refunded_item_rows(
            sales, ('product__category_id', 'product__category__name'),
            date_from, date_to, sort_key='total_revenue', reverse=True,
        )

    @classmethod
    def _net_refunded_item_rows(cls, sales, group_fields, date_from, date_to,
                                *, sort_key, reverse, limit=None):
        """Merge refund-date negative line events into grouped product sales."""
        refund_filters = {}
        if date_from:
            refund_filters['refunded_at__gte'] = date_from
        if date_to:
            refund_filters['refunded_at__lte'] = date_to
        refund_items = refund_item_events(**refund_filters)
        refunds = list(refund_items.values(*group_fields).annotate(
            refund_qty=Sum(refund_line_quantity(REFUND_EVENT_ALIAS)),
            refund_revenue=Coalesce(
                Sum(refund_line_revenue(REFUND_EVENT_ALIAS)), Decimal('0.00'),
            ),
            # Provider refunds reverse money, not another sold order. Reverse
            # order count once, only for the terminal cancellation event.
            refund_order_count=Count(
                'order_id',
                filter=Q(refund_event__source='ORDER_CANCEL'),
                distinct=True,
            ),
        ))

        def row_key(row):
            return tuple(row.get(field) for field in group_fields)

        merged = {row_key(row): dict(row) for row in sales}
        for row in refunds:
            target = merged.setdefault(row_key(row), {
                field: row.get(field) for field in group_fields
            })
            target['refund_qty'] = row['refund_qty'] or 0
            target['refund_revenue'] = row['refund_revenue'] or Decimal('0.00')
            target['refund_order_count'] = row['refund_order_count'] or 0

        for row in merged.values():
            gross_qty = row.get('total_qty') or 0
            gross_revenue = row.get('total_revenue') or Decimal('0.00')
            gross_orders = row.get('order_count') or 0
            row['gross_qty'] = gross_qty
            row['gross_revenue'] = gross_revenue
            row['gross_order_count'] = gross_orders
            row.setdefault('refund_qty', 0)
            row.setdefault('refund_revenue', Decimal('0.00'))
            row.setdefault('refund_order_count', 0)
            row['total_qty'] = gross_qty - row['refund_qty']
            row['total_revenue'] = gross_revenue - row['refund_revenue']
            row['order_count'] = gross_orders - row['refund_order_count']

        rows = sorted(
            merged.values(),
            key=lambda row: row.get(sort_key) or 0,
            reverse=reverse,
        )
        return rows[:limit] if limit is not None else rows
