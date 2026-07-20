from decimal import Decimal
import uuid

from django.db import migrations, models


def backfill_payment_actions(apps, schema_editor):
    """Give only demonstrably complete legacy settlements a stable identity.

    The UUID is derived from the synced Order UUID, so local and cloud nodes
    independently converge on the same action during a rolling deployment.
    Incomplete/ambiguous evidence deliberately stays NULL: that compatibility
    lane must remain open for a delayed old-client split line to arrive.
    """
    Order = apps.get_model('base', 'Order')
    OrderPayment = apps.get_model('base', 'OrderPayment')
    concrete_methods = {'CASH', 'UZCARD', 'HUMO', 'CARD', 'PAYME'}

    orders = Order.objects.filter(
        is_deleted=False,
        is_paid=True,
        paid_at__isnull=False,
        payment_action_id__isnull=True,
    ).iterator(chunk_size=500)
    for order in orders:
        payments = list(OrderPayment.objects.filter(
            order_id=order.pk,
            is_deleted=False,
        ).order_by('created_at', 'uuid'))
        if not payments:
            continue
        if any(
            payment.payment_action_id is not None
            or payment.line_index is not None
            or payment.branch_id != order.branch_id
            or payment.method not in concrete_methods
            or payment.amount <= 0
            for payment in payments
        ):
            continue

        total = Decimal(order.total_amount or 0)
        if total < 0:
            continue
        noncash = sum(
            (payment.amount for payment in payments
             if payment.method != 'CASH'),
            Decimal('0'),
        )
        cash = sum(
            (payment.amount for payment in payments
             if payment.method == 'CASH'),
            Decimal('0'),
        )
        if noncash > total:
            continue
        residual = total - noncash
        if (cash and cash < residual) or (not cash and residual != 0):
            continue

        methods = {payment.method for payment in payments}
        derived_method = next(iter(methods)) if len(methods) == 1 else 'MIXED'
        if order.payment_method != derived_method:
            # A MIXED header with one arrived tender is the important rolling
            # upgrade edge: leave it anonymous so the late component is not
            # rejected by the new settled-action guard.
            continue

        action_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f'https://alphapos.uz/payment-action/{order.uuid}',
        )
        updated = Order.objects.filter(
            pk=order.pk,
            payment_action_id__isnull=True,
        ).update(payment_action_id=action_id)
        if not updated:
            continue
        for line_index, payment in enumerate(payments):
            OrderPayment.objects.filter(pk=payment.pk).update(
                payment_action_id=action_id,
                line_index=line_index,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0052_external_order_payment'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='payment_action_id',
            field=models.UUIDField(
                blank=True, editable=False, null=True, unique=True,
            ),
        ),
        migrations.AddField(
            model_name='orderpayment',
            name='line_index',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='orderpayment',
            name='payment_action_id',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            backfill_payment_actions,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='orderpayment',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        payment_action_id__isnull=True,
                        line_index__isnull=True,
                    )
                    | models.Q(
                        payment_action_id__isnull=False,
                        line_index__isnull=False,
                    )
                ),
                name='order_payment_action_pair_complete',
            ),
        ),
        migrations.AddConstraint(
            model_name='orderpayment',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    is_deleted=False,
                    payment_action_id__isnull=False,
                ),
                fields=('order', 'payment_action_id', 'line_index'),
                name='uniq_live_order_payment_action_line',
            ),
        ),
    ]
