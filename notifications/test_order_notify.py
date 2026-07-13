"""Server-side staff order notifications: edition gating, idempotent dispatch,
and the new -> ready reply threading (capture order.new message ids, reply
order.ready under them)."""
import pytest
from django.utils import timezone

from base.models import User, Order, OrderItem, Product, Category
from notifications.models import (
    NotificationSettings, NotificationTemplate, OrderNotificationDispatch,
)
from notifications.handlers.order import OrderNotification
from notifications.services.sender_service import SenderService
from notifications.services.telegram_service import TelegramService
from notifications.services import worker


@pytest.fixture
def server_edition(settings):
    settings.EDITION = 'server'
    return settings


def _order(status='PREPARING', with_items=True):
    u = User.objects.create(first_name='C', last_name='X', email=f'{status}-{timezone.now().timestamp()}@x.com',
                            password='x', role='CASHIER', status='ACTIVE')
    o = Order.objects.create(user=u, cashier=u, status=status, display_id=7,
                             subtotal='10.00', total_amount='10.00',
                             ready_at=timezone.now() if status == 'READY' else None)
    if with_items:
        cat = Category.objects.create(name='Drinks')
        p = Product.objects.create(name='Coffee', price='10.00', category=cat)
        OrderItem.objects.create(order=o, product=p, quantity=2, price='10.00')
    return o


@pytest.mark.django_db
def test_dispatch_noop_on_local_edition(settings, monkeypatch):
    settings.EDITION = 'local'
    sent = []
    monkeypatch.setattr(SenderService, 'send',
                        classmethod(lambda cls, *a, **k: sent.append(a)))
    OrderNotification.dispatch(_order('PREPARING'))
    assert sent == []                      # the till never sends
    assert OrderNotificationDispatch.objects.count() == 0


@pytest.mark.django_db
def test_dispatch_is_idempotent_and_threads(server_edition, monkeypatch):
    sent = []

    def fake_send(cls, notification_type, context, order_id=None, thread_role=None):
        sent.append((notification_type, order_id, thread_role))

    monkeypatch.setattr(SenderService, 'send', classmethod(fake_send))

    o = _order('PREPARING')
    OrderNotification.dispatch(o)
    assert [s[0] for s in sent] == ['order.new']
    assert sent[0][1] == o.id and sent[0][2] == 'new'     # threaded as the root

    OrderNotification.dispatch(o)                          # idempotent — no resend
    assert len(sent) == 1

    o.status = 'READY'
    o.ready_at = timezone.now()
    o.save(update_fields=['status', 'ready_at'])
    OrderNotification.dispatch(o)
    assert [s[0] for s in sent] == ['order.new', 'order.ready']
    assert sent[1][2] == 'reply'                          # ready replies to new

    OrderNotification.dispatch(o)                          # ready is idempotent too
    assert len(sent) == 2

    disp = OrderNotificationDispatch.objects.get(order_id=o.id)
    assert disp.new_sent and disp.ready_sent


@pytest.mark.django_db
def test_order_new_held_until_items_present(server_edition, monkeypatch):
    """On the cloud the order syncs in a batch BEFORE its items, so a freshly
    received order has none. order.new must be HELD (not sent with an empty item
    list, not marked new_sent) until the items land — then it fires."""
    sent = []
    monkeypatch.setattr(SenderService, 'send', classmethod(
        lambda cls, ntype, ctx, order_id=None, thread_role=None: sent.append(ntype)))

    o = _order('PREPARING', with_items=False)     # order applied; items not yet
    OrderNotification.dispatch(o)
    assert sent == []                              # held — no empty order.new
    disp = OrderNotificationDispatch.objects.filter(order_id=o.id).first()
    assert disp is not None and disp.new_sent is False

    cat = Category.objects.create(name='c')
    p = Product.objects.create(name='Tea', price='5.00', category=cat)
    OrderItem.objects.create(order=o, product=p, quantity=1, price='5.00')
    OrderNotification.dispatch(o)                  # item batch landed -> re-dispatch
    assert sent == ['order.new']
    disp.refresh_from_db()
    assert disp.new_sent is True


@pytest.mark.django_db
def test_notification_chat_syncs_settings_and_routing():
    """Editing NotificationChat rows rebuilds the derived chat_ids + chat_routing
    that the send path reads — so the admin is the single editable surface."""
    from notifications.models import NotificationChat
    NotificationChat.objects.create(chat_id='111', label='Owner', recv_shifts=False)
    NotificationChat.objects.create(chat_id='222', is_enabled=False)  # disabled

    s = NotificationSettings.load()
    assert s.chat_ids == ['111']                         # only enabled chats
    assert s.recipients_for('order_paid') == ['111']     # orders on
    assert s.recipients_for('daily') == []               # 111 muted shift/daily

    NotificationChat.objects.filter(chat_id='111').delete()
    assert NotificationSettings.load().chat_ids == []     # delete rebuilds too


@pytest.mark.django_db
def test_worker_stores_new_message_ids_then_replies(server_edition, monkeypatch):
    NotificationSettings.objects.update_or_create(
        pk=1, defaults={'bot_token': 'x', 'chat_ids': ['111'], 'is_enabled': True})
    NotificationTemplate.objects.get_or_create(
        notification_type='order.new', defaults={'name': 'n', 'template_text': 'NEW'})
    NotificationTemplate.objects.get_or_create(
        notification_type='order.ready', defaults={'name': 'r', 'template_text': 'READY'})

    captured = {}

    def fake_send_to_chats(cls, text, chat_ids, reply_to=None):
        captured['reply_to'] = reply_to
        # all chats accept; Telegram returns message_id 555 for each
        return [], '', {str(c): 555 for c in chat_ids}

    monkeypatch.setattr(TelegramService, 'send_to_chats', classmethod(fake_send_to_chats))

    # 1) the order.new send stores the per-chat message id
    worker._dispatch({'text': 'NEW', 'notification_type': 'order.new',
                      'order_id': 7, 'thread_role': 'new'}, 0)
    disp = OrderNotificationDispatch.objects.get(order_id=7)
    assert disp.new_message_ids == {'111': 555}

    # 2) the order.ready send replies under the stored message id
    worker._dispatch({'text': 'READY', 'notification_type': 'order.ready',
                      'order_id': 7, 'thread_role': 'reply'}, 0)
    assert captured['reply_to'] == {'111': 555}


@pytest.mark.django_db
def test_delivery_log_failure_never_requeues_a_successful_send(
    server_edition, monkeypatch,
):
    from django.db.models import TextField
    from notifications.models import NotificationLog
    from notifications.services.queue_service import QueueService

    chat_ids = ['1111111111', '2222222222', '3333333333', '4444444444', '5555555555']
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={'bot_token': 'x', 'chat_ids': chat_ids, 'is_enabled': True},
    )
    monkeypatch.setattr(
        TelegramService, 'send_to_chats',
        classmethod(lambda cls, text, chats, reply_to=None: ([], '', {})),
    )
    monkeypatch.setattr(
        NotificationLog.objects, 'create',
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError('log DB unavailable')),
    )
    requeued = []
    monkeypatch.setattr(
        QueueService, 'add',
        classmethod(lambda cls, *args, **kwargs: requeued.append((args, kwargs))),
    )

    worker._dispatch({'text': 'ok', 'notification_type': 'test'}, 0)

    assert requeued == []
    assert isinstance(NotificationLog._meta.get_field('recipient'), TextField)
