"""Regression tests for the inbound Telegram bot foothold.

Webhook auth, dispatcher resolution, /start handler, customer upsert.
TelegramAPI.send_to_chat is monkeypatched so no real network calls happen.
"""
import itertools
import json

import pytest
from django.test import Client

from notifications.models import NotificationTemplate, TelegramCustomer


pytestmark = pytest.mark.django_db


WEBHOOK_URL = '/api/telegram/webhook/'
SECRET = 'test-webhook-secret-token'


@pytest.fixture
def webhook_secret(settings):
    """Set TELEGRAM_WEBHOOK_SECRET on the live settings for the test.
    pytest-django's `settings` fixture rolls back automatically."""
    settings.TELEGRAM_WEBHOOK_SECRET = SECRET
    return SECRET


@pytest.fixture
def patched_send(monkeypatch):
    """Replace TelegramAPI.send_to_chat with a recorder so tests can assert
    on what the bot tried to send without hitting api.telegram.org."""
    sent = []

    def fake_send(chat_id, text, reply_markup=None):
        sent.append({
            'chat_id': chat_id, 'text': text, 'reply_markup': reply_markup,
        })
        return True, None

    from base.notifications.telegram import TelegramAPI
    monkeypatch.setattr(TelegramAPI, 'send_to_chat', staticmethod(fake_send))
    return sent


@pytest.fixture
def start_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.start',
        name='Bot welcome',
        template_text='Welcome {first_name} to {brand}',
    )


@pytest.fixture
def unknown_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.unknown_command',
        name='Bot unknown',
        template_text="Sorry {first_name}, didn't get '{input}'",
    )


def _post(client, body, secret=SECRET):
    headers = {'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': secret} if secret else {}
    return client.post(
        WEBHOOK_URL,
        data=json.dumps(body),
        content_type='application/json',
        **headers,
    )


_update_id_seq = itertools.count(1)


def _start_update(chat_id=12345, first_name='Adrian', update_id=None):
    # Each real Telegram update carries a distinct update_id; auto-assign a
    # unique one per call so handle_update's redelivery dedup (which keys on
    # update_id) doesn't drop a second genuine update in the same test.
    if update_id is None:
        update_id = next(_update_id_seq)
    return {
        'update_id': update_id,
        'message': {
            'message_id': 1,
            'chat': {'id': chat_id, 'type': 'private'},
            'from': {
                'id': chat_id,
                'first_name': first_name,
                'language_code': 'uz',
                'is_bot': False,
            },
            'text': '/start',
        },
    }


class TestWebhookAuth:
    def test_no_secret_configured_returns_503(self, settings):
        # Default: TELEGRAM_WEBHOOK_SECRET = ''. Webhook refuses to serve.
        settings.TELEGRAM_WEBHOOK_SECRET = ''
        client = Client()
        resp = _post(client, _start_update())
        assert resp.status_code == 503

    def test_wrong_secret_returns_401(self, webhook_secret):
        client = Client()
        resp = _post(client, _start_update(), secret='not-the-secret')
        assert resp.status_code == 401

    def test_missing_secret_header_returns_401(self, webhook_secret):
        client = Client()
        resp = _post(client, _start_update(), secret=None)
        assert resp.status_code == 401

    def test_correct_secret_returns_200(self, webhook_secret, patched_send, start_template):
        client = Client()
        resp = _post(client, _start_update())
        assert resp.status_code == 200

    def test_invalid_json_still_returns_200(self, webhook_secret):
        # Telegram-friendly: never make Telegram retry forever on a junk body.
        client = Client()
        resp = client.post(
            WEBHOOK_URL,
            data='not json',
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET,
        )
        assert resp.status_code == 200


class TestStartCommand:
    def test_start_creates_customer(self, webhook_secret, patched_send, start_template):
        client = Client()
        _post(client, _start_update(chat_id=999, first_name='Adrian'))
        customer = TelegramCustomer.objects.get(chat_id=999)
        assert customer.first_name == 'Adrian'
        assert customer.language_code == 'uz'

    def test_start_renders_template_with_first_name(
        self, webhook_secret, patched_send, start_template,
    ):
        client = Client()
        _post(client, _start_update(chat_id=999, first_name='Adrian'))
        assert len(patched_send) == 1
        sent = patched_send[0]
        assert sent['chat_id'] == 999
        assert 'Welcome Adrian' in sent['text']
        assert 'Alpha POS' in sent['text']  # default brand

    def test_start_repeated_updates_existing_customer(
        self, webhook_secret, patched_send, start_template,
    ):
        client = Client()
        _post(client, _start_update(chat_id=999, first_name='Adrian'))
        # Same chat_id, different first_name (user changed Telegram profile)
        update = _start_update(chat_id=999, first_name='Adrian-Updated')
        _post(client, update)

        # Still only one row, but profile fields refreshed.
        assert TelegramCustomer.objects.filter(chat_id=999).count() == 1
        assert TelegramCustomer.objects.get(chat_id=999).first_name == 'Adrian-Updated'

    def test_blocked_customer_gets_no_reply(
        self, webhook_secret, patched_send, start_template,
    ):
        TelegramCustomer.objects.create(chat_id=999, first_name='X', is_blocked=True)
        client = Client()
        _post(client, _start_update(chat_id=999, first_name='X'))
        assert len(patched_send) == 0


class TestCommandRouting:
    def test_unknown_command_falls_through_to_unknown_template(
        self, webhook_secret, patched_send, unknown_template,
    ):
        client = Client()
        update = _start_update()
        update['message']['text'] = '/somethingweird'
        _post(client, update)
        assert len(patched_send) == 1
        assert "didn't get" in patched_send[0]['text']
        assert '/somethingweird' in patched_send[0]['text']

    def test_bot_suffixed_command_resolves(
        self, webhook_secret, patched_send, start_template,
    ):
        # Telegram appends @bot_username when commands run in groups —
        # /start@my_alpha_bot must route to the same /start handler.
        client = Client()
        update = _start_update()
        update['message']['text'] = '/start@my_alpha_bot'
        _post(client, update)
        assert len(patched_send) == 1
        assert 'Welcome' in patched_send[0]['text']

    def test_non_message_update_silently_ignored(self, webhook_secret, patched_send):
        # callback_query / inline_query / edited_message etc. — we don't
        # handle these yet; they shouldn't crash the webhook.
        client = Client()
        resp = _post(client, {
            'update_id': 2,
            'callback_query': {'id': 'cbq', 'data': 'x'},
        })
        assert resp.status_code == 200
        assert len(patched_send) == 0

    def test_plain_text_treated_as_unknown(
        self, webhook_secret, patched_send, unknown_template,
    ):
        client = Client()
        update = _start_update()
        update['message']['text'] = 'hello there'
        _post(client, update)
        assert len(patched_send) == 1
        assert "didn't get" in patched_send[0]['text']


class TestSendErrorHandling:
    def test_403_marks_customer_blocked(self, webhook_secret, monkeypatch, start_template):
        from base.notifications.telegram import TelegramAPI
        monkeypatch.setattr(
            TelegramAPI, 'send_to_chat',
            staticmethod(lambda chat_id, text, reply_markup=None: (False, 'API 403: forbidden')),
        )
        client = Client()
        _post(client, _start_update(chat_id=999))
        customer = TelegramCustomer.objects.get(chat_id=999)
        assert customer.is_blocked is True


# ---- /menu ---------------------------------------------------------------

@pytest.fixture
def menu_root_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.menu_root',
        name='Menu root',
        template_text='Menu for {first_name}:\n{categories_list}',
    )


@pytest.fixture
def menu_category_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.menu_category',
        name='Menu category',
        template_text='{category_name}\n{products_list}',
    )


@pytest.fixture
def menu_empty_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.menu_empty',
        name='Menu empty',
        template_text='Menu is empty, {first_name}.',
    )


@pytest.fixture
def menu_not_found_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.menu_not_found',
        name='Menu not found',
        template_text="No category '{slug}'.",
    )


def _menu_update(chat_id=12345, first_name='Adrian', text='/menu'):
    update = _start_update(chat_id=chat_id, first_name=first_name)
    update['message']['text'] = text
    return update


def _make_category(name, slug, parent=None, status='ACTIVE'):
    from base.models import Category
    return Category.objects.create(
        name=name, slug=slug, parent=parent, status=status,
    )


def _make_product(name, price, category):
    from base.models import Product
    return Product.objects.create(name=name, price=price, category=category)


class TestMenuRoot:
    def test_menu_lists_top_level_categories_with_counts(
        self, webhook_secret, patched_send, menu_root_template,
    ):
        pizza = _make_category('Pizza', 'pizza')
        _make_category('Salads', 'salads')
        _make_product('Margherita', '50000', pizza)
        _make_product('Pepperoni', '60000', pizza)

        client = Client()
        _post(client, _menu_update(text='/menu'))

        assert len(patched_send) == 1
        sent = patched_send[0]['text']
        assert 'Pizza (2)' in sent
        assert '/menu pizza' in sent
        assert 'Salads (0)' in sent
        assert '/menu salads' in sent

    def test_menu_skips_deleted_and_inactive_categories(
        self, webhook_secret, patched_send, menu_root_template,
    ):
        _make_category('Visible', 'visible')
        inactive = _make_category('Inactive', 'inactive', status='INACTIVE')
        deleted = _make_category('Deleted', 'deleted')
        deleted.is_deleted = True
        deleted.save()
        assert inactive  # silence unused

        client = Client()
        _post(client, _menu_update(text='/menu'))

        sent = patched_send[0]['text']
        assert 'Visible' in sent
        assert 'Inactive' not in sent
        assert 'Deleted' not in sent

    def test_menu_skips_subcategories_at_root(
        self, webhook_secret, patched_send, menu_root_template,
    ):
        parent = _make_category('Drinks', 'drinks')
        _make_category('Hot drinks', 'hot-drinks', parent=parent)
        client = Client()
        _post(client, _menu_update(text='/menu'))
        sent = patched_send[0]['text']
        assert 'Drinks' in sent
        # Subcategory only appears when drilling into the parent.
        assert 'Hot drinks' not in sent

    def test_menu_empty_falls_back(
        self, webhook_secret, patched_send, menu_empty_template,
    ):
        client = Client()
        _post(client, _menu_update(text='/menu'))
        assert len(patched_send) == 1
        assert 'Menu is empty' in patched_send[0]['text']


class TestMenuCategory:
    def test_menu_slug_lists_products_with_prices(
        self, webhook_secret, patched_send, menu_category_template,
    ):
        pizza = _make_category('Pizza', 'pizza')
        _make_product('Margherita', '50000', pizza)
        _make_product('Pepperoni', '60000', pizza)

        client = Client()
        _post(client, _menu_update(text='/menu pizza'))

        sent = patched_send[0]['text']
        assert 'Pizza' in sent
        assert 'Margherita' in sent
        assert '50,000' in sent
        assert 'Pepperoni' in sent
        assert '60,000' in sent

    def test_menu_slug_includes_subcategories(
        self, webhook_secret, patched_send, menu_category_template,
    ):
        drinks = _make_category('Drinks', 'drinks')
        _make_category('Hot', 'drinks-hot', parent=drinks)
        _make_category('Cold', 'drinks-cold', parent=drinks)

        client = Client()
        _post(client, _menu_update(text='/menu drinks'))

        sent = patched_send[0]['text']
        assert 'Drinks' in sent
        assert '/menu drinks-hot' in sent
        assert '/menu drinks-cold' in sent

    def test_menu_slug_unknown_falls_back(
        self, webhook_secret, patched_send, menu_not_found_template,
    ):
        client = Client()
        _post(client, _menu_update(text='/menu nope'))
        sent = patched_send[0]['text']
        assert "No category 'nope'" in sent

    def test_menu_slug_skips_deleted_products(
        self, webhook_secret, patched_send, menu_category_template,
    ):
        pizza = _make_category('Pizza', 'pizza')
        _make_product('Margherita', '50000', pizza)
        deleted = _make_product('Hidden', '99999', pizza)
        deleted.is_deleted = True
        deleted.save()

        client = Client()
        _post(client, _menu_update(text='/menu pizza'))
        sent = patched_send[0]['text']
        assert 'Margherita' in sent
        assert 'Hidden' not in sent


# ---- /login + contact share ---------------------------------------------

@pytest.fixture
def login_prompt_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.login_prompt',
        name='Login prompt',
        template_text='Hi {first_name}, share your phone',
    )


@pytest.fixture
def login_success_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.login_success',
        name='Login success',
        template_text='Saved {phone} for {first_name}',
    )


@pytest.fixture
def login_other_contact_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.login_other_contact',
        name='Login other contact',
        template_text='Share your OWN phone, {first_name}',
    )


def _contact_update(chat_id=12345, sender_id=12345, contact_user_id=12345,
                    phone='998901234567', first_name='Adrian'):
    return {
        'update_id': 99,
        'message': {
            'message_id': 5,
            'chat': {'id': chat_id, 'type': 'private'},
            'from': {
                'id': sender_id,
                'first_name': first_name,
                'is_bot': False,
            },
            'contact': {
                'phone_number': phone,
                'first_name': first_name,
                'user_id': contact_user_id,
            },
        },
    }


class TestLoginCommand:
    def test_login_sends_contact_keyboard(
        self, webhook_secret, patched_send, login_prompt_template,
    ):
        client = Client()
        update = _start_update()
        update['message']['text'] = '/login'
        _post(client, update)

        assert len(patched_send) == 1
        sent = patched_send[0]
        assert 'share your phone' in sent['text']
        keyboard = sent['reply_markup']
        assert keyboard is not None
        assert keyboard['keyboard'][0][0]['request_contact'] is True


class TestContactShare:
    def test_contact_saves_phone_and_removes_keyboard(
        self, webhook_secret, patched_send, login_success_template,
    ):
        client = Client()
        _post(client, _contact_update(chat_id=555, sender_id=555,
                                      contact_user_id=555, phone='998900000001'))

        customer = TelegramCustomer.objects.get(chat_id=555)
        assert customer.phone_number == '998900000001'

        assert len(patched_send) == 1
        sent = patched_send[0]
        assert '998900000001' in sent['text']
        assert sent['reply_markup'] == {'remove_keyboard': True}

    def test_contact_from_other_user_is_refused(
        self, webhook_secret, patched_send, login_other_contact_template,
    ):
        client = Client()
        # Sender id 555, but contact card belongs to user_id 999 — someone
        # forwarded another person's contact. Reject + don't save phone.
        _post(client, _contact_update(chat_id=555, sender_id=555,
                                      contact_user_id=999, phone='998900000999'))

        customer = TelegramCustomer.objects.get(chat_id=555)
        assert customer.phone_number == ''

        assert len(patched_send) == 1
        sent = patched_send[0]
        assert 'OWN phone' in sent['text']
        assert sent['reply_markup'] == {'remove_keyboard': True}

    def test_contact_without_user_id_still_saves(
        self, webhook_secret, patched_send, login_success_template,
    ):
        # Some Telegram clients omit user_id on the contact payload (e.g.,
        # a contact picked from the address book that isn't on Telegram).
        # We can't verify ownership so we trust the sender — restricting
        # this to the request_contact button is enforced UI-side anyway.
        client = Client()
        update = _contact_update(chat_id=555, sender_id=555,
                                 contact_user_id=None, phone='998900000002')
        update['message']['contact'].pop('user_id')
        _post(client, update)

        customer = TelegramCustomer.objects.get(chat_id=555)
        assert customer.phone_number == '998900000002'

    def test_contact_without_phone_is_silent(
        self, webhook_secret, patched_send, login_success_template,
    ):
        client = Client()
        update = _contact_update(chat_id=555, sender_id=555,
                                 contact_user_id=555, phone='')
        _post(client, update)
        # No phone → don't save, don't reply. (Defensive: shouldn't happen.)
        customer = TelegramCustomer.objects.get(chat_id=555)
        assert customer.phone_number == ''
        assert len(patched_send) == 0


# ---- /status -------------------------------------------------------------

@pytest.fixture
def status_unauth_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.status_unauthenticated',
        name='Status unauth',
        template_text='Login first, {first_name}.',
    )


@pytest.fixture
def status_list_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.status_list',
        name='Status list',
        template_text='Orders for {first_name}:\n{orders_list}',
    )


@pytest.fixture
def status_empty_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.status_empty',
        name='Status empty',
        template_text='No orders for {phone}.',
    )


def _logged_in_customer(chat_id=555, phone='998900000001', first_name='Adrian'):
    return TelegramCustomer.objects.create(
        chat_id=chat_id, phone_number=phone, first_name=first_name,
    )


def _status_update(chat_id=555, first_name='Adrian'):
    update = _start_update(chat_id=chat_id, first_name=first_name)
    update['message']['text'] = '/status'
    return update


def _make_order(user, phone, status='COMPLETED', is_paid=True, total='25000',
                display_id=None, created_at=None):
    from base.models import Order
    order = Order.objects.create(
        user=user,
        phone_number=phone,
        order_type='PICKUP',
        status=status,
        is_paid=is_paid,
        total_amount=total,
        subtotal=total,
        display_id=display_id or (Order.objects.count() + 1),
    )
    if created_at:
        # auto_now_add prevents direct assignment; bypass via update().
        from base.models import Order as O
        O.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    return order


class TestStatusCommand:
    def test_status_without_phone_prompts_login(
        self, webhook_secret, patched_send, status_unauth_template,
    ):
        TelegramCustomer.objects.create(chat_id=555, first_name='Adrian')
        client = Client()
        _post(client, _status_update())

        assert len(patched_send) == 1
        assert 'Login first' in patched_send[0]['text']

    def test_status_lists_recent_orders_for_phone(
        self, webhook_secret, patched_send, status_list_template,
        regular_user,
    ):
        _logged_in_customer(phone='998900000001')
        _make_order(regular_user, '998900000001',
                    status='COMPLETED', total='25000', display_id=42)
        _make_order(regular_user, '998900000001',
                    status='READY', total='30000', is_paid=False, display_id=43)

        client = Client()
        _post(client, _status_update())

        sent = patched_send[0]['text']
        assert '#42' in sent
        assert '#43' in sent
        assert 'Yakunlangan' in sent
        assert 'Tayyor' in sent
        assert '25,000' in sent
        assert '30,000' in sent

    def test_status_matches_plus_prefixed_phone(
        self, webhook_secret, patched_send, status_list_template,
        regular_user,
    ):
        # Customer's saved phone has no '+'; cashier typed it with '+'.
        _logged_in_customer(phone='998900000001')
        _make_order(regular_user, '+998900000001', display_id=99)

        client = Client()
        _post(client, _status_update())
        assert '#99' in patched_send[0]['text']

    def test_status_empty_when_no_orders(
        self, webhook_secret, patched_send, status_empty_template,
    ):
        _logged_in_customer(phone='998900000077')
        client = Client()
        _post(client, _status_update())
        assert '998900000077' in patched_send[0]['text']

    def test_status_skips_orders_outside_window(
        self, webhook_secret, patched_send, status_empty_template,
        regular_user,
    ):
        from datetime import timedelta
        from django.utils import timezone as tz
        _logged_in_customer(phone='998900000001')
        # Older than 30 days — must not appear.
        _make_order(regular_user, '998900000001', display_id=1,
                    created_at=tz.now() - timedelta(days=45))

        client = Client()
        _post(client, _status_update())
        # Falls through to the empty-template branch.
        assert '998900000001' in patched_send[0]['text']

    def test_status_ignores_other_customers_orders(
        self, webhook_secret, patched_send, status_empty_template,
        regular_user,
    ):
        _logged_in_customer(phone='998900000001')
        _make_order(regular_user, '998900000999', display_id=7)

        client = Client()
        _post(client, _status_update())
        # No match → empty template (mentions our phone, not display_id).
        assert '998900000001' in patched_send[0]['text']
        assert '#7' not in patched_send[0]['text']
