from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_audit_reports_missing_tender_evidence_and_can_fail(
    order_factory,
):
    from base.models import Order

    order = order_factory(status=Order.Status.COMPLETED, is_paid=True)
    Order.objects.filter(pk=order.pk).update(
        payment_method=Order.PaymentMethod.HUMO,
        paid_at=timezone.now(),
    )
    out = StringIO()

    with pytest.raises(SystemExit) as exc:
        call_command('audit_tender_attribution', '--fail', stdout=out)

    assert exc.value.code == 1
    assert 'UNATTRIBUTABLE: 1 paid order(s) worth 10.00' in out.getvalue()
    assert 'no concrete payment evidence' in out.getvalue()


def test_legacy_tender_audit_name_uses_canonical_command():
    from base.management.commands.audit_tender_attribution import (
        Command as CanonicalCommand,
    )
    from base.management.commands.check_tender_attribution import (
        Command as LegacyCommand,
    )

    assert LegacyCommand is CanonicalCommand
