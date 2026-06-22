"""Notification signals.

1. Staff order-notification trigger (SERVER edition only): post_save(Order) ->
   on_commit -> idempotent OrderNotification.dispatch. Covers server-native
   orders + the READY update; synced orders are also re-dispatched by the sync
   receiver once their item batch lands (dispatch's items-gate holds order.new
   until items are present).
2. Chat-config sync (any edition): NotificationChat is the admin-editable source
   of truth for chats + per-category routing; on change it rebuilds the derived
   NotificationSettings.chat_ids + chat_routing that the send path actually reads.
"""
import logging

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from base.models import Order

logger = logging.getLogger(__name__)


# ── 1. staff order notifications (server only) ──────────────────────────────
@receiver(post_save, sender=Order, dispatch_uid='staff_order_notify')
def _staff_order_notify(sender, instance, created, **kwargs):
    if getattr(django_settings, 'EDITION', '') != 'server':
        return
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


# ── 2. chat config -> derived NotificationSettings (any edition) ────────────
def _rebuild_chat_settings(*args, **kwargs):
    """Rebuild NotificationSettings.chat_ids (enabled chats) + chat_routing
    (per-chat event map) from the NotificationChat rows. Runs on every chat
    add/edit/delete so the send path's recipients_for stays accurate."""
    try:
        from notifications.models import NotificationSettings, NotificationChat
        chats = list(NotificationChat.objects.all())
        obj, _ = NotificationSettings.objects.get_or_create(pk=1)
        obj.chat_ids = [c.chat_id for c in chats if c.is_enabled]
        obj.chat_routing = {
            c.chat_id: {'label': c.label or '', 'events': c.events()}
            for c in chats
        }
        obj.save(update_fields=['chat_ids', 'chat_routing', 'updated_at'])
    except Exception:  # noqa: BLE001
        logger.warning('rebuild chat settings failed', exc_info=True)


def _connect_chat_sync():
    from notifications.models import NotificationChat
    post_save.connect(_rebuild_chat_settings, sender=NotificationChat,
                      dispatch_uid='notif_chat_sync_save')
    post_delete.connect(_rebuild_chat_settings, sender=NotificationChat,
                        dispatch_uid='notif_chat_sync_delete')


_connect_chat_sync()
