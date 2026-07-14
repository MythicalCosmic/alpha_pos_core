"""Staff order notifications (Telegram).

These now fire on the SERVER edition only — the server is the single notification
source. As orders sync up from the tills, a post_save(Order) signal (see
notifications/signals.py) calls `OrderNotification.dispatch(order)`, which fires
each message exactly once via OrderNotificationDispatch and reply-threads
`order.ready` under the original `order.new` message.

The local edition still has these methods on its call paths, but the EDITION
gate makes them no-ops there (the till no longer sends — no more one-bot-per-till
duplicates).
"""
import logging

from django.conf import settings

from notifications.services.sender_service import SenderService
from notifications.helpers import format_datetime, format_money, format_prep_time

logger = logging.getLogger(__name__)

ORDER_TYPE_LABELS = {
    'HALL': 'Zalda',
    'DELIVERY': 'Yetkazib berish',
    'PICKUP': 'Olib ketish',
}


def _is_server():
    return getattr(settings, 'EDITION', '') == 'server'


def _items_list(order):
    """Multiline 'Product xQty — Sum so'm' list for the order's (live) items."""
    lines = []
    for item in order.items.filter(is_deleted=False).select_related('product'):
        name = item.product.name if item.product_id else (item.detail or '—')
        lines.append(
            f"  • {name} x{item.quantity} — {format_money(item.price * item.quantity)} so'm"
        )
    return '\n'.join(lines) if lines else '  —'


def _cashier_name(order):
    if order.cashier_id and order.cashier:
        return f'{order.cashier.first_name} {order.cashier.last_name}'.strip() or '—'
    return '—'


class OrderNotification:

    # ── server-side idempotent dispatcher (called from the post_save signal) ──
    @classmethod
    def dispatch(cls, order):
        """Fire the staff notification(s) for `order`'s current state exactly
        once. Server edition only; idempotent + concurrency-safe via a row lock
        on OrderNotificationDispatch (two near-simultaneous syncs of the same
        order can't both send the order.new message)."""
        if not _is_server():
            return
        from django.db import transaction
        from notifications.models import OrderNotificationDispatch
        status = getattr(order, 'status', '')

        with transaction.atomic():
            disp, _ = OrderNotificationDispatch.objects.get_or_create(order_id=order.id)
            disp = OrderNotificationDispatch.objects.select_for_update().get(pk=disp.pk)

            if status == 'CANCELED':
                # Announce once, but only if we already announced the order.
                if disp.new_sent and not disp.cancelled_sent:
                    cls.on_order_cancelled(order.id)
                    disp.cancelled_sent = True
                    disp.save(update_fields=['cancelled_sent', 'updated_at'])
                return

            changed = []
            if not disp.new_sent:
                # The item list is core to order.new, but on the cloud an order
                # syncs up in a SEPARATE batch BEFORE its OrderItems — so a freshly
                # received order has none yet. Hold order.new (and DON'T set
                # new_sent) until items are present; the post-receive hook
                # re-dispatches once the item batch lands, and server-native
                # orders (smartfood/admin) already have items in the same txn.
                if not order.items.filter(is_deleted=False).exists():
                    return
                cls.on_new_order(order)
                disp.new_sent = True
                changed.append('new_sent')
            # READY replies under the order.new message (worker resolves reply ids).
            # Only after order.new has gone out (new_sent set above or earlier).
            if status == 'READY' and disp.new_sent and not disp.ready_sent:
                cls.on_order_ready(order.id)
                disp.ready_sent = True
                changed.append('ready_sent')
            if changed:
                changed.append('updated_at')
                disp.save(update_fields=changed)

    # ── individual messages (gated to server; local calls are no-ops) ──
    @classmethod
    def on_new_order(cls, order):
        if not _is_server():
            return
        _, time_str = format_datetime()
        SenderService.send('order.new', {
            'display_id': order.id,  # NOT order.display_id — the till counter isn't synced (always 1 on the server)
            'cashier_name': _cashier_name(order),
            'order_type': ORDER_TYPE_LABELS.get(order.order_type, order.order_type),
            'total_amount': format_money(order.total_amount),
            'items_list': _items_list(order),
            'time': time_str,
        }, order_id=order.id, thread_role='new')

    @classmethod
    def on_order_ready(cls, order_id):
        if not _is_server():
            return
        from base.models import Order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return

        prep_time = '—'
        if order.ready_at and order.created_at:
            seconds = (order.ready_at - order.created_at).total_seconds()
            prep_time = format_prep_time(seconds)

        _, time_str = format_datetime()
        SenderService.send('order.ready', {
            'display_id': order.id,  # NOT order.display_id — the till counter isn't synced (always 1 on the server)
            'prep_time': prep_time,
            'total_amount': format_money(order.total_amount),
            'items_list': _items_list(order),
            'time': time_str,
        }, order_id=order.id, thread_role='reply')

    @classmethod
    def on_order_cancelled(cls, order_id):
        if not _is_server():
            return
        from base.models import Order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return
        _, time_str = format_datetime()
        SenderService.send('order.cancelled', {
            'display_id': order.id,  # NOT order.display_id — the till counter isn't synced (always 1 on the server)
            'total_amount': format_money(order.total_amount),
            'time': time_str,
        }, order_id=order.id, thread_role='reply')

    @classmethod
    def on_order_paid(cls, order_id):
        if not _is_server():
            return
        from base.models import Order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return
        _, time_str = format_datetime()
        SenderService.send('order.paid', {
            'display_id': order.id,  # NOT order.display_id — the till counter isn't synced (always 1 on the server)
            'total_amount': format_money(order.total_amount),
            'time': time_str,
        }, order_id=order.id, thread_role='reply')
