from io import StringIO

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def test_tender_audit_cannot_report_clean_when_unpaid_order_has_collected_money(order_factory):
    from base.models import OrderPayment

    order = order_factory(is_paid=False)
    OrderPayment.objects.create(order=order, method='UZCARD', amount='10000')
    with pytest.raises(SystemExit) as stopped:
        call_command('audit_tender_attribution', '--fail', stdout=StringIO())
    assert stopped.value.code == 1
