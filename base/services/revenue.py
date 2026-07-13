"""Canonical product-revenue expressions.

Order revenue is already net in ``Order.total_amount``. Product/category
analytics operate on OrderItem rows, so an order-level discount must be shared
across those rows or product totals overstate sales. The accounting policy is
proportional allocation by each line's gross value::

    line net = line gross * (subtotal - discount_amount) / subtotal

Delivery fees and tips are intentionally not product revenue. A zero/legacy
subtotal falls back to gross line value rather than dividing by zero.
"""
from django.db.models import Case, DecimalField, ExpressionWrapper, F, When


REVENUE_FIELD = DecimalField(max_digits=24, decimal_places=6)


def gross_line_revenue():
    """``price * quantity`` for an OrderItem queryset."""
    return ExpressionWrapper(
        F('price') * F('quantity'), output_field=REVENUE_FIELD,
    )


def net_line_revenue():
    """Discount-adjusted product revenue for an OrderItem queryset."""
    gross = gross_line_revenue()
    discounted = ExpressionWrapper(
        gross
        * (F('order__subtotal') - F('order__discount_amount'))
        / F('order__subtotal'),
        output_field=REVENUE_FIELD,
    )
    return Case(
        When(order__subtotal__gt=0, then=discounted),
        default=gross,
        output_field=REVENUE_FIELD,
    )
