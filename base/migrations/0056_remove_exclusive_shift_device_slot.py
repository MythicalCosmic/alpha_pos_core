from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('base', '0055_alter_auditlog_action'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='shift',
            name='uniq_live_shift_per_device',
        ),
    ]
