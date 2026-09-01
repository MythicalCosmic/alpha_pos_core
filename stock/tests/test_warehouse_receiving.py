from decimal import Decimal
from datetime import timedelta
import secrets

import pytest
from django.test import Client
from django.utils import timezone

from base.models import Session, User
from base.repositories import SessionRepository, UserRepository
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from stock.models import (
    PurchaseOrder, PurchaseOrderItem, StockItem, StockLevel, StockLocation,
    StockSettings, StockTransaction, StockUnit, Supplier, SupplierTransaction,
)
from stock.services.purchase_service import PurchaseReceivingService


pytestmark = pytest.mark.django_db


def _client(user):
    token = secrets.token_hex(32)
    agent = 'warehouse-api-test'
    Session.objects.create(
        user_id=user, ip_address='127.0.0.1', user_agent=agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT=agent)
    client.cookies['session_key'] = token
    return client


def test_warehouse_role_is_not_a_pos_picker_role():
    user = User.objects.create(
        first_name='Warehouse', last_name='Operator', email='warehouse@test.local',
        password='hashed', role=User.RoleChoices.WAREHOUSE, status='ACTIVE',
        permissions=DEFAULT_ROLE_PERMISSIONS['WAREHOUSE'], branch_id='branch1',
    )
    assert not UserRepository.get_pos_staff().filter(id=user.id).exists()
    assert 'stock.receiving.complete' in user.permissions
    assert 'expense.request.create' not in user.permissions


def test_warehouse_read_access_and_money_routes_are_denied():
    user = User.objects.create(
        first_name='Warehouse', last_name='Reader', email='warehouse-reader@test.local',
        password='hashed', role=User.RoleChoices.WAREHOUSE, status='ACTIVE',
        permissions=DEFAULT_ROLE_PERMISSIONS['WAREHOUSE'], branch_id='branch1',
    )
    supplier = Supplier.objects.create(name='Read only supplier', current_balance=123_000,
                                       branch_id='branch1')
    client = _client(user)
    response = client.get('/api/admins/stock/suppliers/')
    assert response.status_code == 200, response.content
    row = response.json()['data']['suppliers'][0]
    assert row['current_balance_uzs'] == 123_000
    assert isinstance(row['current_balance_uzs'], int)
    assert client.post(
        f'/api/admins/stock/suppliers/{supplier.id}/pay/',
        data='{"amount": 1000}', content_type='application/json',
    ).status_code == 403
    assert client.post(
        '/api/admins/stock/adjust/', data='{}', content_type='application/json',
    ).status_code == 403
    assert client.get('/api/admins/stock/settings/').status_code == 403
    assert client.get('/api/admins/stock/inventory-control/').status_code == 403
    assert client.get('/api/admins/cashbox/recipients/search/').status_code == 403
    assert client.post(
        '/api/admins/hr/expenses/', data='{}', content_type='application/json',
    ).status_code == 403


def test_receiving_completion_posts_stock_and_supplier_debt_once(admin_user):
    admin_user.branch_id = 'branch1'
    admin_user.save(update_fields=['branch_id'])
    unit = StockUnit.objects.create(
        name='Piece', short_name='pc', unit_type='COUNT', is_base_unit=True,
        branch_id='branch1',
    )
    location = StockLocation.objects.create(
        name='Warehouse', type='WAREHOUSE', branch_id='branch1',
    )
    item = StockItem.objects.create(
        name='Box', base_unit=unit, item_type='FINISHED', branch_id='branch1',
    )
    supplier = Supplier.objects.create(name='Supplier', branch_id='branch1')
    po = PurchaseOrder.objects.create(
        order_number='PO-WH-1', supplier=supplier, delivery_location=location,
        status=PurchaseOrder.Status.CONFIRMED, order_date=timezone.localdate(),
        total=500_000, created_by=admin_user, branch_id='branch1',
    )
    po_item = PurchaseOrderItem.objects.create(
        purchase_order=po, stock_item=item, quantity_ordered=Decimal('5'),
        unit=unit, unit_price=100_000, total_price=500_000, branch_id='branch1',
    )
    settings = StockSettings.load()
    settings.stock_enabled = True
    settings.save(update_fields=['stock_enabled', 'updated_at'])
    created, status = PurchaseReceivingService.create(po.id, admin_user.id, location.id)
    assert status == 200, created
    receiving_id = created['data']['id']
    added, status = PurchaseReceivingService.add_item(
        receiving_id, po_item.id, Decimal('5'), unit_cost=100_000,
    )
    assert status == 200, added
    completed, status = PurchaseReceivingService.complete(receiving_id)
    assert status == 200, completed
    assert completed['data']['supplier_balance_before_uzs'] == 0
    assert completed['data']['supplier_balance_after_uzs'] == 500_000
    replayed, status = PurchaseReceivingService.complete(receiving_id)
    assert status == 200, replayed
    assert StockLevel.objects.get(stock_item=item, location=location).quantity == 5
    assert StockTransaction.objects.filter(reference_type='PurchaseReceiving').count() == 1
    assert SupplierTransaction.objects.filter(reference_type='PurchaseReceiving').count() == 1
    supplier.refresh_from_db()
    assert supplier.current_balance == 500_000
