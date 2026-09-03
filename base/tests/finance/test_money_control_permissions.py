import json
import secrets
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from base.models import Session, TreasuryAccount, TreasuryTransaction, User
from base.repositories import SessionRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from stock.models import Supplier, SupplierPayment, SupplierTransaction


pytestmark = pytest.mark.django_db


def _user(role, email):
    return User.objects.create(
        first_name=role.title(), last_name='Tester', email=email, password='!',
        role=role, status=User.UserStatus.ACTIVE,
        permissions=DEFAULT_ROLE_PERMISSIONS[role], branch_id='branch1',
    )


def _client(user):
    token = secrets.token_hex(32)
    user_agent = f'money-permission-{user.id}'
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT=user_agent)
    client.cookies['session_key'] = token
    return client


@pytest.mark.parametrize('role', [User.RoleChoices.ADMIN, User.RoleChoices.MANAGER])
def test_admin_and_manager_can_open_inventory_control(role):
    client = _client(_user(role, f'{role.lower()}-owner@test.local'))

    inventory = client.get('/api/admins/stock/inventory-control/')

    assert inventory.status_code == 200, inventory.content
    assert inventory.json()['success'] is True


def test_warehouse_financial_forbidden_requests_do_not_mutate_state():
    warehouse = _user(User.RoleChoices.WAREHOUSE, 'warehouse-money@test.local')
    client = _client(warehouse)
    supplier = Supplier.objects.create(
        name='Warehouse-visible supplier', branch_id='branch1',
    )
    before = {
        'supplier_balance': supplier.current_balance,
        'treasury_accounts': TreasuryAccount.objects.count(),
        'treasury_rows': TreasuryTransaction.objects.count(),
        'supplier_rows': SupplierTransaction.objects.count(),
        'payments': SupplierPayment.objects.count(),
    }

    assert client.get('/api/admins/stock/suppliers/').status_code == 200
    assert client.get(
        f'/api/admins/stock/suppliers/{supplier.id}/ledger/'
    ).status_code == 200
    forbidden = [
        client.get('/api/admins/stock/inventory-control/'),
        client.post(
            f'/api/admins/stock/suppliers/{supplier.id}/payments/',
            json.dumps({
                'amount_uzs': 1,
                'source_account': 'BANK',
                'allocation_mode': 'AUTO_OLDEST_DUE',
            }),
            content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='warehouse-forbidden-payment',
        ),
    ]
    assert all(response.status_code == 403 for response in forbidden)
    assert all(
        response.json()['code'] == 'PERMISSION_DENIED' for response in forbidden
    )

    supplier.refresh_from_db()
    assert {
        'supplier_balance': supplier.current_balance,
        'treasury_accounts': TreasuryAccount.objects.count(),
        'treasury_rows': TreasuryTransaction.objects.count(),
        'supplier_rows': SupplierTransaction.objects.count(),
        'payments': SupplierPayment.objects.count(),
    } == before
