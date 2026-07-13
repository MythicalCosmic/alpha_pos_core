from decimal import Decimal

from django.db import transaction


def reconcile_stale_paid_headers(order_ids):
    """Repair only the unmistakable paid-header sync-race state.

    This is deliberately narrower than normal tender settlement. It never
    creates money or guesses from a drawer balance: complete, later, live
    tender evidence must cover an intact unpaid order and its item arithmetic.
    Cash evidence may exceed the bill because the row records cash tendered
    before change; the order header total remains canonical revenue. Returns
    repaired Order UUID strings.
    """
    from base.models import Order, OrderItem, OrderPayment

    repaired = set()
    concrete_methods = {
        value for value, _label in Order.PaymentMethod.choices
        if value != Order.PaymentMethod.MIXED
    }

    for order_id in set(order_ids or []):
        with transaction.atomic():
            order = (
                Order.objects.select_for_update()
                .filter(
                    pk=order_id,
                    is_deleted=False,
                    is_paid=False,
                    payment_method__isnull=True,
                    paid_at__isnull=True,
                )
                .exclude(status=Order.Status.CANCELED)
                .first()
            )
            if order is None:
                continue

            payments = list(
                OrderPayment.objects.select_for_update()
                .filter(order=order, is_deleted=False)
                .order_by('created_at', 'pk')
            )
            if not payments:
                continue
            if any(payment.method not in concrete_methods for payment in payments):
                continue
            if any(payment.branch_id != order.branch_id for payment in payments):
                continue
            if any(payment.amount < 0 for payment in payments):
                continue
            noncash_sum = sum(
                (payment.amount for payment in payments
                 if payment.method != Order.PaymentMethod.CASH),
                Decimal('0'),
            )
            cash_sum = sum(
                (payment.amount for payment in payments
                 if payment.method == Order.PaymentMethod.CASH),
                Decimal('0'),
            )
            if noncash_sum > order.total_amount:
                continue
            has_cash = any(
                payment.method == Order.PaymentMethod.CASH
                for payment in payments
            )
            if has_cash:
                # CASH rows store the amount tendered and may include change.
                # Only the residual bill after non-cash must be covered; the
                # header total remains canonical revenue.
                if cash_sum < order.total_amount - noncash_sum:
                    continue
            elif noncash_sum != order.total_amount:
                # Card/Payme cannot over- or under-tender.
                continue
            inferred_paid_at = max(
                (payment.created_at for payment in payments if payment.created_at),
                default=None,
            )
            if not inferred_paid_at or inferred_paid_at < order.updated_at:
                # Protect pay -> unpay ordering: an old payment delivered after
                # a newer explicit unpay header must not resurrect the sale.
                continue
            if not order.synced_at or any(not payment.synced_at for payment in payments):
                continue
            if max(payment.synced_at for payment in payments) <= order.synced_at:
                continue

            items = list(
                OrderItem.objects.select_for_update()
                .filter(order=order, is_deleted=False)
            )
            if not items:
                continue
            item_gross = sum(
                (item.price * item.quantity for item in items), Decimal('0')
            )
            if item_gross != order.subtotal:
                continue
            if order.total_amount != order.subtotal - order.discount_amount:
                continue

            order.is_paid = True
            methods = {payment.method for payment in payments}
            order.payment_method = (
                next(iter(methods)) if len(methods) == 1
                else Order.PaymentMethod.MIXED
            )
            order.paid_at = inferred_paid_at
            order.save(update_fields=['is_paid', 'payment_method', 'paid_at'])
            repaired.add(str(order.uuid))

    return repaired
