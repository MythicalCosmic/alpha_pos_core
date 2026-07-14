"""Canonical product-revenue expressions.

Order revenue is already net in ``Order.total_amount``. Product/category
analytics operate on OrderItem rows, so an order-level discount must be shared
across those rows or product totals overstate sales. The accounting policy is
proportional allocation by each line's gross value::

    line net = line gross * (subtotal - discount_amount) / subtotal

Delivery fees and tips are intentionally not product revenue. A zero/legacy
subtotal falls back to gross line value rather than dividing by zero.
"""
from decimal import Decimal

from django.db.models import Case, DecimalField, ExpressionWrapper, F, Sum, Value, When
from django.db.models.functions import Greatest


REVENUE_FIELD = DecimalField(max_digits=24, decimal_places=6)


def gross_line_revenue():
    """``price * quantity`` for an OrderItem queryset."""
    return ExpressionWrapper(
        F('price') * F('quantity'), output_field=REVENUE_FIELD,
    )


def net_line_revenue():
    """Discount-adjusted product revenue for an OrderItem queryset."""
    gross = gross_line_revenue()
    discounted = Greatest(
        ExpressionWrapper(
            gross
            * (F('order__subtotal') - F('order__discount_amount'))
            / F('order__subtotal'),
            output_field=REVENUE_FIELD,
        ),
        Value(Decimal('0'), output_field=REVENUE_FIELD),
    )
    return Case(
        When(order__subtotal__gt=0, then=discounted),
        default=gross,
        output_field=REVENUE_FIELD,
    )


def net_grouped_items(sale_items, refund_items, fields):
    """Group product events and subtract refund-date quantities/revenue.

    ``sale_items`` is bounded by ``order.paid_at``; ``refund_items`` is bounded
    independently by ``order.refunds.refunded_at``. Keeping both clocks avoids
    erasing a prior-day sale merely because its order was canceled today.
    """
    sales = sale_items.values(*fields).annotate(
        q=Sum('quantity'), rev=Sum(net_line_revenue()),
    )
    from base.services.refund_lines import (
        REFUND_EVENT_ALIAS, refund_line_quantity, refund_line_revenue,
    )
    refunds = refund_items.values(*fields).annotate(
        # Provider/tender refunds reverse proportional money only. Physical
        # units reverse once, on the terminal ORDER_CANCEL event.
        q=Sum(refund_line_quantity(REFUND_EVENT_ALIAS)),
        rev=Sum(refund_line_revenue(REFUND_EVENT_ALIAS)),
    )

    def key(row):
        return tuple(row.get(field) for field in fields)

    merged = {key(row): dict(row) for row in sales}
    for row in refunds:
        target = merged.setdefault(
            key(row), {field: row.get(field) for field in fields},
        )
        target['refund_q'] = row['q'] or 0
        target['refund_rev'] = row['rev'] or Decimal('0.00')
    for row in merged.values():
        gross_q = row.get('q') or 0
        gross_rev = row.get('rev') or Decimal('0.00')
        row['gross_q'] = gross_q
        row['gross_rev'] = gross_rev
        row.setdefault('refund_q', 0)
        row.setdefault('refund_rev', Decimal('0.00'))
        row['q'] = gross_q - row['refund_q']
        row['rev'] = gross_rev - row['refund_rev']
    return list(merged.values())
