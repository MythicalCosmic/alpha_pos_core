"""Server-side staff order lifecycle notifications.

Creation sends one message per chat; READY edits those same messages in place.
The tests cover edition gating, idempotency, multi-chat partial failure, durable
retry ordering, and Telegram's idempotent edit response.
"""
import importlib
import time

import pytest
import requests
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
def test_dispatch_is_idempotent_and_ready_uses_edit(server_edition, monkeypatch):
    sent = []

    def fake_send(cls, notification_type, context, order_id=None, thread_role=None):
        sent.append((notification_type, order_id, thread_role, context))
        return True

    monkeypatch.setattr(SenderService, 'send', classmethod(fake_send))

    o = _order('PREPARING')
    OrderNotification.dispatch(o)
    assert [s[0] for s in sent] == ['order.new']
    assert sent[0][1] == o.id and sent[0][2] == 'new'
    assert sent[0][3]['accepted_at'] != '—'

    OrderNotification.dispatch(o)                          # idempotent — no resend
    assert len(sent) == 1

    o.status = 'READY'
    o.ready_at = timezone.now()
    o.save(update_fields=['status', 'ready_at'])
    OrderNotification.dispatch(o)
    assert [s[0] for s in sent] == ['order.new', 'order.ready']
    assert sent[1][2] == 'edit'
    assert sent[1][3]['accepted_at'] != '—'
    assert sent[1][3]['ready_at'] != '—'
    assert sent[1][3]['cashier_name'] == 'C X'

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
        lambda cls, ntype, ctx, order_id=None, thread_role=None:
        (sent.append(ntype) or True)))

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
def test_order_new_held_when_only_item_is_soft_deleted(server_edition, monkeypatch):
    sent = []
    monkeypatch.setattr(SenderService, 'send', classmethod(
        lambda cls, ntype, ctx, order_id=None, thread_role=None:
        (sent.append(ntype) or True)))

    order = _order('PREPARING')
    order.items.get().delete()

    OrderNotification.dispatch(order)

    assert sent == []
    dispatch = OrderNotificationDispatch.objects.get(order_id=order.id)
    assert dispatch.new_sent is False


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
def test_worker_stores_new_message_ids_then_edits(server_edition, monkeypatch):
    NotificationSettings.objects.update_or_create(
        pk=1, defaults={'bot_token': 'x', 'chat_ids': ['111'], 'is_enabled': True})
    NotificationTemplate.objects.get_or_create(
        notification_type='order.new', defaults={'name': 'n', 'template_text': 'NEW'})
    NotificationTemplate.objects.get_or_create(
        notification_type='order.ready', defaults={'name': 'r', 'template_text': 'READY'})

    sent_calls = []
    edit_calls = []

    def fake_send_to_chats(cls, text, chat_ids, reply_to=None):
        sent_calls.append({
            'text': text,
            'chat_ids': list(chat_ids),
            'reply_to': reply_to,
        })
        # all chats accept; Telegram returns message_id 555 for each
        return [], '', {str(c): 555 for c in chat_ids}

    def fake_edit_in_chats(cls, text, message_ids, chat_ids=None):
        edit_calls.append({
            'text': text,
            'message_ids': dict(message_ids),
            'chat_ids': list(chat_ids or []),
        })
        return [], ''

    monkeypatch.setattr(TelegramService, 'send_to_chats', classmethod(fake_send_to_chats))
    monkeypatch.setattr(TelegramService, 'edit_in_chats', classmethod(fake_edit_in_chats))

    # 1) the order.new send stores the per-chat message id
    worker._dispatch({'text': 'NEW', 'notification_type': 'order.new',
                      'order_id': 7, 'thread_role': 'new'}, 0)
    disp = OrderNotificationDispatch.objects.get(order_id=7)
    assert disp.new_recipient_ids == ['111']
    assert disp.new_message_ids == {'111': 555}

    # Changing routing mid-preparation must not leave chat 111's original card
    # stale or attempt an impossible edit in newly added chat 222.
    settings_row = NotificationSettings.load()
    settings_row.chat_ids = ['222']
    settings_row.save(update_fields=['chat_ids', 'updated_at'])

    # 2) order.ready edits that exact message; it does not send another one.
    worker._dispatch({'text': 'READY', 'notification_type': 'order.ready',
                      'order_id': 7, 'thread_role': 'edit'}, 0)
    assert len(sent_calls) == 1
    assert edit_calls == [{
        'text': 'READY',
        'message_ids': {'111': 555},
        'chat_ids': ['111'],
    }]


@pytest.mark.django_db
def test_partial_new_delivery_retries_only_missing_root_then_edits_it(
    server_edition,
    monkeypatch,
):
    """Chat A must never get a duplicate while chat B recovers in queue order:
    send B's missing root first, store its id, then edit only B."""
    from notifications.services.queue_service import QueueService

    QueueService.clear()
    QueueService.clear_dead_letters()
    monkeypatch.setattr(
        QueueService,
        '_retry_delay',
        staticmethod(lambda attempts: 0),
    )
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'x',
            'chat_ids': ['111', '222'],
            'is_enabled': True,
        },
    )

    send_calls = []
    edit_calls = []

    def first_send(cls, text, chat_ids, reply_to=None):
        targets = list(chat_ids)
        send_calls.append(targets)
        if targets == ['111', '222']:
            return ['222'], 'Timeout', {'111': 101}
        assert targets == ['222']
        return [], '', {'222': 202}

    def edit_messages(cls, text, message_ids, chat_ids=None):
        targets = list(chat_ids or [])
        edit_calls.append(targets)
        missing = [chat_id for chat_id in targets if chat_id not in message_ids]
        return missing, 'Original order message is not available yet' if missing else ''

    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(first_send),
    )
    monkeypatch.setattr(
        TelegramService,
        'edit_in_chats',
        classmethod(edit_messages),
    )
    monkeypatch.setattr(
        TelegramService,
        'is_online',
        classmethod(lambda cls: True),
    )

    worker._dispatch({
        'text': 'NEW',
        'notification_type': 'order.new',
        'order_id': 88,
        'thread_role': 'new',
    }, 0)
    worker._dispatch({
        'text': 'READY',
        'notification_type': 'order.ready',
        'order_id': 88,
        'thread_role': 'edit',
    }, 0)

    pending = QueueService.get_all()
    assert [(item['thread_role'], item['chat_ids']) for item in pending] == [
        ('new', ['222']),
        ('edit', ['222']),
    ]
    assert send_calls == [['111', '222']]
    assert edit_calls == [['111', '222']]

    sent, failed = QueueService.process()

    assert (sent, failed) == (2, 0)
    assert send_calls == [['111', '222'], ['222']]
    assert edit_calls == [['111', '222'], ['222']]
    dispatch = OrderNotificationDispatch.objects.get(order_id=88)
    assert dispatch.new_recipient_ids == ['111', '222']
    assert dispatch.new_message_ids == {'111': 101, '222': 202}
    assert QueueService.get_all() == []


@pytest.mark.django_db
def test_queue_atomic_drain_merge_preserves_add_during_transport(
    server_edition,
    monkeypatch,
):
    """A web-worker add during a retry pass survives the pass's final merge."""
    from notifications.services.queue_service import QueueService

    QueueService.clear()
    QueueService.clear_dead_letters()
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'x',
            'chat_ids': ['111'],
            'is_enabled': True,
        },
    )
    QueueService.add(
        'FIRST',
        'test',
        chat_ids=['111'],
    )

    def send_and_concurrently_add(cls, text, chat_ids, reply_to=None):
        assert text == 'FIRST'
        QueueService.add(
            'CONCURRENT',
            'test',
            chat_ids=['111'],
        )
        return [], '', {}

    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(send_and_concurrently_add),
    )

    sent, pending = QueueService.process()

    assert sent == 1
    assert pending == 1
    assert [item['message'] for item in QueueService.get_all()] == [
        'CONCURRENT',
    ]


@pytest.mark.django_db
def test_future_retries_do_not_starve_due_work_behind_batch_limit(
    server_edition,
    monkeypatch,
):
    """A wall of backed-off retries must not block a newly due notification."""
    from notifications.services.queue_service import (
        MAX_PROCESS_BATCH,
        QueueService,
    )

    QueueService.clear()
    QueueService.clear_dead_letters()
    future = time.time() + 600
    for index in range(MAX_PROCESS_BATCH):
        QueueService.add(
            f'FUTURE-{index}',
            'test',
            chat_ids=['111'],
            next_attempt_at=future,
        )
    QueueService.add('DUE-NOW', 'test', chat_ids=['111'])

    sends = []
    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(
            lambda cls, text, chat_ids, reply_to=None:
            (sends.append(text) or ([], '', {}))
        ),
    )

    completed, pending = QueueService.process()

    assert completed == 1
    assert pending == MAX_PROCESS_BATCH
    assert sends == ['DUE-NOW']
    assert all(
        item['message'].startswith('FUTURE-')
        for item in QueueService.get_all()
    )


@pytest.mark.django_db
def test_retry_batch_http_budget_stays_inside_process_lease(
    server_edition,
    monkeypatch,
):
    from notifications.services.queue_service import (
        MAX_TELEGRAM_REQUESTS_PER_BATCH,
        QueueService,
    )

    QueueService.clear()
    QueueService.clear_dead_letters()
    for index in range(MAX_TELEGRAM_REQUESTS_PER_BATCH + 1):
        QueueService.add(
            f'ORDER-{index}',
            'test',
            chat_ids=['111'],
        )

    sends = []
    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(
            lambda cls, text, chat_ids, reply_to=None:
            (sends.append(text) or ([], '', {}))
        ),
    )

    completed, pending = QueueService.process()

    assert completed == MAX_TELEGRAM_REQUESTS_PER_BATCH
    assert pending == 1
    assert len(sends) == MAX_TELEGRAM_REQUESTS_PER_BATCH
    assert QueueService.get_all()[0]['message'] == (
        f'ORDER-{MAX_TELEGRAM_REQUESTS_PER_BATCH}'
    )


@pytest.mark.django_db
def test_queue_process_lease_prevents_a_second_consumer(
    server_edition,
    monkeypatch,
):
    from notifications.services.queue_service import QueueService

    QueueService.clear()
    QueueService.clear_dead_letters()
    QueueService.add('ONE', 'test', chat_ids=['111'])
    sends = []
    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(
            lambda cls, text, chat_ids, reply_to=None:
            (sends.append(text) or ([], '', {}))
        ),
    )

    with QueueService._process_lock() as acquired:
        assert acquired is True
        assert QueueService.process() == (0, 1)
        assert sends == []

    assert QueueService.process() == (1, 0)
    assert sends == ['ONE']


@pytest.mark.django_db
def test_delivered_root_retries_only_id_persistence(
    server_edition,
    monkeypatch,
):
    """A DB failure after Telegram success must never resend the root."""
    from notifications.services.queue_service import QueueService

    QueueService.clear()
    QueueService.clear_dead_letters()
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'x',
            'chat_ids': ['111'],
            'is_enabled': True,
        },
    )

    sends = []
    store_attempts = []

    def send_once(cls, text, chat_ids, reply_to=None):
        sends.append((text, list(chat_ids)))
        return [], '', {'111': 901}

    def flaky_store(order_id, sent_ids):
        store_attempts.append((order_id, dict(sent_ids)))
        return len(store_attempts) > 1

    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(send_once),
    )
    monkeypatch.setattr(worker, '_store_message_ids', flaky_store)

    worker._dispatch({
        'text': 'NEW',
        'notification_type': 'order.new',
        'order_id': 901,
        'thread_role': 'new',
    }, 0)

    queued = QueueService.get_all()
    assert len(queued) == 1
    assert queued[0]['thread_role'] == 'persist'
    assert queued[0]['sent_ids'] == {'111': 901}

    completed, pending = QueueService.process()

    assert (completed, pending) == (1, 0)
    assert sends == [('NEW', ['111'])]  # no second sendMessage call
    assert len(store_attempts) == 2


@pytest.mark.django_db
def test_legacy_ready_reply_retry_is_upgraded_to_edit(
    server_edition,
    monkeypatch,
):
    from notifications.services.queue_service import QueueService

    QueueService.clear()
    QueueService.clear_dead_letters()
    OrderNotificationDispatch.objects.create(
        order_id=902,
        new_sent=True,
        new_recipient_ids=['111'],
        new_message_ids={'111': 77},
    )
    QueueService.add(
        'READY',
        'order.ready',
        chat_ids=['111'],
        order_id=902,
        thread_role='reply',  # pre-0012 queue payload
    )

    edits = []
    monkeypatch.setattr(
        TelegramService,
        'edit_in_chats',
        classmethod(
            lambda cls, text, message_ids, chat_ids=None:
            (edits.append((text, dict(message_ids), list(chat_ids))) or ([], ''))
        ),
    )
    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(
            lambda cls, *args, **kwargs:
            pytest.fail('legacy READY retry must not call sendMessage')
        ),
    )

    completed, pending = QueueService.process()

    assert (completed, pending) == (1, 0)
    assert edits == [('READY', {'111': 77}, ['111'])]


@pytest.mark.django_db
def test_retry_backoff_is_bounded_and_permanent_failure_dead_letters(
    server_edition,
    monkeypatch,
):
    from notifications.services.queue_service import (
        MAX_ATTEMPTS,
        MAX_RETRY_SECONDS,
        QueueService,
    )

    QueueService.clear()
    QueueService.clear_dead_letters()
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'x',
            'chat_ids': ['111'],
            'is_enabled': True,
        },
    )
    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(
            lambda cls, text, chat_ids, reply_to=None:
            (list(chat_ids), 'Timeout', {})
        ),
    )

    QueueService.add('TRANSIENT', 'test', chat_ids=['111'])
    before = time.time()
    completed, pending = QueueService.process()
    retry = QueueService.get_all()[0]

    assert (completed, pending) == (0, 1)
    assert retry['attempts'] == 1
    assert retry['next_attempt_at'] >= before
    assert QueueService._retry_delay(100) == MAX_RETRY_SECONDS
    assert QueueService.dead_letter_count() == 0

    QueueService.clear()
    QueueService.add(
        'BAD HTML',
        'test',
        chat_ids=['111'],
        attempts=MAX_ATTEMPTS - 1,
    )
    monkeypatch.setattr(
        TelegramService,
        'send_to_chats',
        classmethod(
            lambda cls, text, chat_ids, reply_to=None:
            (list(chat_ids), 'HTTP 400: Bad Request: cannot parse entities', {})
        ),
    )

    completed, pending = QueueService.process()

    assert (completed, pending) == (0, 0)
    assert QueueService.get_all() == []
    dead = QueueService.get_dead_letters()
    assert len(dead) == 1
    assert dead[0]['message'] == 'BAD HTML'
    assert dead[0]['attempts'] == MAX_ATTEMPTS


@pytest.mark.django_db
def test_disabled_or_recipientless_creation_is_not_marked_sent(
    server_edition,
    monkeypatch,
):
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'x',
            'chat_ids': [],
            'is_enabled': True,
        },
    )
    NotificationTemplate.objects.update_or_create(
        notification_type='order.new',
        defaults={'name': 'new', 'template_text': 'NEW {brand}'},
    )
    async_calls = []
    monkeypatch.setattr(
        SenderService,
        '_send_async',
        classmethod(lambda cls, *args, **kwargs: async_calls.append(args)),
    )

    assert SenderService.send(
        'order.new',
        {},
        order_id=903,
        thread_role='new',
    ) is False
    assert async_calls == []

    order = _order('PREPARING')
    monkeypatch.setattr(
        SenderService,
        'send',
        classmethod(lambda cls, *args, **kwargs: False),
    )
    OrderNotification.dispatch(order)

    dispatch = OrderNotificationDispatch.objects.get(order_id=order.id)
    assert dispatch.new_sent is False


@pytest.mark.django_db
def test_template_migration_preserves_custom_rows_and_has_safe_reverse():
    migration = importlib.import_module(
        'notifications.migrations.0012_order_lifecycle_message_edit',
    )
    previous = migration.PREVIOUS_TEMPLATES['order.ready']
    current = migration.NEW_TEMPLATES['order.ready']

    row, _ = NotificationTemplate.objects.update_or_create(
        notification_type='order.ready',
        defaults=previous,
    )
    migration.apply_new_templates(importlib.import_module('django.apps').apps, None)
    row.refresh_from_db()
    assert row.template_text == current['template_text']

    # Exact new defaults reverse to the old context contract, which is the
    # required first step before rolling binaries back.
    migration.restore_previous_templates(
        importlib.import_module('django.apps').apps,
        None,
    )
    row.refresh_from_db()
    assert row.template_text == previous['template_text']

    custom = '<b>Operator custom READY</b>'
    row.template_text = custom
    row.save(update_fields=['template_text', 'updated_at'])
    migration.apply_new_templates(importlib.import_module('django.apps').apps, None)
    row.refresh_from_db()
    assert row.template_text == custom

    # A post-deploy operator edit is also preserved by reverse migration.
    row.name = current['name']
    row.description = current['description']
    row.template_text = current['template_text']
    row.save(update_fields=['name', 'description', 'template_text', 'updated_at'])
    row.template_text = '<b>Edited after deploy</b>'
    row.save(update_fields=['template_text', 'updated_at'])
    migration.restore_previous_templates(
        importlib.import_module('django.apps').apps,
        None,
    )
    row.refresh_from_db()
    assert row.template_text == '<b>Edited after deploy</b>'


@pytest.mark.django_db
def test_edit_api_is_per_chat_and_message_not_modified_is_success(
    server_edition,
    monkeypatch,
):
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'secret',
            'chat_ids': ['111', '222', '333'],
            'is_enabled': True,
            'timeout': 999,
        },
    )
    payloads = []
    timeouts = []

    class Response:
        def __init__(self, status_code, description=''):
            self.status_code = status_code
            self.ok = status_code == 200
            self.text = description
            self._description = description

        def json(self):
            return {
                'ok': self.ok,
                'description': self._description,
            }

    responses = iter([
        Response(200),
        Response(400, 'Bad Request: message is not modified'),
        Response(429, 'Too Many Requests'),
    ])

    def fake_post(url, json, timeout):
        assert url.endswith('/editMessageText')
        payloads.append(json)
        timeouts.append(timeout)
        return next(responses)

    monkeypatch.setattr(requests, 'post', fake_post)

    failed, error = TelegramService.edit_in_chats(
        'READY',
        {'111': 11, '222': 22, '333': 33},
        chat_ids=['111', '222', '333'],
    )

    assert failed == ['333']
    assert '429' in error
    assert [payload['message_id'] for payload in payloads] == [11, 22, 33]
    assert all(payload['parse_mode'] == 'HTML' for payload in payloads)
    assert timeouts == [20, 20, 20]


@pytest.mark.django_db
def test_edit_api_missing_id_and_timeout_never_send_a_second_message(
    server_edition,
    monkeypatch,
):
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'secret',
            'chat_ids': ['111', '222'],
            'is_enabled': True,
        },
    )
    posts = []

    def timeout_post(url, json, timeout):
        posts.append(json)
        raise requests.Timeout()

    monkeypatch.setattr(requests, 'post', timeout_post)

    failed, error = TelegramService.edit_in_chats(
        'READY',
        {'111': 11},
        chat_ids=['111', '222'],
    )

    assert failed == ['111', '222']
    assert error == 'Original order message is not available yet'
    # Only an edit was attempted for chat 111; chat 222 had no root id and no
    # fallback sendMessage call was made.
    assert posts == [{
        'chat_id': '111',
        'message_id': 11,
        'text': 'READY',
        'parse_mode': 'HTML',
    }]


def test_retry_worker_once_drains_modern_staff_queue(monkeypatch):
    from django.core.management import call_command
    from notifications.services.queue_service import QueueService

    calls = []
    monkeypatch.setattr(
        QueueService,
        'count',
        classmethod(lambda cls: 2),
    )
    monkeypatch.setattr(
        QueueService,
        'process',
        classmethod(lambda cls: calls.append('processed') or (2, 0)),
    )

    call_command('notification_retry_worker', '--once')

    assert calls == ['processed']


@pytest.mark.django_db
def test_polished_ready_template_renders_complete_replacement_card(
    server_edition,
    monkeypatch,
):
    from notifications.management.commands.seed_templates import TEMPLATES

    ready_template = next(
        template
        for template in TEMPLATES
        if template['notification_type'] == 'order.ready'
    )
    NotificationTemplate.objects.update_or_create(
        notification_type='order.ready',
        defaults={
            'name': ready_template['name'],
            'description': ready_template['description'],
            'template_text': ready_template['template_text'],
        },
    )
    NotificationSettings.objects.update_or_create(
        pk=1,
        defaults={
            'bot_token': 'x',
            'chat_ids': ['111'],
            'brand_name': 'Ruxsora',
            'is_enabled': True,
        },
    )
    rendered = []
    monkeypatch.setattr(
        SenderService,
        '_send_async',
        classmethod(
            lambda cls, text, notification_type, order_id=None, thread_role=None:
            rendered.append((text, notification_type, order_id, thread_role))
        ),
    )

    SenderService.send('order.ready', {
        'display_id': 42,
        'cashier_name': 'Dilnoza',
        'order_type': 'Zalda',
        'prep_time': '12:34',
        'total_amount': '296,000',
        'items_list': '  • Osh x2 — 296,000 so\'m',
        'accepted_at': '2026-07-26 14:01:00',
        'ready_at': '2026-07-26 14:13:34',
        'time': '14:13:34',
    }, order_id=42, thread_role='edit')

    assert len(rendered) == 1
    text, notification_type, order_id, thread_role = rendered[0]
    assert (notification_type, order_id, thread_role) == (
        'order.ready',
        42,
        'edit',
    )
    assert '✅ <b>BUYURTMA TAYYOR</b>' in text
    assert '2026-07-26 14:01:00' in text
    assert '2026-07-26 14:13:34' in text
    assert '12:34' in text
    assert 'Dilnoza' in text
    assert '296,000' in text
    assert 'Ruxsora' in text


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
