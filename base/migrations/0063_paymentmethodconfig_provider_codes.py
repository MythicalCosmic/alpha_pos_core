from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('base', '0062_alter_auditlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='paymentmethodconfig',
            name='code',
            field=models.CharField(max_length=10, unique=True),
        ),
    ]
