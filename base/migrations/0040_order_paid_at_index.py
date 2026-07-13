from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0039_syncqueuerecord_generation'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='paid_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
