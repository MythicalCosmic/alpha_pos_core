"""Inline keyboard / callback_query routing tests.

Covers:
  - dispatcher recognizes callback_query updates
  - all registered callbacks (add, inc, dec, rm, clear, checkout) operate
    on the customer's cart and call answer_callback_query
  - /menu category and /order responses include the right inline_keyboard
    structure
  - unknown callback_data is still answered (so the spinner doesn't hang)
"""
import json

import pytest
from django.test import Client

from notifications.models import (
    Cart, CartItem, NotificationTemplate, TelegramCustomer,
)
from notifications.services import cart_service


pytestmark = pytest.mark.django_db


WEBHOOK_URL = '/api/telegram/webhook/'
SECRET = 'test-webhook-secret-token'


@pytest.fixture
def webhook_secret(settings):
    settings.TELEGRAM_WEBHOOK_SECRET = SECRET
    return SECRET


@pytest.fixture
def fake_telegram(monkeypatch):
    """Record all outbound Telegram calls so tests can assert without
    real network requests."""
    sent = []
    answered = []
    edited = []

    def fake_send(chat_id, text, reply_markup=None):
        sent.append({'chat_id': chat_id, 'text': text, 'reply_markup': reply_markup})
        return True, None

    def fake_answer(callback_id, text=None):
        answered.append({'callback_id': callback_id, 'text': text})
        return True, None

    def fake_edit(chat_id, message_id, text, reply_markup=None):
        edited.append({
            'chat_id': chat_id, 'message_id': message_id,
            'text': text, 'reply_markup': reply_markup,
        })
        return True, None

    from base.notifications.telegram import TelegramAPI
    monkeypatch.setattr(TelegramAPI, 'send_to_chat', staticmethod(fake_send))
    monkeypatch.setattr(TelegramAPI, 'answer_callback_query', staticmethod(fake_answer))
    monkeypatch.setattr(TelegramAPI, 'edit_message_text', staticmethod(fake_edit))
    return {'sent': sent, 'answered': answered, 'edited': edited}


@pytest.fixture(autouse=True)
def _templates(db):
    from notifications.models import NotificationTemplate
    NotificationTemplate.objects.create(
        notification_type='telegram.order_cart',
        name='cart', template_text='Cart: {items_list} total {total}',
    )
    NotificationTemplate.objects.create(
        notification_type='telegram.order_empty',
        name='empty', template_text='Empty',
    )
    NotificationTemplate.objects.create(
        notification_type='telegram.order_checked_out',
        name='checked', template_text='Placed #{display_id} total {total}',
    )
    NotificationTemplate.objects.create(
        notification_type='telegram.order_no_phone',
        name='nophone', template_text='Need login',
    )
    NotificationTemplate.objects.create(
        notification_type='telegram.menu_category',
        name='cat', template_text='{category_name}\n{products_list}',
    )


def _make_product(name='Pizza', price='50000'):
    from decimal import Decimal
    from base.models import Category, Product
    cat, _ = Category.objects.get_or_create(name='C', slug='c')
    return Product.objects.create(name=name, price=Decimal(price), category=cat)


def _make_customer(chat_id=555, phone='998900000001'):
    return TelegramCustomer.objects.create(
        chat_id=chat_id, first_name='Adrian', phone_number=phone,
    )


def _callback(chat_id=555, message_id=42, data='add:1'):
    return {
        'update_id': 1,
        'callback_query': {
            'id': 'cb-id-1',
            'from': {'id': chat_id, 'first_name': 'Adrian', 'is_bot': False},
            'message': {
                'message_id': message_id,
                'chat': {'id': chat_id, 'type': 'private'},
                'text': 'old',
            },
            'data': data,
            'chat_instance': '123',
        },
    }


def _post(client, body):
    return client.post(
        WEBHOOK_URL, data=json.dumps(body), content_type='application/json',
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET,
    )


class TestMenuKeyboard:
    def test_menu_category_includes_add_buttons(
        self, webhook_secret, fake_telegram,
    ):
        from base.models import Category
        cat = Category.objects.create(name='Pizza', slug='pizza')
        from decimal import Decimal
        from base.models import Product
        p = Product.objects.create(name='Margherita', price=Decimal('50000'), category=cat)
        _make_customer()

        client = Client()
        client.post(
            WEBHOOK_URL,
            data=json.dumps({
                'update_id': 1,
                'message': {
                    'message_id': 1,
                    'chat': {'id': 555, 'type': 'private'},
                    'from': {'id': 555, 'first_name': 'A'},
                    'text': '/menu pizza',
                },
            }),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET,
        )
        keyboard = fake_telegram['sent'][0]['reply_markup']
        assert keyboard is not None
        rows = keyboard['inline_keyboard']
        assert any(
            btn.get('callback_data') == f'add:{p.id}'
            for row in rows for btn in row
        )


class TestCallbackDispatch:
    def test_unknown_callback_still_answered(
        self, webhook_secret, fake_telegram,
    ):
        _make_customer()
        client = Client()
        _post(client, _callback(data='garbage'))
        # Spinner must always be dismissed.
        assert len(fake_telegram['answered']) == 1

    def test_blocked_customer_callback_ignored(
        self, webhook_secret, fake_telegram,
    ):
        TelegramCustomer.objects.create(
            chat_id=555, first_name='X', is_blocked=True,
        )
        client = Client()
        _post(client, _callback(data='clear'))
        # No cart-state edits, but still dismiss the spinner.
        assert len(fake_telegram['answered']) == 1
        assert len(fake_telegram['edited']) == 0


class TestCartCallbacks:
    def test_add_from_menu_creates_cart_and_replies(
        self, webhook_secret, fake_telegram,
    ):
        _make_customer()
        p = _make_product()
        client = Client()
        _post(client, _callback(data=f'add:{p.id}'))

        assert CartItem.objects.count() == 1
        # Brief cart status replied as a new message (not an edit).
        assert len(fake_telegram['sent']) == 1
        assert 'Cart' in fake_telegram['sent'][0]['text']
        # Inline keyboard attached so the customer can adjust qty.
        assert fake_telegram['sent'][0]['reply_markup'] is not None

    def test_inc_bumps_quantity_and_edits(
        self, webhook_secret, fake_telegram,
    ):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _callback(data=f'inc:{p.id}'))

        cart = Cart.objects.get(customer=c, status='ACTIVE')
        assert cart.items.first().quantity == 2
        assert len(fake_telegram['edited']) == 1

    def test_dec_decrements_quantity(self, webhook_secret, fake_telegram):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 3)
        client = Client()
        _post(client, _callback(data=f'dec:{p.id}'))

        cart = Cart.objects.get(customer=c, status='ACTIVE')
        assert cart.items.first().quantity == 2

    def test_dec_removes_row_when_quantity_hits_zero(
        self, webhook_secret, fake_telegram,
    ):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _callback(data=f'dec:{p.id}'))

        assert CartItem.objects.count() == 0

    def test_rm_removes_item(self, webhook_secret, fake_telegram):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 5)
        client = Client()
        _post(client, _callback(data=f'rm:{p.id}'))
        assert CartItem.objects.count() == 0

    def test_clear_empties_cart(self, webhook_secret, fake_telegram):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _callback(data='clear'))
        assert CartItem.objects.count() == 0

    def test_checkout_creates_order_and_edits_message(
        self, webhook_secret, fake_telegram,
    ):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 2)
        client = Client()
        _post(client, _callback(data='checkout'))

        from base.models import Order
        assert Order.objects.count() == 1
        assert len(fake_telegram['edited']) == 1
        # Confirmation has no keyboard.
        assert fake_telegram['edited'][0]['reply_markup'] is None

    def test_checkout_without_phone_warns_via_callback_toast(
        self, webhook_secret, fake_telegram,
    ):
        c = TelegramCustomer.objects.create(chat_id=555, first_name='A')
        p = _make_product()
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _callback(data='checkout'))

        # No order created.
        from base.models import Order
        assert Order.objects.count() == 0
        # Toast on the callback + a "need login" reply.
        assert fake_telegram['answered'][0]['text'] is not None
        assert any('Need login' in s['text'] for s in fake_telegram['sent'])

    def test_checkout_empty_cart_just_answers(
        self, webhook_secret, fake_telegram,
    ):
        _make_customer()
        client = Client()
        _post(client, _callback(data='checkout'))
        from base.models import Order
        assert Order.objects.count() == 0
        assert fake_telegram['answered'][0]['text'] is not None


class TestOrderCommandAttachesKeyboard:
    def test_order_show_cart_includes_keyboard(
        self, webhook_secret, fake_telegram,
    ):
        c = _make_customer()
        p = _make_product()
        cart_service.add_item(c, p.id, 1)
        client = Client()
        client.post(
            WEBHOOK_URL,
            data=json.dumps({
                'update_id': 1,
                'message': {
                    'message_id': 1,
                    'chat': {'id': 555, 'type': 'private'},
                    'from': {'id': 555, 'first_name': 'A'},
                    'text': '/order',
                },
            }),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET,
        )
        kb = fake_telegram['sent'][0]['reply_markup']
        assert kb is not None
        # Has at least the bottom row with Checkout button.
        assert any(
            btn.get('callback_data') == 'checkout'
            for row in kb['inline_keyboard'] for btn in row
        )
