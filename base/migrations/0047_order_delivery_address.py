from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('base', '0046_accounting_recorded_cursor'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='delivery_address',
            field=models.TextField(blank=True, default=''),
        ),
    ]
