"""Loyalty engine regression tests.

Three surfaces:
  - loyalty_service.maybe_accrue / get_account / redeem
  - admin endpoints (settings GET/PUT, account GET, redeem POST)
  - /loyalty Telegram command

The order-service accrual hook is also exercised via mark_as_paid +
update_order_status to confirm both code paths credit stamps and stay
idempotent.
"""
import json

import pytest
from django.test import Client

from base.repositories.session import SessionRepository

from notifications.models import (
    LoyaltyAccount, LoyaltySettings, NotificationTemplate,
    OrderLoyaltyCredit, TelegramCustomer,
)
from notifications.services import loyalty_service


pytestmark = pytest.mark.django_db


# ---- service unit tests --------------------------------------------------

class TestLoyaltyServiceAccrual:
    def _completed_paid_order(self, user, phone='998900000001'):
        from base.models import Order
        return Order.objects.create(
            user=user, phone_number=phone, order_type='PICKUP',
            status='COMPLETED', is_paid=True,
            total_amount='25000', subtotal='25000',
            display_id=1,
        )

    def test_accrue_creates_account_and_credits(self, regular_user):
        order = self._completed_paid_order(regular_user)
        account = loyalty_service.maybe_accrue(order)
        assert account is not None
        assert account.stamps_balance == 1
        assert account.stamps_earned_total == 1
        assert OrderLoyaltyCredit.objects.filter(order_id=order.id).exists()

    def test_accrue_is_idempotent(self, regular_user):
        order = self._completed_paid_order(regular_user)
        loyalty_service.maybe_accrue(order)
        loyalty_service.maybe_accrue(order)
        loyalty_service.maybe_accrue(order)
        account = LoyaltyAccount.objects.get(phone_number='998900000001')
        assert account.stamps_balance == 1
        assert OrderLoyaltyCredit.objects.filter(order_id=order.id).count() == 1

    def test_accrue_skips_unpaid_order(self, regular_user):
        from base.models import Order
        order = Order.objects.create(
            user=regular_user, phone_number='998900000001',
            order_type='PICKUP', status='COMPLETED', is_paid=False,
            total_amount='25000', subtotal='25000', display_id=1,
        )
        assert loyalty_service.maybe_accrue(order) is None
        assert not LoyaltyAccount.objects.filter(phone_number='998900000001').exists()

    def test_accrue_skips_non_completed(self, regular_user):
        from base.models import Order
        order = Order.objects.create(
            user=regular_user, phone_number='998900000001',
            order_type='PICKUP', status='READY', is_paid=True,
            total_amount='25000', subtotal='25000', display_id=1,
        )
        assert loyalty_service.maybe_accrue(order) is None

    def test_accrue_skips_when_no_phone(self, regular_user):
        from base.models import Order
        order = Order.objects.create(
            user=regular_user, phone_number=None,
            order_type='PICKUP', status='COMPLETED', is_paid=True,
            total_amount='25000', subtotal='25000', display_id=1,
        )
        assert loyalty_service.maybe_accrue(order) is None

    def test_accrue_skips_when_disabled(self, regular_user):
        settings = LoyaltySettings.load()
        settings.is_enabled = False
        settings.save()
        order = self._completed_paid_order(regular_user)
        assert loyalty_service.maybe_accrue(order) is None

    def test_accrue_uses_settings_per_order(self, regular_user):
        settings = LoyaltySettings.load()
        settings.stamps_per_completed_order = 3
        settings.save()
        order = self._completed_paid_order(regular_user)
        account = loyalty_service.maybe_accrue(order)
        assert account.stamps_balance == 3

    def test_phone_normalization_drops_leading_plus(self, regular_user):
        order = self._completed_paid_order(regular_user, phone='+998900000001')
        loyalty_service.maybe_accrue(order)
        # Stored without '+', so a Telegram-saved customer matches.
        assert LoyaltyAccount.objects.filter(phone_number='998900000001').exists()


class TestLoyaltyServiceRedeem:
    def test_redeem_decrements_when_enough_stamps(self, db):
        LoyaltyAccount.objects.create(phone_number='998900000001', stamps_balance=12)
        account = loyalty_service.redeem('998900000001')
        assert account.stamps_balance == 2
        assert account.stamps_redeemed_total == 10

    def test_redeem_fails_when_not_enough_stamps(self, db):
        LoyaltyAccount.objects.create(phone_number='998900000001', stamps_balance=5)
        assert loyalty_service.redeem('998900000001') is None
        account = LoyaltyAccount.objects.get(phone_number='998900000001')
        assert account.stamps_balance == 5

    def test_redeem_unknown_phone_returns_none(self, db):
        assert loyalty_service.redeem('998900000099') is None


# ---- order-service hook --------------------------------------------------

class TestOrderHookAccrual:
    def test_mark_as_paid_then_complete_credits_stamps(
        self, order_factory, cashier_user, regular_user,
    ):
        from base.models import Order
        from admins.services.order_service import AdminOrderService as OrderService
        order = order_factory(user=regular_user, cashier=cashier_user)
        order.phone_number = '998900000001'
        order.save()

        OrderService.mark_as_paid(order.id, payment_method='CASH')
        # Not yet COMPLETED, so no credit yet.
        assert not LoyaltyAccount.objects.filter(phone_number='998900000001').exists()

        OrderService.update_order_status(order.id, 'COMPLETED')
        account = LoyaltyAccount.objects.get(phone_number='998900000001')
        assert account.stamps_balance == 1

    def test_complete_then_pay_credits_stamps(
        self, order_factory, cashier_user, regular_user,
    ):
        from admins.services.order_service import AdminOrderService as OrderService
        order = order_factory(user=regular_user, cashier=cashier_user)
        order.phone_number = '998900000001'
        order.save()

        OrderService.update_order_status(order.id, 'COMPLETED')
        # COMPLETED but not paid → no credit.
        assert not LoyaltyAccount.objects.filter(phone_number='998900000001').exists()

        OrderService.mark_as_paid(order.id, payment_method='CASH')
        account = LoyaltyAccount.objects.get(phone_number='998900000001')
        assert account.stamps_balance == 1


# ---- admin endpoints ------------------------------------------------------

@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    import secrets
    from django.utils import timezone
    from datetime import timedelta
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


@pytest.fixture
def cashier_session(cashier_user):
    from base.models import Session
    import secrets
    from django.utils import timezone
    from datetime import timedelta
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=cashier_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


def _auth(session_key):
    return {'HTTP_AUTHORIZATION': f'Bearer {session_key}'}


class TestLoyaltyAdminAPI:
    def test_settings_get_returns_defaults(self, admin_session):
        client = Client()
        resp = client.get('/api/admins/notifications/loyalty/settings/', **_auth(admin_session))
        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['is_enabled'] is True
        assert data['stamps_per_completed_order'] == 1
        assert data['stamps_per_reward'] == 10

    def test_settings_put_updates_thresholds(self, admin_session):
        client = Client()
        resp = client.put(
            '/api/admins/notifications/loyalty/settings/',
            data=json.dumps({
                'stamps_per_completed_order': 2,
                'stamps_per_reward': 8,
                'reward_description': 'Free coffee',
            }),
            content_type='application/json',
            **_auth(admin_session),
        )
        assert resp.status_code == 200
        s = LoyaltySettings.load()
        assert s.stamps_per_completed_order == 2
        assert s.stamps_per_reward == 8
        assert s.reward_description == 'Free coffee'

    def test_settings_put_rejects_zero_threshold(self, admin_session):
        client = Client()
        resp = client.put(
            '/api/admins/notifications/loyalty/settings/',
            data=json.dumps({'stamps_per_reward': 0}),
            content_type='application/json',
            **_auth(admin_session),
        )
        assert resp.status_code == 422

    def test_settings_requires_admin(self, cashier_session):
        client = Client()
        resp = client.get('/api/admins/notifications/loyalty/settings/', **_auth(cashier_session))
        assert resp.status_code == 403

    def test_account_lookup_returns_404_when_missing(self, admin_session):
        client = Client()
        resp = client.get('/api/admins/notifications/loyalty/accounts/998900000001/',
                          **_auth(admin_session))
        assert resp.status_code == 404

    def test_account_lookup_normalizes_plus_prefix(self, admin_session):
        LoyaltyAccount.objects.create(phone_number='998900000001', stamps_balance=4)
        client = Client()
        resp = client.get('/api/admins/notifications/loyalty/accounts/+998900000001/',
                          **_auth(admin_session))
        assert resp.status_code == 200
        assert resp.json()['data']['stamps_balance'] == 4

    def test_cashier_can_redeem(self, cashier_session):
        LoyaltyAccount.objects.create(phone_number='998900000001', stamps_balance=12)
        client = Client()
        resp = client.post('/api/admins/notifications/loyalty/accounts/998900000001/redeem/',
                           **_auth(cashier_session))
        assert resp.status_code == 200
        assert resp.json()['data']['stamps_balance'] == 2

    def test_redeem_returns_409_when_not_enough(self, cashier_session):
        LoyaltyAccount.objects.create(phone_number='998900000001', stamps_balance=3)
        client = Client()
        resp = client.post('/api/admins/notifications/loyalty/accounts/998900000001/redeem/',
                           **_auth(cashier_session))
        assert resp.status_code == 409


# ---- /loyalty Telegram command -------------------------------------------

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
        sent.append({'chat_id': chat_id, 'text': text, 'reply_markup': reply_markup})
        return True, None

    from base.notifications.telegram import TelegramAPI
    monkeypatch.setattr(TelegramAPI, 'send_to_chat', staticmethod(fake_send))
    return sent


@pytest.fixture
def loyalty_balance_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.loyalty_balance',
        name='Loyalty balance',
        template_text=(
            'Stamps: {stamps}/{threshold}, remaining: {remaining}, '
            'rewards: {available_rewards}, reward: {reward}'
        ),
    )


@pytest.fixture
def loyalty_unauth_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.loyalty_unauthenticated',
        name='Loyalty unauth',
        template_text='Login first.',
    )


@pytest.fixture
def loyalty_disabled_template(db):
    return NotificationTemplate.objects.create(
        notification_type='telegram.loyalty_disabled',
        name='Loyalty disabled',
        template_text='Loyalty is off.',
    )


def _loyalty_update(chat_id=555):
    return {
        'update_id': 1,
        'message': {
            'message_id': 1,
            'chat': {'id': chat_id, 'type': 'private'},
            'from': {'id': chat_id, 'first_name': 'Adrian', 'is_bot': False},
            'text': '/loyalty',
        },
    }


def _post(client, body):
    return client.post(
        WEBHOOK_URL, data=json.dumps(body), content_type='application/json',
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=SECRET,
    )


class TestLoyaltyBotCommand:
    def test_loyalty_without_phone_prompts_login(
        self, webhook_secret, patched_send, loyalty_unauth_template,
    ):
        TelegramCustomer.objects.create(chat_id=555, first_name='Adrian')
        client = Client()
        _post(client, _loyalty_update())
        assert 'Login first' in patched_send[0]['text']

    def test_loyalty_shows_balance_for_linked_customer(
        self, webhook_secret, patched_send, loyalty_balance_template,
    ):
        TelegramCustomer.objects.create(
            chat_id=555, first_name='Adrian', phone_number='998900000001',
        )
        LoyaltyAccount.objects.create(phone_number='998900000001', stamps_balance=7)
        client = Client()
        _post(client, _loyalty_update())
        text = patched_send[0]['text']
        assert 'Stamps: 7/10' in text
        assert 'remaining: 3' in text
        assert 'rewards: 0' in text

    def test_loyalty_zero_balance_for_new_customer(
        self, webhook_secret, patched_send, loyalty_balance_template,
    ):
        TelegramCustomer.objects.create(
            chat_id=555, first_name='Adrian', phone_number='998900000077',
        )
        client = Client()
        _post(client, _loyalty_update())
        assert 'Stamps: 0/10' in patched_send[0]['text']

    def test_loyalty_disabled_message(
        self, webhook_secret, patched_send, loyalty_disabled_template,
    ):
        TelegramCustomer.objects.create(
            chat_id=555, first_name='Adrian', phone_number='998900000001',
        )
        s = LoyaltySettings.load()
        s.is_enabled = False
        s.save()
        client = Client()
        _post(client, _loyalty_update())
        assert 'off' in patched_send[0]['text']
