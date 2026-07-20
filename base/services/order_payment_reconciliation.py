from decimal import Decimal

from django.db import transaction


def reconcile_stale_paid_headers(order_ids, *, require_later_sync_evidence=True):
    """Repair only the unmistakable paid-header sync-race state.

    This is deliberately narrower than normal tender settlement. It never
    creates money or guesses from a drawer balance: complete, later, live
    tender evidence must cover an intact unpaid order and its item arithmetic.
    Cash evidence may exceed the bill because the row records cash tendered
    before change; the order header total remains canonical revenue. Returns
    repaired Order UUID strings. ``require_later_sync_evidence=False`` is only
    for the authenticated cloud-to-owning-branch pull path: there, complete
    immutable tender rows remain authoritative even after a newer local
    operational status edit.
    """
    from base.models import (
        ExternalOrderPayment, Order, OrderItem, OrderPayment,
    )

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

            till_payments = list(
                OrderPayment.objects.select_for_update()
                .filter(order=order, is_deleted=False)
                .order_by('created_at', 'pk')
            )
            external_payments = list(
                ExternalOrderPayment.objects.select_for_update()
                .filter(order=order, is_deleted=False)
                .order_by('occurred_at', 'pk')
            )
            if not till_payments and not external_payments:
                continue
            # New tills stamp every line in one checkout with the same logical
            # action identity.  Refuse to combine actions (or half-upgraded
            # identified + anonymous lines) into one repaired paid header. Old
            # clients remain compatible because an entirely anonymous set is
            # still handled by the legacy evidence rules below.
            identified_actions = {
                payment.payment_action_id
                for payment in till_payments
                if payment.payment_action_id is not None
            }
            has_anonymous_till_line = any(
                payment.payment_action_id is None
                for payment in till_payments
            )
            if len(identified_actions) > 1 or (
                identified_actions and has_anonymous_till_line
            ):
                continue
            inferred_action_id = (
                next(iter(identified_actions)) if identified_actions else None
            )
            if order.payment_action_id is not None and till_payments and (
                inferred_action_id is None
                or inferred_action_id != order.payment_action_id
            ):
                continue
            if any(
                payment.method not in concrete_methods
                for payment in [*till_payments, *external_payments]
            ):
                continue
            if any(
                payment.branch_id != order.branch_id
                for payment in [*till_payments, *external_payments]
            ):
                continue
            if any(payment.amount < 0 for payment in till_payments):
                continue
            if any(payment.amount <= 0 for payment in external_payments):
                continue
            noncash_sum = sum(
                (payment.amount for payment in till_payments
                 if payment.method != Order.PaymentMethod.CASH),
                Decimal('0'),
            )
            noncash_sum += sum(
                (payment.amount for payment in external_payments
                 if payment.method != Order.PaymentMethod.CASH),
                Decimal('0'),
            )
            till_cash_tendered = sum(
                (payment.amount for payment in till_payments
                 if payment.method == Order.PaymentMethod.CASH),
                Decimal('0'),
            )
            external_cash = sum(
                (payment.amount for payment in external_payments
                 if payment.method == Order.PaymentMethod.CASH),
                Decimal('0'),
            )
            exact_external_total = noncash_sum + external_cash
            if exact_external_total > order.total_amount:
                continue
            has_till_cash = any(
                payment.method == Order.PaymentMethod.CASH
                for payment in till_payments
            )
            residual = order.total_amount - exact_external_total
            if has_till_cash:
                # CASH rows store the amount tendered and may include change.
                # Only the residual bill after exact non-drawer collections
                # must be covered; the header total remains canonical revenue.
                if till_cash_tendered < residual:
                    continue
            elif residual != 0:
                # External cash and all non-cash evidence are exact collected
                # amounts; only a till CASH line may legitimately include change.
                continue
            inferred_paid_at = max(
                [
                    payment.created_at
                    for payment in till_payments if payment.created_at
                ] + [
                    payment.occurred_at
                    for payment in external_payments if payment.occurred_at
                ],
                default=None,
            )
            if not inferred_paid_at:
                continue
            if (
                require_later_sync_evidence
                and inferred_paid_at < order.updated_at
            ):
                # Protect pay -> unpay ordering: an old payment delivered after
                # a newer explicit unpay header must not resurrect the sale.
                continue
            if require_later_sync_evidence:
                all_payments = [*till_payments, *external_payments]
                if not order.synced_at or any(
                    not payment.synced_at for payment in all_payments
                ):
                    continue
                if max(
                    payment.synced_at for payment in all_payments
                ) <= order.synced_at:
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
            # Product arithmetic remains strict, while total_amount may also
            # contain non-product delivery fees/tips (Telegram/online orders).
            # Those additions are non-negative by contract, so the customer
            # total can exceed, but never fall below, discounted product value.
            product_net = order.subtotal - order.discount_amount
            if (
                order.subtotal < 0
                or order.discount_amount < 0
                or product_net < 0
                or order.total_amount < product_net
            ):
                continue

            order.is_paid = True
            methods = {
                payment.method
                for payment in [*till_payments, *external_payments]
            }
            order.payment_method = (
                next(iter(methods)) if len(methods) == 1
                else Order.PaymentMethod.MIXED
            )
            order.paid_at = inferred_paid_at
            update_fields = ['is_paid', 'payment_method', 'paid_at']
            if order.payment_action_id is None and inferred_action_id is not None:
                order.payment_action_id = inferred_action_id
                update_fields.append('payment_action_id')
            order.save(update_fields=update_fields)
            repaired.add(str(order.uuid))

    return repaired
