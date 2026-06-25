import base.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0032_order_payment_requested_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='appsettings',
            name='business_day_start',
            field=models.TimeField(default=base.models._default_business_day_start),
        ),
    ]
