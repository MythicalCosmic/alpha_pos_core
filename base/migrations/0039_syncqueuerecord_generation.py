import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0038_cash_register_per_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='syncqueuerecord',
            name='generation',
            field=models.UUIDField(
                db_index=True,
                default=uuid.uuid4,
                editable=False,
            ),
        ),
    ]
