# Adds License.plan_name — display-only subscription plan label from the
# control center (captured on heartbeat / register). See licensing/models.py.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('licensing', '0004_alter_license_status_alter_licenseevent_action'),
    ]

    operations = [
        migrations.AddField(
            model_name='license',
            name='plan_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
