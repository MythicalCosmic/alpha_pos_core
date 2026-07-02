import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    # Adds the missing User.created_at column (auto_now_add). The 11 pre-existing
    # rows have no historical creation time anywhere, so they are backfilled to the
    # migration run time via the one-off default below (preserve_default=False so
    # future INSERTs use auto_now_add, not this frozen default).

    dependencies = [
        ('base', '0034_order_order_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='created_at',
            field=models.DateTimeField(
                auto_now_add=True,
                default=django.utils.timezone.now,
            ),
            preserve_default=False,
        ),
    ]
