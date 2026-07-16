"""Canonical product-line allocation for immutable refund events."""
from decimal import Decimal

from django.db.models import (
    Case, DecimalField, ExpressionWrapper, F, FilteredRelation, IntegerField,
    Q, Subquery, Value, When,
)
from django.db.models.query import QuerySet
from django.db.models.functions import NullIf

from base.services.revenue import net_line_revenue


REFUND_EVENT_ALIAS = 'refund_event'


def refund_item_events(item_queryset=None, **refund_filters):
    """Return OrderItems joined to exactly one filtered refund-event alias.

    Chaining separate ``order__refund`` filters makes Django add one reverse
    JOIN per call. With two refund events those joins form a Cartesian product,
    multiplying quantities and proportional revenue. A FilteredRelation keeps
    the event window/source/branch predicates and every aggregate on one alias.

    ``refund_filters`` are OrderRefund lookups without the relation prefix,
    e.g. ``refunded_at__gte=...`` or ``source='ORDER_CANCEL'``.
    """
    if item_queryset is None:
        from base.models import OrderItem
        item_queryset = OrderItem.objects.all()

    condition = Q(order__refund__is_deleted=False)
    for lookup, value in refund_filters.items():
        # FilteredRelation intentionally rejects a QuerySet RHS because its
        # OuterRef prefixes cannot be safely rewritten.  An explicit PK
        # subquery preserves the caller's filtered event set without creating
        # another reverse refund JOIN (and therefore without cartesian sums).
        if isinstance(value, QuerySet):
            value = Subquery(value.values('pk'))
        condition &= Q(**{f'order__refund__{lookup}': value})

    return (
        item_queryset
        .filter(is_deleted=False, order__is_deleted=False)
        .annotate(**{
            REFUND_EVENT_ALIAS: FilteredRelation(
                'order__refund', condition=condition,
            ),
        })
        .filter(**{f'{REFUND_EVENT_ALIAS}__isnull': False})
    )


def refund_item_events_in_window(window, item_queryset=None):
    """Refund-line events scoped by a ReportingWindow without duplicate joins.

    Applying a time predicate after ``refund_item_events`` can make Django add a
    second reverse-refund JOIN. Multiple refund events would then multiply line
    quantities/revenue. Filter event PKs first and feed that subquery into the
    existing single ``FilteredRelation`` instead.
    """
    from base.models import OrderRefund

    events = window.filter(
        OrderRefund.objects.filter(is_deleted=False), 'refunded_at',
    )
    return refund_item_events(item_queryset, pk__in=events)


def refund_line_revenue(refund_path='order__refund'):
    """Proportionally allocate a refund's money over its original order lines."""
    money = DecimalField(max_digits=24, decimal_places=6)
    zero = Value(Decimal('0.00'), output_field=money)
    return ExpressionWrapper(
        (net_line_revenue() * F(f'{refund_path}__amount'))
        / NullIf(F('order__total_amount'), zero),
        output_field=money,
    )


def refund_line_quantity(refund_path='order__refund'):
    """Reverse units only when the order itself was terminally cancelled.

    A partial provider/tender refund changes realized revenue but does not say
    which physical menu items were returned.
    """
    return Case(
        When(
            **{f'{refund_path}__source': 'ORDER_CANCEL'},
            then=F('quantity'),
        ),
        default=Value(0),
        output_field=IntegerField(),
    )
