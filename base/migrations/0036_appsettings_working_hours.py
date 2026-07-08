import base.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0035_user_created_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='appsettings',
            name='business_open',
            field=models.TimeField(default=base.models._default_business_open),
        ),
        migrations.AddField(
            model_name='appsettings',
            name='business_close',
            field=models.TimeField(default=base.models._default_business_close),
        ),
    ]
