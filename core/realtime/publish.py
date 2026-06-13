"""Producer-side helpers: broadcast events to the realtime groups.

Called from sync Django code (signals / services). Safe no-op when no channel
layer is configured (e.g. a management command run without ASGI).
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from core.realtime import consumers

logger = logging.getLogger('core.realtime')


def _send(group, payload):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, {'type': 'broadcast', 'payload': payload})
    except Exception:  # noqa: BLE001 — realtime is best-effort, never fatal
        logger.debug('realtime publish failed (group=%s)', group, exc_info=True)


def publish_order_event(event, order):
    """Broadcast an order lifecycle event to the order-queue + KDS displays."""
    payload = {'channel': 'orders', 'event': event, 'order': order}
    _send(consumers.ORDERS_GROUP, payload)
    _send(consumers.KDS_GROUP, payload)


def publish_cashier_control(action, **data):
    """Server -> till: lock_cashier / force_logout / deactivate."""
    _send(consumers.CASHIERS_GROUP, {'channel': 'cashiers', 'action': action, **data})
