"""QR self-order public endpoint tests.

Covers token signing, menu fetch, order creation, validation/error paths,
and the admin token-mint endpoint.
"""
import json
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository
from notifications.services import qr_order_service


pytestmark = pytest.mark.django_db


# ---- fixtures -------------------------------------------------------------

@pytest.fixture
def place(db):
    from base.models import Place
    return Place.objects.create(name='Main Hall', place_type='HALL')


@pytest.fixture
def table(db, place):
    from base.models import Table
    return Table.objects.create(place=place, number='4', capacity=4)


@pytest.fixture
def product(db):
    from base.models import Category, Product
    cat, _ = Category.objects.get_or_create(name='C', slug='c')
    return Product.objects.create(
        name='Margherita', price=Decimal('50000'), category=cat,
    )


@pytest.fixture
def token(table):
    return qr_order_service.make_token(table)


# ---- service tests --------------------------------------------------------

class TestTokenRoundtrip:
    def test_signed_token_resolves_to_table(self, table, token):
        assert qr_order_service.resolve_token(token) == table

    def test_tampered_token_returns_none(self, table, token):
        bad = token[:-3] + 'aaa'
        assert qr_order_service.resolve_token(bad) is None

    def test_random_garbage_token_returns_none(self):
        assert qr_order_service.resolve_token('not-a-token') is None

    def test_inactive_table_token_returns_none(self, table, token):
        table.is_active = False
        table.save()
        assert qr_order_service.resolve_token(token) is None


class TestValidateItems:
    def test_empty_list_rejected(self):
        rows, err = qr_order_service.validate_items([])
        assert rows is None and err == 'items_empty'

    def test_non_list_rejected(self):
        rows, err = qr_order_service.validate_items('nope')
        assert err == 'items_empty'

    def test_too_many_items_rejected(self, product):
        items = [{'product_id': product.id, 'quantity': 1}] * 51
        rows, err = qr_order_service.validate_items(items)
        assert err == 'items_too_many'

    def test_invalid_dict_rejected(self):
        rows, err = qr_order_service.validate_items(['not a dict'])
        assert err == 'items_invalid'

    def test_zero_quantity_rejected(self, product):
        rows, err = qr_order_service.validate_items(
            [{'product_id': product.id, 'quantity': 0}],
        )
        assert err == 'quantity_out_of_range'

    def test_unknown_product_rejected(self):
        rows, err = qr_order_service.validate_items(
            [{'product_id': 99999, 'quantity': 1}],
        )
        assert err == 'product_not_found'

    def test_valid_items_pass(self, product):
        rows, err = qr_order_service.validate_items(
            [{'product_id': product.id, 'quantity': 3}],
        )
        assert err is None
        assert len(rows) == 1
        assert rows[0][1] == 3


class TestCreateOrderService:
    def test_creates_hall_order_at_table(self, table, product):
        rows, _ = qr_order_service.validate_items(
            [{'product_id': product.id, 'quantity': 2}],
        )
        order = qr_order_service.create_qr_order(table, rows)
        assert order.order_type == 'HALL'
        assert order.table_id == table.id
        assert order.status == 'OPEN'
        assert order.is_paid is False
        assert order.total_amount == Decimal('100000')
        assert order.items.count() == 1

    def test_qr_user_singleton_reused(self, table, product):
        rows, _ = qr_order_service.validate_items(
            [{'product_id': product.id, 'quantity': 1}],
        )
        o1 = qr_order_service.create_qr_order(table, rows)
        o2 = qr_order_service.create_qr_order(table, rows)
        assert o1.user_id == o2.user_id


# ---- public endpoint tests -----------------------------------------------

class TestMenuEndpoint:
    def test_menu_returns_categories_and_products(
        self, table, product, token,
    ):
        client = Client()
        resp = client.get(f'/api/qr/menu/{token}/')
        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['table']['number'] == '4'
        assert any(p['name'] == 'Margherita' for p in data['products'])

    def test_menu_invalid_token_returns_404(self, table, product):
        client = Client()
        resp = client.get('/api/qr/menu/garbage/')
        assert resp.status_code == 404

    def test_menu_skips_deleted_products(self, table, product, token):
        product.is_deleted = True
        product.save()
        client = Client()
        resp = client.get(f'/api/qr/menu/{token}/')
        data = resp.json()['data']
        assert not any(p['name'] == 'Margherita' for p in data['products'])


class TestOrderEndpoint:
    def test_order_happy_path(self, table, product, token):
        client = Client()
        resp = client.post(
            f'/api/qr/order/{token}/',
            data=json.dumps({'items': [
                {'product_id': product.id, 'quantity': 2},
            ]}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        d = resp.json()['data']
        assert d['items'] == 1
        from base.models import Order
        order = Order.objects.first()
        assert order.table_id == table.id
        assert order.total_amount == Decimal('100000')

    def test_order_invalid_token_returns_404(self, product):
        client = Client()
        resp = client.post(
            '/api/qr/order/garbage/',
            data=json.dumps({'items': [{'product_id': product.id, 'quantity': 1}]}),
            content_type='application/json',
        )
        assert resp.status_code == 404

    def test_order_bad_json_returns_400(self, table, token):
        client = Client()
        resp = client.post(
            f'/api/qr/order/{token}/',
            data='not json', content_type='application/json',
        )
        assert resp.status_code == 400

    def test_order_empty_items_returns_422(self, table, token):
        client = Client()
        resp = client.post(
            f'/api/qr/order/{token}/',
            data=json.dumps({'items': []}),
            content_type='application/json',
        )
        assert resp.status_code == 422

    def test_order_unknown_product_returns_422(self, table, token):
        client = Client()
        resp = client.post(
            f'/api/qr/order/{token}/',
            data=json.dumps({'items': [{'product_id': 99999, 'quantity': 1}]}),
            content_type='application/json',
        )
        assert resp.status_code == 422

    def test_order_records_customer_note(self, table, product, token):
        client = Client()
        client.post(
            f'/api/qr/order/{token}/',
            data=json.dumps({
                'items': [{'product_id': product.id, 'quantity': 1}],
                'note': 'No onions please',
            }),
            content_type='application/json',
        )
        from base.models import Order
        assert Order.objects.first().description == 'No onions please'


# ---- admin token-mint endpoint -------------------------------------------

@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


def _auth(session_key):
    return {'HTTP_AUTHORIZATION': f'Bearer {session_key}'}


class TestMintTokenEndpoint:
    def test_admin_can_mint_token(self, admin_session, table):
        client = Client()
        resp = client.get(
            f'/api/admins/notifications/qr/tables/{table.id}/token/',
            **_auth(admin_session),
        )
        assert resp.status_code == 200
        token = resp.json()['data']['token']
        # The minted token round-trips through resolve.
        assert qr_order_service.resolve_token(token) == table

    def test_admin_404_on_unknown_table(self, admin_session):
        client = Client()
        resp = client.get(
            '/api/admins/notifications/qr/tables/99999/token/',
            **_auth(admin_session),
        )
        assert resp.status_code == 404

    def test_unauthenticated_caller_blocked(self, table):
        client = Client()
        resp = client.get(f'/api/admins/notifications/qr/tables/{table.id}/token/')
        assert resp.status_code == 401
