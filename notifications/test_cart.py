"""Cart service + /order bot command tests.

Service surface (cart_service.add_item / remove_item / clear / checkout)
is exercised directly; /order is hit through the Telegram webhook with
the existing fake-send pattern.
"""
import json

import pytest
from django.test import Client

from notifications.models import (
    Cart, CartItem, NotificationTemplate, TelegramCustomer,
)
from notifications.services import cart_service


pytestmark = pytest.mark.django_db


def _make_product(name, price, slug='cat'):
    from decimal import Decimal
    from base.models import Category, Product
    cat, _ = Category.objects.get_or_create(name='C', slug=slug)
    return Product.objects.create(
        name=name, price=Decimal(price), category=cat,
    )


def _make_customer(chat_id=555, phone='998900000001'):
    return TelegramCustomer.objects.create(
        chat_id=chat_id, first_name='Adrian', phone_number=phone,
    )


# ---- service unit tests ---------------------------------------------------

class TestCartService:
    def test_add_creates_cart_and_item(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart, product = cart_service.add_item(c, p.id, 2)
        assert cart.items.count() == 1
        item = cart.items.first()
        assert item.product_id == p.id
        assert item.quantity == 2
        assert item.price == p.price

    def test_add_bumps_existing_quantity_not_duplicates_row(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        cart_service.add_item(c, p.id, 3)
        cart = Cart.objects.get(customer=c, status='ACTIVE')
        assert cart.items.count() == 1
        assert cart.items.first().quantity == 4

    def test_add_clamps_quantity_to_one(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 0)
        assert cart_service.get_or_create_active_cart(c).items.first().quantity == 1

    def test_add_returns_error_for_unknown_product(self):
        c = _make_customer()
        cart, err = cart_service.add_item(c, 999999, 1)
        assert cart is None
        assert err == 'product_not_found'

    def test_add_skips_deleted_product(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        p.is_deleted = True
        p.save()
        cart, err = cart_service.add_item(c, p.id, 1)
        assert cart is None and err == 'product_not_found'

    def test_remove_item(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        assert cart_service.remove_item(c, p.id) is True
        assert CartItem.objects.filter(cart__customer=c).count() == 0

    def test_remove_missing_item_returns_false(self):
        c = _make_customer()
        assert cart_service.remove_item(c, 999) is False

    def test_clear_empties_cart(self):
        c = _make_customer()
        p1 = _make_product('Pizza', '50000')
        p2 = _make_product('Salad', '30000', slug='salads')
        cart_service.add_item(c, p1.id, 1)
        cart_service.add_item(c, p2.id, 1)
        cart_service.clear(c)
        assert CartItem.objects.filter(cart__customer=c).count() == 0

    def test_cart_total_sums_subtotals(self):
        c = _make_customer()
        p1 = _make_product('Pizza', '50000')
        p2 = _make_product('Salad', '30000', slug='salads')
        cart_service.add_item(c, p1.id, 2)
        cart_service.add_item(c, p2.id, 1)
        cart = cart_service.get_or_create_active_cart(c)
        from decimal import Decimal
        assert cart_service.cart_total(cart) == Decimal('130000')


class TestCheckout:
    def test_empty_cart_returns_error(self):
        c = _make_customer()
        order, err = cart_service.checkout(c)
        assert order is None and err == 'empty'

    def test_checkout_creates_order_with_items(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 2)
        order, err = cart_service.checkout(c)
        assert err is None
        assert order is not None
        assert order.items.count() == 1
        item = order.items.first()
        assert item.quantity == 2
        assert item.price == p.price
        from decimal import Decimal
        assert order.total_amount == Decimal('100000')
        assert order.status == 'OPEN'
        assert order.is_paid is False
        assert order.order_type == 'PICKUP'

    def test_checkout_provisions_user_for_new_customer(self):
        from base.models import User
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        cart_service.checkout(c)
        c.refresh_from_db()
        assert c.user is not None
        assert c.user.email == f'tg-{c.chat_id}@telegram.local'
        assert c.user.role == User.RoleChoices.USER

    def test_checkout_reuses_existing_user_on_second_checkout(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        cart_service.checkout(c)
        first_user_id = c.user_id
        cart_service.add_item(c, p.id, 1)
        cart_service.checkout(c)
        c.refresh_from_db()
        assert c.user_id == first_user_id

    def test_checkout_marks_cart_checked_out(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        order, _ = cart_service.checkout(c)
        cart = Cart.objects.filter(customer=c).first()
        assert cart.status == 'CHECKED_OUT'
        assert cart.order_id == order.id

    def test_checkout_starts_new_cart_after_previous_checkout(self):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        cart_service.checkout(c)
        # Next add() should make a fresh ACTIVE cart.
        cart, _ = cart_service.add_item(c, p.id, 1)
        assert cart.status == 'ACTIVE'
        assert Cart.objects.filter(customer=c).count() == 2

    def test_checkout_without_phone_requires_login(self):
        c = TelegramCustomer.objects.create(chat_id=555, first_name='Adrian')
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        order, err = cart_service.checkout(c)
        assert order is None
        assert err == 'no_phone'


# ---- /order bot command --------------------------------------------------

WEBHOOK_URL = '/api/telegram/webhook/'
SECRET = 'test-webhook-secret-token'


@pytest.fixture
def webhook_secret(settings):
    settings.TELEGRAM_WEBHOOK_SECRET = SECRET
    return SECRET


@pytest.fixture
def patched_send(monkeypatch):
    sent = []

    def fake_send(chat_id, text, reply_markup=None):
        sent.append({'chat_id': chat_id, 'text': text})
        return True, None

    from base.notifications.telegram import TelegramAPI
    monkeypatch.setattr(TelegramAPI, 'send_to_chat', staticmethod(fake_send))
    return sent


@pytest.fixture(autouse=True)
def _order_templates(db):
    for t in [
        ('telegram.order_cart', 'Cart: {items_list} total {total}'),
        ('telegram.order_empty', 'Empty'),
        ('telegram.order_added', 'Added {product_name} x{quantity}'),
        ('telegram.order_removed', 'Removed {product_id}'),
        ('telegram.order_cleared', 'Cleared'),
        ('telegram.order_checked_out', 'Placed #{display_id} total {total}'),
        ('telegram.order_help', 'Help text'),
        ('telegram.order_no_phone', 'Need login'),
        ('telegram.order_invalid_product', 'No product {product_id}'),
    ]:
        NotificationTemplate.objects.create(
            notification_type=t[0], name=t[0], template_text=t[1],
        )


def _order_update(chat_id=555, text='/order'):
    return {
        'update_id': 1,
        'message': {
            'message_id': 1,
            'chat': {'id': chat_id, 'type': 'private'},
            'from': {'id': chat_id, 'first_name': 'Adrian', 'is_bot': False},
            'text': text,
        },
    }


def _post(client, body):
    return client.post(
        WEBHOOK_URL, data=json.dumps(body), content_type='application/json',
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET,
    )


class TestOrderBot:
    def test_order_shows_empty_cart(self, webhook_secret, patched_send):
        _make_customer()
        client = Client()
        _post(client, _order_update())
        assert 'Empty' in patched_send[0]['text']

    def test_order_add_inserts_item(self, webhook_secret, patched_send):
        _make_customer()
        p = _make_product('Pizza', '50000')
        client = Client()
        _post(client, _order_update(text=f'/order add {p.id} 2'))
        assert 'Added Pizza x2' in patched_send[0]['text']
        assert CartItem.objects.count() == 1

    def test_order_add_with_unknown_id_warns(
        self, webhook_secret, patched_send,
    ):
        _make_customer()
        client = Client()
        _post(client, _order_update(text='/order add 99999 1'))
        assert 'No product 99999' in patched_send[0]['text']
        assert CartItem.objects.count() == 0

    def test_order_remove(self, webhook_secret, patched_send):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _order_update(text=f'/order remove {p.id}'))
        assert CartItem.objects.count() == 0

    def test_order_clear(self, webhook_secret, patched_send):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _order_update(text='/order clear'))
        assert CartItem.objects.count() == 0

    def test_order_checkout_creates_order(self, webhook_secret, patched_send):
        c = _make_customer()
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 3)
        client = Client()
        _post(client, _order_update(text='/order checkout'))
        from base.models import Order
        assert Order.objects.count() == 1
        order = Order.objects.first()
        assert order.items.first().quantity == 3
        assert 'Placed' in patched_send[0]['text']
        assert f'#{order.display_id}' in patched_send[0]['text']

    def test_order_checkout_without_login_warns(
        self, webhook_secret, patched_send,
    ):
        c = TelegramCustomer.objects.create(chat_id=555, first_name='Adrian')
        p = _make_product('Pizza', '50000')
        cart_service.add_item(c, p.id, 1)
        client = Client()
        _post(client, _order_update(text='/order checkout'))
        from base.models import Order
        assert Order.objects.count() == 0
        assert 'Need login' in patched_send[0]['text']

    def test_order_help_invoked_on_invalid_args(
        self, webhook_secret, patched_send,
    ):
        _make_customer()
        client = Client()
        _post(client, _order_update(text='/order add notanumber'))
        assert 'Help text' in patched_send[0]['text']
