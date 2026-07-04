"""Generic read-only query_db tool: aggregation / rows / group-by, plus the
whitelist, sensitive-field, soft-delete and read-only safety guards."""
import json
import secrets
from decimal import Decimal

import pytest
from django.utils import timezone

from stock.services.ai_tools_service import AIToolbox

pytestmark = pytest.mark.django_db


def _u(role='CASHIER'):
    from base.models import User
    return User.objects.create(email=f'q{secrets.token_hex(4)}@x.local', first_name='Ann',
                               last_name='Lee', role=role, status='ACTIVE',
                               password='SECRET-HASH-VALUE')


def _order(cashier, total='100000', status='COMPLETED', paid=True):
    from base.models import Order
    return Order.objects.create(user=cashier, cashier=cashier, status=status, is_paid=paid,
                                display_id=1, subtotal=total, total_amount=total,
                                payment_method='CASH', paid_at=timezone.now())


def _q(**args):
    return json.loads(AIToolbox.execute('query_db', args))


def test_aggregate_sum_and_count():
    c = _u()
    _order(c, '100000'); _order(c, '50000')
    _order(c, '30000', status='CANCELED', paid=False)
    r = _q(model='order', filters={'is_paid': True},
           aggregate={'revenue': 'sum:total_amount', 'n': 'count'})
    assert r['result']['revenue'] == 150000.0 and r['result']['n'] == 2


def test_group_by():
    c1, c2 = _u(), _u()
    _order(c1, '100000'); _order(c1, '20000'); _order(c2, '5000')
    r = _q(model='order', aggregate={'rev': 'sum:total_amount'}, group_by=['cashier'])
    by = {row['cashier']: row['rev'] for row in r['result']}
    assert by[c1.id] == 120000.0 and by[c2.id] == 5000.0


def test_row_mode_fields_and_decimal_floatified():
    c = _u()
    _order(c, '77000')
    r = _q(model='order', filters={'cashier_id': c.id},
           fields=['total_amount', 'status', 'cashier__first_name'], limit=5)
    assert r['total_matching'] == 1
    row = r['rows'][0]
    assert row['status'] == 'COMPLETED' and row['cashier__first_name'] == 'Ann'
    assert row['total_amount'] == 77000.0            # Decimal -> float


def test_unknown_model_rejected():
    r = _q(model='license')
    assert 'error' in r and 'unknown model' in r['error']


def test_password_never_returned_in_default_rows():
    u = _u()
    r = _q(model='user', filters={'id': u.id})
    assert r['rows'] and 'password' not in r['rows'][0]


def test_sensitive_field_blocked_everywhere():
    _u()
    assert 'error' in _q(model='user', fields=['id', 'password'])
    assert 'error' in _q(model='user', filters={'password': 'x'})
    assert 'error' in _q(model='order', fields=['id', 'cashier__password'])
    assert 'error' in _q(model='user', aggregate={'x': 'max:password'})
    assert 'error' in _q(model='user', order_by=['password'])


def test_soft_deleted_excluded_by_default():
    c = _u()
    o = _order(c, '9000'); o.is_deleted = True; o.save()
    _order(c, '1000')
    r = _q(model='order', aggregate={'n': 'count'})
    assert r['result']['n'] == 1


def test_bad_filter_returns_error_not_crash():
    r = _q(model='order', filters={'nonexistent_field': 1})
    assert 'error' in r and 'bad filter' in r['error']


def test_readonly_no_write_side_effects():
    from base.models import Order
    c = _u()
    _order(c, '1000')
    before = Order.objects.count()
    _q(model='order', aggregate={'n': 'count'})
    _q(model='order', fields=['id'])
    _q(model='order', filters={'status': 'COMPLETED'}, aggregate={'n': 'count'})
    assert Order.objects.count() == before
