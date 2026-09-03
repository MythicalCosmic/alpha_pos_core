from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('stock', '0016_receiving_base_unit_snapshots'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransaction',
            name='actor_display_snapshot',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='stocktransaction',
            name='command_id',
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='stocktransaction',
            name='idempotency_key',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='stocktransaction',
            name='reversal_of',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='reversal',
                to='stock.stocktransaction',
            ),
        ),
    ]
