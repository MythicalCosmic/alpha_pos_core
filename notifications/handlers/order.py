"""Staff order notifications (Telegram).

These now fire on the SERVER edition only — the server is the single notification
source. As orders sync up from the tills, a post_save(Order) signal (see
notifications/signals.py) calls `OrderNotification.dispatch(order)`, which fires
each lifecycle transition exactly once via OrderNotificationDispatch. `order.new`
creates one message per configured chat; `order.ready` edits that message in
place with the final preparation details.

The local edition still has these methods on its call paths, but the EDITION
gate makes them no-ops there (the till no longer sends — no more one-bot-per-till
duplicates).
"""
from django.conf import settings

from notifications.services.sender_service import SenderService
from notifications.helpers import format_datetime, format_money, format_prep_time
from notifications.preparation import (
    classify_preparation,
    preparation_target_for_order,
)

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


def _format_timestamp(value):
    if not value:
        return '—'
    date_str, time_str = format_datetime(value)
    return f'{date_str} {time_str}'


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
                if not cls.on_new_order(order):
                    # Disabled/template-less/no-recipient creation is not a
                    # delivery.  Leave the transition pending so a later order
                    # save can retry after configuration is repaired.
                    return
                disp.new_sent = True
                changed.append('new_sent')
            # READY replaces the order.new text in place (the worker resolves the
            # per-chat Telegram message ids). Only edit after order.new has gone
            # out (new_sent set above or earlier).
            if status == 'READY' and disp.new_sent and not disp.ready_sent:
                if cls.on_order_ready(order.id):
                    disp.ready_sent = True
                    changed.append('ready_sent')
            if changed:
                changed.append('updated_at')
                disp.save(update_fields=changed)

    # ── individual messages (gated to server; local calls are no-ops) ──
    @classmethod
    def on_new_order(cls, order):
        if not _is_server():
            return False
        accepted_at = _format_timestamp(order.created_at)
        _, legacy_time = format_datetime(order.created_at)
        return SenderService.send('order.new', {
            'display_id': order.id,  # NOT order.display_id — the till counter isn't synced (always 1 on the server)
            'cashier_name': _cashier_name(order),
            'order_type': ORDER_TYPE_LABELS.get(order.order_type, order.order_type),
            'total_amount': format_money(order.total_amount),
            'items_list': _items_list(order),
            'accepted_at': accepted_at,
            # Kept for operator-customized legacy templates.
            'time': legacy_time,
        }, order_id=order.id, thread_role='new')

    @classmethod
    def on_order_ready(cls, order_id):
        if not _is_server():
            return False
        from base.models import Order
        try:
            order = Order.objects.select_related('cashier').get(id=order_id)
        except Order.DoesNotExist:
            return False

        prep_elapsed = '—'
        prep_time = '—'
        prep_target = 'Belgilanmagan'
        prep_status_icon = '✅'
        prep_status_label = 'TAYYOR'
        prep_status_level = 'UNTRACKED'
        if order.ready_at and order.created_at:
            seconds = (order.ready_at - order.created_at).total_seconds()
            if seconds >= 0:
                prep_elapsed = '0:00' if seconds == 0 else format_prep_time(seconds)
                prep_time = prep_elapsed
                product_names = order.items.filter(
                    is_deleted=False,
                    product_id__isnull=False,
                ).values_list('product__name', flat=True)
                target = preparation_target_for_order(product_names)
                if target is not None:
                    performance = classify_preparation(seconds, target)
                    prep_target = target.display
                    prep_status_icon = performance.icon
                    prep_status_label = performance.label
                    prep_status_level = performance.key
                    # Legacy/operator-customized templates generally only use
                    # {prep_time}. Keep the new color and target visible there
                    # too, while exposing individual fields to the new default.
                    prep_time = (
                        f'{performance.icon} {prep_elapsed} · '
                        f"me'yor {target.display} · {performance.label}"
                    )

        accepted_at = _format_timestamp(order.created_at)
        ready_at = _format_timestamp(order.ready_at)
        _, legacy_time = format_datetime(order.ready_at)
        return SenderService.send('order.ready', {
            'display_id': order.id,  # NOT order.display_id — the till counter isn't synced (always 1 on the server)
            'cashier_name': _cashier_name(order),
            'order_type': ORDER_TYPE_LABELS.get(order.order_type, order.order_type),
            'prep_time': prep_time,
            'prep_elapsed': prep_elapsed,
            'prep_target': prep_target,
            'prep_status_icon': prep_status_icon,
            'prep_status_label': prep_status_label,
            'prep_status_level': prep_status_level,
            'total_amount': format_money(order.total_amount),
            'items_list': _items_list(order),
            'accepted_at': accepted_at,
            'ready_at': ready_at,
            # Kept for operator-customized legacy templates.
            'time': legacy_time,
        }, order_id=order.id, thread_role='edit')

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
