"""Broadcast order lifecycle changes to the realtime displays.

A ``post_save`` on ``base.Order`` covers create + every status / payment update
(including sync-applied changes from other tills), so the KDS / order-queue stay
live without editing each service call site. Best-effort: a broadcast failure
never blocks the save.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from base.models import Order
from core.realtime.publish import publish_order_event

logger = logging.getLogger('core.realtime')


@receiver(post_save, sender=Order, dispatch_uid='realtime_order_broadcast')
def _broadcast_order(sender, instance, created, **kwargs):
    try:
        publish_order_event('created' if created else 'updated', {
            'id': instance.id,
            'uuid': str(instance.uuid),
            'status': getattr(instance, 'status', None),
            'is_paid': getattr(instance, 'is_paid', None),
            'total_amount': str(getattr(instance, 'total_amount', '') or ''),
            'display_id': getattr(instance, 'display_id', None),
            'table_id': getattr(instance, 'table_id', None),
        })
    except Exception:  # noqa: BLE001
        logger.debug('order broadcast signal failed', exc_info=True)
