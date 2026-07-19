"""Broadcast order lifecycle changes to the realtime displays.

A ``post_save`` on ``base.Order`` covers create + every status / payment update
(including sync-applied changes from other tills), so the KDS / order-queue stay
live without editing each service call site. Best-effort: a broadcast failure
never blocks the save.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from base.models import Order
from core.realtime.publish import publish_order_event

logger = logging.getLogger('core.realtime')


@receiver(post_save, sender=Order, dispatch_uid='realtime_order_broadcast')
def _broadcast_order(sender, instance, created, **kwargs):
    event = 'created' if created else 'updated'
    payload = {
        'id': instance.id,
        'uuid': str(instance.uuid),
        'status': getattr(instance, 'status', None),
        # A cashier frontend may use this only as a wake-up hint for the
        # durable /orders/print-jobs/claim endpoint. Including it avoids an
        # extra detail fetch for every ordinary POS order; the print ledger,
        # not this best-effort WebSocket event, remains delivery truth.
        'order_origin': getattr(instance, 'order_origin', Order.Origin.POS),
        'is_paid': getattr(instance, 'is_paid', None),
        'total_amount': str(getattr(instance, 'total_amount', '') or ''),
        'display_id': getattr(instance, 'display_id', None),
        'table_id': getattr(instance, 'table_id', None),
    }
    # post_save runs inside the caller's transaction. Publishing here used to
    # let screens observe is_paid=True before the OrderPayment rows, drawer and
    # stock writes completed; if any later step rolled back, clients retained a
    # phantom paid/printed state that never existed in the database.
    transaction.on_commit(
        lambda: _safe_publish_order_event(event, payload),
        robust=True,
    )


def _safe_publish_order_event(event, payload):
    try:
        publish_order_event(event, payload)
    except Exception:  # noqa: BLE001
        logger.debug('order broadcast signal failed', exc_info=True)
