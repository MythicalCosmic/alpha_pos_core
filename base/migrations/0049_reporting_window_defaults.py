from datetime import time

import base.models
from django.db import migrations, models


def adopt_canonical_defaults(apps, schema_editor):
    settings = apps.get_model('base', 'AppSettings')
    # This rollout establishes one product-wide operating contract. Existing
    # site values are intentionally normalized too: leaving a customized
    # 10:00/04:00 row while every report enforces 07:00/03:00 would make the
    # settings API contradict the money counted by analytics.
    settings.objects.all().update(
        business_day_start=time(7, 0),
        business_open=time(7, 0),
        business_close=time(3, 0),
    )


def restore_previous_defaults(apps, schema_editor):
    settings = apps.get_model('base', 'AppSettings')
    settings.objects.all().update(
        business_day_start=time(3, 0),
        business_open=time(9, 0),
        business_close=time(23, 0),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0048_unique_shift_tender_safe_post'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appsettings',
            name='business_day_start',
            field=models.TimeField(
                default=base.models._default_business_day_start,
            ),
        ),
        migrations.AlterField(
            model_name='appsettings',
            name='business_open',
            field=models.TimeField(default=base.models._default_business_open),
        ),
        migrations.AlterField(
            model_name='appsettings',
            name='business_close',
            field=models.TimeField(default=base.models._default_business_close),
        ),
        migrations.RunPython(
            adopt_canonical_defaults,
            restore_previous_defaults,
        ),
    ]
