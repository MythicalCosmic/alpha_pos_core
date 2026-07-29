import json
import uuid

import pytest
from django.test import RequestFactory


pytestmark = pytest.mark.django_db


def _feed(settings, token, claimed_branch):
    from base.services.sync.views import changes

    request = RequestFactory().get(
        '/api/sync/changes',
        HTTP_AUTHORIZATION=f'Branch {token}',
        HTTP_X_BRANCH_ID=claimed_branch,
        HTTP_X_DEVICE_ID=f'{claimed_branch}-terminal',
    )
    response = changes(request)
    return response.status_code, json.loads(response.content)


def _uuids(body, model_name):
    return {
        row['uuid'] for row in body.get('data', {}).get(model_name, [])
    }


def test_changes_delivers_target_transactions_and_commands_not_peer_rows(settings):
    """The old ``exclude(requester)`` feed did precisely the opposite."""
    from base.models import Category, Inkassa, Order, User

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {
        'token-a': 'branch-a',
        'token-b': 'branch-b',
    }
    settings.ALLOWED_BRANCH_TOKENS = []

    cashier = User.objects.create(
        first_name='Shared', last_name='Cashier',
        email='shared-cashier@example.test', password='!',
        role=User.RoleChoices.CASHIER, status=User.UserStatus.ACTIVE,
        branch_id='cloud',
    )
    catalog = Category.objects.create(
        name='Shared menu', slug='shared-menu', branch_id='cloud',
    )
    order_a = Order.objects.create(
        user=cashier, cashier=cashier, branch_id='branch-a',
        total_amount='100000', subtotal='100000',
    )
    order_b = Order.objects.create(
        user=cashier, cashier=cashier, branch_id='branch-b',
        total_amount='200000', subtotal='200000',
    )
    command_a = Inkassa.objects.create(
        cashier=cashier, branch_id='branch-a', amount='25000',
        inkass_type=Inkassa.InkassType.CASH,
        balance_before='100000', balance_after='75000',
        register_command=True,
        notes=Inkassa.command_notes('target branch A'),
    )
    command_b = Inkassa.objects.create(
        cashier=cashier, branch_id='branch-b', amount='30000',
        inkass_type=Inkassa.InkassType.CASH,
        balance_before='200000', balance_after='170000',
        register_command=True,
        notes=Inkassa.command_notes('target branch B'),
    )

    status_a, feed_a = _feed(settings, 'token-a', 'branch-a')
    status_b, feed_b = _feed(settings, 'token-b', 'branch-b')

    assert status_a == status_b == 200
    assert str(order_a.uuid) in _uuids(feed_a, 'order')
    assert str(order_b.uuid) not in _uuids(feed_a, 'order')
    assert str(command_a.uuid) in _uuids(feed_a, 'inkassa')
    assert str(command_b.uuid) not in _uuids(feed_a, 'inkassa')

    assert str(order_b.uuid) in _uuids(feed_b, 'order')
    assert str(order_a.uuid) not in _uuids(feed_b, 'order')
    assert str(command_b.uuid) in _uuids(feed_b, 'inkassa')
    assert str(command_a.uuid) not in _uuids(feed_b, 'inkassa')

    # Cloud-owned same-company identity/catalog is intentionally global for
    # this release and therefore arrives on both branches.
    for body in (feed_a, feed_b):
        assert str(cashier.uuid) in _uuids(body, 'user')
        assert str(catalog.uuid) in _uuids(body, 'category')


def test_bound_token_cannot_claim_another_branch_feed(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {'token-a': 'branch-a'}
    settings.ALLOWED_BRANCH_TOKENS = []

    status, body = _feed(settings, 'token-a', 'branch-b')

    assert status == 403
    assert 'does not match token' in body['error']


def test_legacy_unbound_token_is_limited_to_allowed_branch_ids(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {}
    settings.ALLOWED_BRANCH_TOKENS = ['legacy-token']
    settings.ALLOWED_BRANCH_IDS = ['branch-a']

    denied_status, denied = _feed(settings, 'legacy-token', 'branch-b')
    allowed_status, allowed = _feed(settings, 'legacy-token', 'branch-a')

    assert denied_status == 403
    assert denied['error'] == 'X-Branch-ID is not in ALLOWED_BRANCH_IDS'
    assert allowed_status == 200
    assert allowed['success'] is True


def test_legacy_unbound_token_fails_closed_in_production_without_allowlist(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.DEBUG = False
    settings.BRANCH_TOKEN_MAP = {}
    settings.ALLOWED_BRANCH_TOKENS = ['legacy-token']
    settings.ALLOWED_BRANCH_IDS = []

    status, body = _feed(settings, 'legacy-token', 'branch-a')

    assert status == 403
    assert 'Unbound branch tokens are not permitted in production' in body['error']


def test_target_cash_command_applies_once_and_persists_cumulative_ack(settings):
    from base.models import CashRegister, Inkassa

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    register = CashRegister.objects.create(
        branch_id='branch-a', current_balance='100000',
    )
    command_uuid = uuid.uuid4()
    payload = {
        'uuid': str(command_uuid),
        'sync_version': 3,
        'is_deleted': False,
        'branch_id': 'branch-a',
        'cashier_uuid': None,
        'amount': '25000',
        'inkass_type': Inkassa.InkassType.CASH,
        'balance_before': '100000',
        'balance_after': '75000',
        'total_orders': 0,
        'total_revenue': '0',
        'register_command': True,
        'notes': Inkassa.command_notes('remote collection'),
    }

    _instance, first_action = Inkassa.from_sync_dict(
        payload, branch_id='branch-a',
    )
    register.refresh_from_db()
    assert first_action == 'created'
    assert register.current_balance == 75000
    assert register.remote_cash_out_applied_total == 25000
    assert Inkassa.pending_register_amount(register) == 0

    # A crash-safe NULL-lane replay or cursor overlap is harmless.
    _instance, replay_action = Inkassa.from_sync_dict(
        payload, branch_id='branch-a',
    )
    register.refresh_from_db()
    assert replay_action == 'skipped'
    assert register.current_balance == 75000
    assert register.remote_cash_out_applied_total == 25000
    assert Inkassa.pending_register_amount(register) == 0


def test_single_branch_cloud_default_targets_only_branch_scoped_creates(settings):
    from base.models import Category, Customer

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    settings.CLOUD_DEFAULT_TARGET_BRANCH_ID = 'branch1'

    customer = Customer.objects.create(name='Cloud-created customer')
    global_catalog = Category.objects.create(name='Global catalog', slug='global-catalog')
    explicit_other = Customer.objects.create(
        name='Explicit other branch', branch_id='branch-b',
    )

    assert customer.branch_id == 'branch1'
    assert global_catalog.branch_id == 'cloud'
    assert explicit_other.branch_id == 'branch-b'


def test_multi_branch_cloud_requires_an_explicit_target(settings):
    from base.models import Customer

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    settings.CLOUD_DEFAULT_TARGET_BRANCH_ID = ''

    with pytest.raises(ValueError, match='cloud creates must pass branch_id'):
        Customer.objects.create(name='Ambiguous target')
