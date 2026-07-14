from django.db import migrations, models
from django.db.models import F
from django.db.models.functions import Now


def backfill_accounting_recorded_at(apps, schema_editor):
    """Seed the local receipt cursor from immutable historical event times."""
    database = schema_editor.connection.alias
    Order = apps.get_model('base', 'Order')
    OrderRefund = apps.get_model('base', 'OrderRefund')

    Order.objects.using(database).filter(
        is_paid=True,
        paid_at__isnull=False,
        accounting_recorded_at__isnull=True,
    ).update(accounting_recorded_at=F('paid_at'))
    OrderRefund.objects.using(database).filter(
        accounting_recorded_at__isnull=True,
    ).update(accounting_recorded_at=F('refunded_at'))


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0045_repair_legacy_branch_ownership'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='accounting_recorded_at',
            field=models.DateTimeField(
                blank=True, editable=False, null=True,
            ),
        ),
        migrations.AddField(
            model_name='orderrefund',
            name='accounting_recorded_at',
            field=models.DateTimeField(
                editable=False, null=True,
            ),
        ),
        migrations.RunPython(
            backfill_accounting_recorded_at,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='orderrefund',
            name='accounting_recorded_at',
            field=models.DateTimeField(
                db_default=Now(), editable=False,
            ),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(
                fields=['branch_id', 'accounting_recorded_at'],
                name='order_branch_acct_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='orderrefund',
            index=models.Index(
                fields=['branch_id', 'accounting_recorded_at'],
                name='refund_branch_acct_idx',
            ),
        ),
    ]
