import json
import secrets
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from base.models import Session, User
from base.repositories import SessionRepository
from stock.models import StockLocation


pytestmark = pytest.mark.django_db


def _client(user):
    token = secrets.token_hex(32)
    user_agent = f'location-scope-{user.id}'
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


def _manager(email, branch, permissions):
    return User.objects.create(
        first_name='Branch', last_name='Manager', email=email, password='!',
        role=User.RoleChoices.MANAGER, status=User.UserStatus.ACTIVE,
        permissions=permissions, branch_id=branch,
    )


def test_inventory_permission_lists_only_actor_branch_locations():
    branch_a_user = _manager(
        'location-reader@test.local', 'branch-a',
        ['stock.inventory_control.view'],
    )
    branch_b_user = _manager(
        'location-reader-b@test.local', 'branch-b',
        ['stock.inventory_control.view'],
    )
    own = StockLocation.objects.create(
        name='Branch A warehouse', type='WAREHOUSE', branch_id='branch-a',
    )
    foreign = StockLocation.objects.create(
        name='Branch B warehouse', type='WAREHOUSE', branch_id='branch-b',
    )
    client = _client(branch_a_user)

    response = client.get('/api/admins/stock/locations/')

    assert response.status_code == 200, response.content
    ids = {row['id'] for row in response.json()['data']['locations']}
    assert ids == {own.id}
    assert client.get(
        f'/api/admins/stock/locations/{foreign.id}/'
    ).status_code == 404

    other_response = _client(branch_b_user).get('/api/admins/stock/locations/')
    assert other_response.status_code == 200
    assert {
        row['id'] for row in other_response.json()['data']['locations']
    } == {foreign.id}


def test_location_mutations_require_manage_permission_and_stay_in_branch():
    reader = _manager(
        'location-no-write@test.local', 'branch-a',
        ['stock.inventory_control.view'],
    )
    manager = _manager(
        'location-writer@test.local', 'branch-a', ['stock.manage'],
    )
    foreign = StockLocation.objects.create(
        name='Foreign warehouse', type='WAREHOUSE', branch_id='branch-b',
    )
    payload = json.dumps({
        'name': 'New warehouse', 'type': 'WAREHOUSE', 'branch_id': 'branch-b',
    })

    denied = _client(reader).post(
        '/api/admins/stock/locations/', payload, content_type='application/json',
    )
    assert denied.status_code == 403
    assert not StockLocation.objects.filter(name='New warehouse').exists()

    writer = _client(manager)
    created = writer.post(
        '/api/admins/stock/locations/', payload, content_type='application/json',
    )
    assert created.status_code == 200, created.content
    assert StockLocation.objects.get(
        pk=created.json()['data']['id'],
    ).branch_id == 'branch-a'
    cross_branch = writer.put(
        f'/api/admins/stock/locations/{foreign.id}/',
        json.dumps({'name': 'Leaked rename'}),
        content_type='application/json',
    )
    assert cross_branch.status_code == 404
    foreign.refresh_from_db()
    assert foreign.name == 'Foreign warehouse'
