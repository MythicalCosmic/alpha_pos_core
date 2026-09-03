import json
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.models import IdempotencyKey, Session, User
from base.repositories import SessionRepository
from stock.models import (
    StockItem, StockLevel, StockLocation, StockSettings, StockTransaction,
    StockUnit,
)


pytestmark = pytest.mark.django_db


def _client(user):
    token = secrets.token_hex(32)
    user_agent = f'secure-adjustment-{user.id}'
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


def _setup():
    actor = User.objects.create(
        first_name='Stock', last_name='Controller',
        email='stock-controller@test.local', password='!',
        role=User.RoleChoices.MANAGER, status=User.UserStatus.ACTIVE,
        permissions=['stock.adjustment.approve'], branch_id='branch-a',
    )
    unit = StockUnit.objects.create(
        name='Kilogram', short_name='kg', unit_type='WEIGHT',
        is_base_unit=True,
    )
    own_location = StockLocation.objects.create(
        name='Own warehouse', type='WAREHOUSE', branch_id='branch-a',
    )
    foreign_location = StockLocation.objects.create(
        name='Foreign warehouse', type='WAREHOUSE', branch_id='branch-b',
    )
    item = StockItem.objects.create(
        name='Tomatoes', sku='SECURE-TOMATOES', base_unit=unit,
        item_type='RAW', avg_cost_price='100', branch_id='branch-a',
    )
    level = StockLevel.objects.create(
        stock_item=item, location=own_location, quantity='10',
        branch_id='branch-a',
    )
    settings = StockSettings.load()
    settings.stock_enabled = True
    settings.allow_negative_stock = False
    settings.save(update_fields=[
        'stock_enabled', 'allow_negative_stock', 'updated_at',
    ])
    return actor, item, level, own_location, foreign_location


def test_adjustment_rejects_cross_branch_and_requires_idempotency():
    actor, item, level, own_location, foreign_location = _setup()
    client = _client(actor)
    payload = {
        'stock_item_id': item.id,
        'location_id': foreign_location.id,
        'quantity': '2.0000',
        'movement_type': 'WASTE',
        'reason': 'Damaged in storage',
    }

    missing_key = client.post(
        '/api/admins/stock/adjust/',
        json.dumps({**payload, 'location_id': own_location.id}),
        content_type='application/json',
    )
    assert missing_key.status_code == 422
    assert missing_key.json()['code'] == 'IDEMPOTENCY_KEY_REQUIRED'

    forbidden = client.post(
        '/api/admins/stock/adjust/', json.dumps(payload),
        content_type='application/json', HTTP_IDEMPOTENCY_KEY='cross-branch',
    )
    assert forbidden.status_code == 403
    assert forbidden.json()['code'] == 'STOCK_SCOPE_FORBIDDEN'
    level.refresh_from_db()
    assert level.quantity == Decimal('10')
    assert not StockTransaction.objects.exists()
    assert not IdempotencyKey.objects.exists()


def test_waste_uses_weighted_cost_is_idempotent_and_has_linked_reversal():
    actor, item, level, location, _foreign = _setup()
    client = _client(actor)
    payload = {
        'stock_item_id': item.id,
        'location_id': location.id,
        'quantity': '2.0000',
        'movement_type': 'SPOILAGE',
        'reason': 'Cold storage failure',
    }

    posted = client.post(
        '/api/admins/stock/adjust/', json.dumps(payload),
        content_type='application/json', HTTP_IDEMPOTENCY_KEY='spoilage-one',
    )
    replayed = client.post(
        '/api/admins/stock/adjust/', json.dumps(payload),
        content_type='application/json', HTTP_IDEMPOTENCY_KEY='spoilage-one',
    )

    assert posted.status_code == 200, posted.content
    assert replayed.status_code == 200
    assert replayed.json() == posted.json()
    data = posted.json()['data']
    assert data['total_cost_uzs'] == 200
    assert data['unit_cost'] == '100.0000'
    level.refresh_from_db()
    assert level.quantity == Decimal('8')
    assert StockTransaction.objects.count() == 1
    original = StockTransaction.objects.get(pk=data['transaction_id'])
    assert original.reference_type == 'StockWaste'
    assert original.actor_display_snapshot == 'Stock Controller'
    assert original.notes == 'Cold storage failure'

    reversed_response = client.post(
        f'/api/admins/stock/adjust/{original.id}/reverse/',
        json.dumps({'reason': 'Recovered sealed stock'}),
        content_type='application/json', HTTP_IDEMPOTENCY_KEY='spoilage-reverse',
    )
    assert reversed_response.status_code == 200, reversed_response.content
    level.refresh_from_db()
    assert level.quantity == Decimal('10')
    reversal = StockTransaction.objects.get(reversal_of=original)
    assert reversal.movement_type == StockTransaction.MovementType.ADJUSTMENT_PLUS
    assert reversal.total_cost == original.total_cost
    assert reversal.notes == 'Recovered sealed stock'
