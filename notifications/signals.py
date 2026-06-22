"""Staff order-notification trigger (SERVER edition only).

post_save(Order) -> on_commit -> the idempotent OrderNotification.dispatch.
Registered only on the server (see apps.ready), so the tills never double-send.

This path covers SERVER-NATIVE orders (smartfood / admin), whose items are
created in the same transaction, and the later READY status update of a synced
order (its items have arrived by then). For a freshly SYNCED order the items
arrive in a SEPARATE batch AFTER the order, so dispatch's items-gate holds
order.new until the sync receiver re-dispatches once the item batch lands
(base.services.sync.receiver._notify_received_orders).
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from base.models import Order

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Order, dispatch_uid='staff_order_notify')
def _staff_order_notify(sender, instance, created, **kwargs):
    order_id = instance.id
    transaction.on_commit(lambda: _safe_dispatch(order_id))


def _safe_dispatch(order_id):
    try:
        from notifications.handlers.order import OrderNotification
        order = Order.objects.select_related('cashier').filter(id=order_id).first()
        if order:
            OrderNotification.dispatch(order)
    except Exception:  # noqa: BLE001 — a notification must never break a sync apply
        logger.warning('staff order notify dispatch failed (order=%s)', order_id, exc_info=True)
