from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('base', '0056_remove_exclusive_shift_device_slot'),
        ('stock', '0009_aibriefing_location_scope'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransaction',
            name='order_item',
            field=models.ForeignKey(
                blank=True,
                help_text='Exact sold line that caused this movement, when available.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='stock_transactions',
                to='base.orderitem',
            ),
        ),
    ]
