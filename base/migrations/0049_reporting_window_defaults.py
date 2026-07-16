from datetime import time

import base.models
from django.db import migrations, models


def adopt_canonical_defaults(apps, schema_editor):
    settings = apps.get_model('base', 'AppSettings')
    # Preserve genuine per-site customizations while upgrading rows that still
    # carry the former shipped defaults.
    settings.objects.filter(business_day_start=time(3, 0)).update(
        business_day_start=time(7, 0),
    )
    settings.objects.filter(business_open=time(9, 0)).update(
        business_open=time(7, 0),
    )
    settings.objects.filter(business_close=time(23, 0)).update(
        business_close=time(3, 0),
    )


def restore_previous_defaults(apps, schema_editor):
    settings = apps.get_model('base', 'AppSettings')
    settings.objects.filter(business_day_start=time(7, 0)).update(
        business_day_start=time(3, 0),
    )
    settings.objects.filter(business_open=time(7, 0)).update(
        business_open=time(9, 0),
    )
    settings.objects.filter(business_close=time(3, 0)).update(
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
