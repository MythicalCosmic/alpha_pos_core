"""Customer/CHEF additions: client id on orders syncs; new role is valid."""
import uuid as uuidlib
from decimal import Decimal
import pytest
from base.models import User, Customer, Order

pytestmark = pytest.mark.django_db


def _user():
    return User.objects.create(email='c@t', first_name='A', last_name='B',
                               role='ADMIN', password='x')


def test_chef_role_is_valid_and_passwordless():
    u = User.objects.create(email='chef@t', first_name='C', last_name='H',
                            role='CHEF', password='')
    assert u.role == 'CHEF' and u.password == ''


def test_customer_is_staff_flag():
    c = Customer.objects.create(name='Staff', phone_number='+99890',
                                is_staff=True, branch_id='b')
    assert c.is_staff is True


def test_order_carries_customer_and_syncs():
    u = _user()
    cust = Customer.objects.create(name='Nigora', phone_number='+998901112233',
                                   branch_id='branch1')
    o = Order.objects.create(user=u, customer=cust, order_type='DELIVERY',
                             status='PREPARING', branch_id='branch1',
                             total_amount=Decimal('100'))
    # to_sync_dict emits the client link
    payload = o.to_sync_dict()
    assert payload['customer_uuid'] == str(cust.uuid)

    # a peer receives a NEW order referencing the (already-synced) customer
    payload['uuid'] = str(uuidlib.uuid4())
    inst, action = Order.from_sync_dict(payload, branch_id='branch1')
    assert action == 'created'
    assert inst.customer_id == cust.id   # FK resolved + linked on the peer
