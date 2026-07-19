from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0049_reporting_window_defaults'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='order_origin',
            field=models.CharField(
                choices=[
                    ('POS', 'POS'),
                    ('QR', 'QR'),
                    ('TELEGRAM', 'Telegram'),
                ],
                db_index=True,
                default='POS',
                max_length=16,
            ),
        ),
    ]
