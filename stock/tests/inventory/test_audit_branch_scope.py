"""Exercise branch isolation through the authenticated HTTP routes."""
import json

import pytest

from stock.models import StockCount, StockLocation, StockTransfer
from stock.services.count_service import StockCountService
from stock.tests.catalog.test_api_contracts import _authenticated_client
from .test_audit_regressions import make_transfer

pytestmark = pytest.mark.django_db
PREFIX = '/api/admins/stock'


@pytest.fixture
def scoped_client(catalog):
    catalog.actor.branch_id = 'branch1'
    catalog.actor.save(update_fields=['branch_id'])
    return _authenticated_client(catalog.actor)


def foreign_location():
    return StockLocation.objects.create(name='Other branch', branch_id='branch2')


def test_transfer_list_hides_peer_documents(catalog, scoped_client):
    own = make_transfer(catalog)
    foreign = foreign_location()
    StockTransfer.objects.create(transfer_number='OTHER', from_location=foreign,
                                 to_location=foreign, branch_id='branch2')
    response = scoped_client.get(f'{PREFIX}/transfers/')
    assert response.status_code == 200, response.content
    assert [row['id'] for row in response.json()['data']['transfers']] == [own.pk]


@pytest.mark.parametrize('action', ['approve', 'ship', 'receive', 'cancel'])
def test_transfer_actions_reject_foreign_branch(catalog, scoped_client, action):
    foreign = foreign_location()
    transfer = StockTransfer.objects.create(
        transfer_number='OTHER', from_location=foreign, to_location=foreign,
        status='DRAFT', branch_id='branch2')
    response = scoped_client.post(f'{PREFIX}/transfers/{transfer.pk}/{action}/',
                                  data='{}', content_type='application/json')
    assert response.status_code == 403, response.content
    transfer.refresh_from_db()
    assert transfer.status == 'DRAFT'


def test_transfer_creation_cannot_spoof_branch(catalog, scoped_client):
    foreign = foreign_location()
    response = scoped_client.post(f'{PREFIX}/transfers/', data=json.dumps({
        'from_location_id': foreign.pk, 'to_location_id': catalog.dest.pk,
        'branch_id': 'branch2',
    }), content_type='application/json')
    assert response.status_code == 403, response.content
    assert not StockTransfer.objects.exists()


def test_own_transfer_detail_remains_accessible(catalog, scoped_client):
    own = make_transfer(catalog)
    response = scoped_client.get(f'{PREFIX}/transfers/{own.pk}/')
    assert response.status_code == 200, response.content


def test_count_list_and_actions_are_branch_scoped(catalog, scoped_client):
    result, status = StockCountService.create(catalog.source.pk, 'FULL', catalog.actor.pk)
    assert status == 201, result
    own_id = result['data']['id']
    other = StockCount.objects.create(
        count_number='OTHER', location=foreign_location(), count_type='FULL',
        counted_by=catalog.actor, branch_id='branch2')
    response = scoped_client.get(f'{PREFIX}/counts/')
    assert response.status_code == 200, response.content
    assert [row['id'] for row in response.json()['data']['counts']] == [own_id]
    for suffix in ('', 'start/', 'complete/', 'approve/'):
        url = f'{PREFIX}/counts/{other.pk}/{suffix}'
        response = (scoped_client.post(url, data='{}', content_type='application/json')
                    if suffix else scoped_client.get(url))
        assert response.status_code == 403, response.content
    other.refresh_from_db()
    assert other.status == 'DRAFT'


def test_count_creation_cannot_spoof_branch(catalog, scoped_client):
    response = scoped_client.post(f'{PREFIX}/counts/', data=json.dumps({
        'location_id': foreign_location().pk, 'count_type': 'FULL', 'branch_id': 'branch2',
    }), content_type='application/json')
    assert response.status_code == 403, response.content
    assert not StockCount.objects.exists()


@pytest.mark.parametrize('action', ['start', 'complete'])
def test_count_recorder_cannot_act_on_another_owners_count(catalog, cashier_user, action):
    cashier_user.role = 'WAREHOUSE'
    cashier_user.branch_id = 'branch1'
    cashier_user.permissions = ['stock.count.record']
    cashier_user.save(update_fields=['role', 'branch_id', 'permissions'])
    client = _authenticated_client(cashier_user)
    other = StockCount.objects.create(
        count_number='OTHER-OWNER', location=catalog.source, count_type='FULL',
        counted_by=catalog.actor, branch_id='branch1',
        status='DRAFT' if action == 'start' else 'IN_PROGRESS',
        auto_adjust=True)
    response = client.post(f'{PREFIX}/counts/{other.pk}/{action}/',
                           data='{}', content_type='application/json')
    assert response.status_code == 403, response.content
    other.refresh_from_db()
    assert other.status == ('DRAFT' if action == 'start' else 'IN_PROGRESS')
    # Prove this is ownership enforcement, not a blanket role denial.
    own = StockCount.objects.create(
        count_number='OWNED', location=catalog.dest, count_type='FULL',
        counted_by=cashier_user, branch_id='branch1',
        status='DRAFT' if action == 'start' else 'IN_PROGRESS')
    response = client.post(f'{PREFIX}/counts/{own.pk}/{action}/',
                           data='{}', content_type='application/json')
    assert response.status_code == 200, response.content
